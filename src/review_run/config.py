"""Load, layer and validate the config (interface.md, Config; design guide section 5).

Layers, lowest first: the schema defaults, ``config.yaml`` plus ``config.d/*.yaml`` (byte-wise
lexical order; maps merge, hook lists append, any other value is replaced), ``REVIEW_RUN_<KEY>``
environment (top-level scalar keys only), ``repos.<id>`` (the exact canonical repo id) and flags.
``limits`` merges key by key across layers.

``schemas/config.json`` is the single validation source; the defaults are read from it too.
Semantic checks the schema cannot express live in ``config_semantic``. Problems are
``Issue(key, problem, source)`` with a fixed vocabulary: a key and a problem, never the offending
value (a config value can be a secret). ``check_config`` returns every issue found (what a
``validate`` command needs); ``load_config`` raises the first as ``config_invalid`` (exit 2) with
``details.key``.
"""

from __future__ import annotations

import copy
import functools
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from jsonschema import Draft202012Validator, validators

from . import config_semantic as sem
from .config_semantic import read_guidelines_file as read_guidelines_file
from .config_yaml import (
    ConfigFileError,
    check_private_dir,
    parse_yaml,
    read_private_file,
)
from .durations import DurationError, parse_duration
from .errors import ReviewRunError, printable

ENV_PREFIX: Final = "REVIEW_RUN_"
MAX_DROP_INS: Final = 256
MAX_TOTAL_BYTES: Final = 1024 * 1024
MAX_ISSUES_IN_MESSAGE: Final = 5
GLOBAL_ONLY: Final = frozenset({"mode", "retention", "escalation_ttl", "me", "hooks"})
_NESTED: Final = ("limits", "me", "hooks")  # layered key by key
_KEY_PART_MAX: Final = 64
_ENV_INT_RE: Final = re.compile(r"(?:0|[1-9][0-9]{0,8})", re.ASCII)
_FILE_KEY: Final = "(file)"
_PATH_VARS: Final = ("HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME")


# -- schema ------------------------------------------------------------------------------------


def _schema_file() -> Path:
    here = Path(__file__).resolve().parent
    for candidate in (here / "schemas", here.parents[1] / "schemas"):
        if (candidate / "config.json").is_file():
            return candidate / "config.json"
    raise FileNotFoundError("schemas/config.json")


def _python_anchors(node: Any) -> Any:
    """Python's ``$`` also matches before a trailing newline (ECMA's does not): a trailing ``$``
    of every pattern becomes ``\\Z`` in the in-memory copy so ``"PATH\\n"`` cannot pass."""
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key == "pattern" and isinstance(value, str):
                value = _anchor(value)
            elif key == "patternProperties" and isinstance(value, dict):
                value = {_anchor(k): _python_anchors(v) for k, v in value.items()}
            else:
                value = _python_anchors(value)
            out[key] = value
        return out
    if isinstance(node, list):
        return [_python_anchors(item) for item in node]
    return node


def _anchor(pattern: str) -> str:
    if pattern.endswith("$") and not pattern.endswith("\\$"):
        return pattern[:-1] + r"\Z"
    return pattern


@functools.cache
def _schema() -> dict[str, Any]:
    document = json.loads(_schema_file().read_text(encoding="utf-8"))
    return _python_anchors(document)


def _is_integer(_checker: Any, instance: object) -> bool:
    """JSON Schema counts 2000.0 as an integer; a config integer must be an int (not a bool)."""
    return isinstance(instance, int) and not isinstance(instance, bool)


_Validator: Final = validators.extend(
    Draft202012Validator,
    type_checker=Draft202012Validator.TYPE_CHECKER.redefine("integer", _is_integer),
)


def _without_required(node: Any) -> Any:
    """An in-memory copy of a schema with every ``required`` list removed (a drop-in may set
    part of an entry; the merged document is validated against the full schema)."""
    if isinstance(node, dict):
        return {
            key: _without_required(value)
            for key, value in node.items()
            if not (key == "required" and isinstance(value, list))
        }
    if isinstance(node, list):
        return [_without_required(item) for item in node]
    return node


@functools.cache
def _validator() -> Draft202012Validator:
    return _Validator(_schema())


@functools.cache
def _partial_validator() -> Draft202012Validator:
    return _Validator(_without_required(_schema()))


def _deref(node: dict[str, Any]) -> dict[str, Any]:
    if "$ref" not in node:
        return node
    target = _schema()["$defs"][node["$ref"].rsplit("/", 1)[1]]
    return {**target, **{k: v for k, v in node.items() if k != "$ref"}}


def _repo_schema() -> dict[str, Any]:
    patterns = _schema()["properties"]["repos"]["patternProperties"]
    return next(iter(patterns.values()))


def _node_default(node: dict[str, Any]) -> Any:
    node = _deref(node)
    if node.get("type") == "object" and "properties" in node:
        return {
            key: _node_default(sub)
            for key, sub in node["properties"].items()
            if "default" in _deref(sub) or _deref(sub).get("type") == "object"
        }
    return copy.deepcopy(node.get("default"))


def defaults() -> dict[str, Any]:
    """Every global key at its schema default (``repos`` and ``run_pr_code`` excluded)."""
    return {
        key: _node_default(node) for key, node in _schema()["properties"].items() if key != "repos"
    }


def _run_pr_code_defaults() -> dict[str, Any]:
    props = _repo_schema()["properties"]["run_pr_code"]["properties"]
    return {key: copy.deepcopy(node["default"]) for key, node in props.items() if "default" in node}


# -- issues ------------------------------------------------------------------------------------


@dataclass(frozen=True, order=True)
class Issue:
    """One problem: where (a dotted key), what (fixed text) and in which source."""

    key: str
    problem: str
    source: str


def _part(value: object) -> str:
    return printable(str(value), _KEY_PART_MAX)


def _key(parts: tuple[object, ...]) -> str:
    out = ""
    for part in parts:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += ("." if out else "") + _part(part)
    return out or "(top level)"


_KEYWORD_PROBLEMS: Final = {
    "type": "has the wrong type",
    "pattern": "has an invalid format",
    "enum": "is not an allowed value",
    "const": "is not an allowed value",
    "minimum": "is out of range",
    "maximum": "is out of range",
    "minLength": "must not be empty",
    "maxLength": "is too long",
    "minItems": "must not be empty",
    "uniqueItems": "must not repeat an entry",
    "oneOf": "is not a valid value",
    "anyOf": "is not a valid value",
}


def _schema_errors(
    validator: Draft202012Validator, document: Any
) -> list[tuple[tuple[object, ...], str]]:
    found: list[tuple[tuple[object, ...], str]] = []
    errors = sorted(validator.iter_errors(document), key=lambda e: [str(p) for p in e.path])
    for error in errors:
        path = tuple(error.absolute_path)
        keyword = str(error.validator)
        if keyword == "additionalProperties" and isinstance(error.instance, dict):
            schema = error.schema if isinstance(error.schema, dict) else {}
            known = set(schema.get("properties", {}))
            patterns = [re.compile(p) for p in schema.get("patternProperties", {})]
            for extra in error.instance:
                if extra in known or any(p.search(str(extra)) for p in patterns):
                    continue
                found.append(_extra(path, str(extra)))
        elif keyword == "required" and isinstance(error.instance, dict):
            for name in error.validator_value:
                if name not in error.instance:
                    found.append(((*path, name), "is required"))
        else:
            found.append((path, _KEYWORD_PROBLEMS.get(keyword, "is not valid")))
    return found


_ECHOABLE_KEY_RE: Final = re.compile(r"[a-z_][a-z0-9_.-]{0,40}", re.ASCII)


def _extra(path: tuple[object, ...], name: str) -> tuple[tuple[object, ...], str]:
    """An unknown key: named only when it looks like a key name (it could be a pasted secret);
    otherwise the parent is reported without the name."""
    if _ECHOABLE_KEY_RE.fullmatch(name) is None:
        what = "an invalid repo id" if path == ("repos",) else "an unknown key"
        return path, f"contains {what}"
    where = (*path, name)
    if path == ("repos",):
        return where, "is not a valid repo id (host/<full project path>)"
    if len(path) == 2 and path[0] == "repos" and name in GLOBAL_ONLY:
        return where, "is global only and cannot be set per repo"
    if not path and name == "run_pr_code":
        return where, "is per repo only (under repos.<id>)"
    return where, "is not a known key"


def schema_issues(document: object, source: str) -> list[Issue]:
    """The schema problems of ``document`` (a whole config, or any subset of its keys)."""
    return _issues(_validator(), document, source)


def _issues(validator: Draft202012Validator, document: object, source: str) -> list[Issue]:
    errors = _schema_errors(validator, document)
    return sorted(Issue(_key(parts), problem, source) for parts, problem in errors)


# -- semantic checks and normalization -----------------------------------------------------------

_ItemProblems = list[tuple[tuple[object, ...], str]]


def _positive(text: object, *, allow_zero: bool = False) -> str | None:
    if not isinstance(text, str):
        return None
    try:
        parse_duration(text, positive=not allow_zero)
    except DurationError as exc:
        return str(exc)
    return None


def _normalize_scope(
    scope: dict[str, Any], path: tuple[object, ...], env: Mapping[str, str] | None = None
) -> tuple[dict[str, Any], _ItemProblems]:
    """A schema-valid global or per-repo mapping with its semantic checks applied; paths and
    ``~`` are normalized (plugin and bind realpaths, the expanded guidelines path)."""
    out = copy.deepcopy(scope)
    problems: _ItemProblems = []

    def add(parts: tuple[object, ...], problem: str) -> None:
        problems.append(((*path, *parts), problem))

    for name in ("timeout",):
        if (why := _positive(out.get(name))) is not None:
            add((name,), why)
    for name in ("retention", "escalation_ttl"):
        if (why := _positive(out.get(name), allow_zero=True)) is not None:
            add((name,), why)
    if isinstance(out.get("guidelines_file"), str):
        try:
            out["guidelines_file"] = sem.expand_guidelines_path(out["guidelines_file"], env)
            sem.read_guidelines_file(out["guidelines_file"])
        except sem.GuidelinesError as exc:
            add(("guidelines_file",), str(exc))
    if isinstance(out.get("permission_profile"), str):
        if (why := sem.permission_profile_refusal(out["permission_profile"])) is not None:
            add(("permission_profile",), why)
    model = out.get("model")
    if isinstance(model, str):
        if sem.has_control_char(model):
            add(("model",), "must not contain control characters")
        elif model.startswith("-"):
            add(("model",), "must not start with '-'")
    if isinstance(out.get("plugin_dirs"), list):
        out["plugin_dirs"], found = sem.check_plugin_dirs(out["plugin_dirs"])
        for index, why in found:
            add(("plugin_dirs", index), why)
    _check_env_names(out.get("env_allowlist"), ("env_allowlist",), add)
    for hook, entries in (out.get("hooks") or {}).items():
        for index, entry in enumerate(entries):
            if (why := _positive(entry.get("timeout"))) is not None:
                add(("hooks", hook, index, "timeout"), why)
            if sem.has_control_char(entry.get("cmd", ""), allow="\n\t"):
                add(("hooks", hook, index, "cmd"), "must not contain control characters")
    code = out.get("run_pr_code")
    if isinstance(code, dict):
        if (why := _positive(code.get("timeout"))) is not None:
            add(("run_pr_code", "timeout"), why)
        for index, arg in enumerate(code.get("command") or []):
            if sem.has_control_char(arg):
                add(("run_pr_code", "command", index), "must not contain control characters")
        _check_env_names(code.get("env_allow"), ("run_pr_code", "env_allow"), add)
        if isinstance(code.get("ro_binds"), list):
            code["ro_binds"], found = sem.check_ro_binds(code["ro_binds"], env)
            for index, why in found:
                add(("run_pr_code", "ro_binds", index), why)
    return out, problems


def _check_env_names(names: object, where: tuple[object, ...], add: Any) -> None:
    if isinstance(names, list):
        for index, name in enumerate(names):
            if (why := sem.env_name_refusal(name)) is not None:
                add((*where, index), why)


def _normalize_document(
    document: dict[str, Any], source: str, env: Mapping[str, str] | None = None
) -> tuple[dict[str, Any], list[Issue]]:
    """Semantic checks of a merged file document: the global scope and every repo entry."""
    out, problems = _normalize_scope({k: v for k, v in document.items() if k != "repos"}, (), env)
    repos: dict[str, Any] = {}
    for repo_id, entry in document.get("repos", {}).items():
        repos[repo_id], found = _normalize_scope(entry, ("repos", repo_id), env)
        problems.extend(found)
    out["repos"] = repos
    return out, sorted(Issue(_key(parts), why, source) for parts, why in problems)


# -- environment -----------------------------------------------------------------------------------


def _json_types(node: dict[str, Any]) -> set[str]:
    node = _deref(node)
    kinds: set[str] = set()
    declared = node.get("type", [])
    kinds.update([declared] if isinstance(declared, str) else declared)
    if "enum" in node:
        kinds.add("string")
    for branch in node.get("oneOf", []):
        kinds |= _json_types(branch)
    return kinds


def env_keys() -> dict[str, set[str]]:
    """The top-level scalar keys that ``REVIEW_RUN_<KEY>`` can set, with their JSON types."""
    keys: dict[str, set[str]] = {}
    for key, node in _schema()["properties"].items():
        kinds = _json_types(node)
        if kinds and not kinds & {"array", "object"}:
            keys[key] = kinds
    return keys


def _parse_env_value(text: str, kinds: set[str]) -> object | None:
    """The typed value, or the ``_BAD`` sentinel for a text that is none of the key's types."""
    if "null" in kinds and text == "null":
        return None
    if "boolean" in kinds:
        return {"true": True, "false": False}.get(text, _BAD)
    if "integer" in kinds:
        return int(text) if _ENV_INT_RE.fullmatch(text) else _BAD
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text):
        return _BAD
    return text


_BAD: Final = object()


def _env_layer(env: Mapping[str, str]) -> tuple[dict[str, Any], list[Issue]]:
    layer: dict[str, Any] = {}
    issues: list[Issue] = []
    for key, kinds in env_keys().items():
        name = ENV_PREFIX + key.upper()
        if name not in env:
            continue
        value = _parse_env_value(env[name], kinds)
        if value is _BAD:
            issues.append(Issue(name, "is not a valid value for this key", "environment"))
            continue
        found = schema_issues({key: value}, "environment")
        if found:
            issues.extend(Issue(name, i.problem, "environment") for i in found[:1])
            continue
        normalized, semantic = _normalize_scope({key: value}, (), env)
        if semantic:
            issues.extend(Issue(name, i[1], "environment") for i in semantic[:1])
            continue
        layer[key] = normalized[key]
    return layer, issues


# -- files ---------------------------------------------------------------------------------------


def default_config_path(env: Mapping[str, str] | None = None) -> Path:
    """``$XDG_CONFIG_HOME/review-run/config.yaml`` (when absolute), else ``~/.config/...``;
    the variables come from ``env`` (the process environment when None)."""
    xdg = (os.environ if env is None else env).get("XDG_CONFIG_HOME", "")
    base = Path(xdg) if xdg.startswith("/") else Path(sem.home_dir(env)) / ".config"
    return base / "review-run" / "config.yaml"


def merge(base: dict[str, Any], over: dict[str, Any], path: tuple[str, ...] = ()) -> dict[str, Any]:
    """``over`` on top of ``base``: maps merge, hook lists append, anything else is replaced."""
    out = dict(base)
    for key, value in over.items():
        current = out.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            out[key] = merge(current, value, (*path, key))
        elif path == ("hooks",) and isinstance(current, list) and isinstance(value, list):
            out[key] = current + value
        else:
            out[key] = copy.deepcopy(value)
    return out


def _display(path: Path) -> str:
    return printable(str(path))


def _discover(
    explicit: Path | None, issues: list[Issue], env: Mapping[str, str] | None
) -> list[Path]:
    path = explicit if explicit is not None else default_config_path(env)
    files: list[Path] = []
    if explicit is not None or os.path.lexists(path):
        files.append(path)
    directory = path.parent / "config.d"
    try:
        if not check_private_dir(directory):
            return files
        names = sorted(os.listdir(directory), key=os.fsencode)
    except (ConfigFileError, OSError) as exc:
        issues.append(Issue(_FILE_KEY, f"cannot be used: {exc}", _display(directory)))
        return files
    chosen = [n for n in names if n.endswith(".yaml") and not n.startswith(".")]
    if len(chosen) > MAX_DROP_INS:
        issues.append(
            Issue(_FILE_KEY, f"more than {MAX_DROP_INS} drop-in files", _display(directory))
        )
        return files
    files.extend(directory / n for n in chosen)
    return files


@dataclass(frozen=True)
class Resolved:
    """The values of one run: every key of the Config table and where each came from.

    ``origins`` maps a key (``limits.<k>``, ``me.name`` and ``hooks.<name>`` individually) to
    ``flag``, ``repo``, ``env``, ``config`` or ``default``.
    """

    values: dict[str, Any]
    origins: dict[str, str]


@dataclass(frozen=True)
class Config:
    """A loaded, validated and normalized config; ``resolve`` layers it for one run."""

    file: dict[str, Any]
    env: dict[str, Any]
    files: tuple[Path, ...] = field(default=())
    # the path variables (HOME, XDG_*) the config was loaded with; flags are normalized with them
    environ: Mapping[str, str] | None = field(default=None, repr=False, compare=False)

    def resolve(self, repo: str | None = None, flags: Mapping[str, Any] | None = None) -> Resolved:
        """The effective values: defaults, config files, environment, ``repos.<repo>`` (the exact
        canonical id), then ``flags`` (only the keys the user gave; ``None`` is a value).

        Raises ``usage`` naming the key when a flag value is invalid, ``flags`` is not a mapping
        or ``repo`` is not a non-empty string.
        """
        if repo is not None and (not isinstance(repo, str) or not repo):
            raise ReviewRunError("usage", "the repo must be a non-empty string or absent")
        if flags is not None and not isinstance(flags, Mapping):
            raise ReviewRunError("usage", "flags must be a mapping")
        flag_layer = self._flag_layer({} if flags is None else flags)
        values = defaults()
        values["run_pr_code"] = None
        origins: dict[str, str] = {}
        _mark(values, origins, "default")
        _apply(values, origins, {k: v for k, v in self.file.items() if k != "repos"}, "config")
        _apply(values, origins, self.env, "env")
        entry = self.file.get("repos", {}).get(repo) if repo is not None else None
        if entry is not None:
            _apply(values, origins, entry, "repo")
        _apply(values, origins, flag_layer, "flag")
        return Resolved(values, origins)

    def _flag_layer(self, flags: Mapping[str, Any]) -> dict[str, Any]:
        allowed = set(_schema()["properties"]) - {"repos"}
        for name in flags:
            if not isinstance(name, str) or name not in allowed:
                key = _part(name)
                raise ReviewRunError(
                    "usage", f"flag for {key}: is not a config key", details={"key": key}
                )
        layer = copy.deepcopy(dict(flags))
        found = [(i.key, i.problem) for i in schema_issues(layer, "flag")]
        if not found:
            layer, problems = _normalize_scope(layer, (), self.environ)
            found = [(_key(parts), why) for parts, why in problems]
        if found:
            key, problem = found[0]
            raise ReviewRunError("usage", f"flag for {key}: {problem}", details={"key": key})
        return layer


def _mark(values: dict[str, Any], origins: dict[str, str], origin: str) -> None:
    for key, value in values.items():
        if key in _NESTED:
            for sub in value:
                origins[f"{key}.{sub}"] = origin
        else:
            origins[key] = origin


def _apply(
    values: dict[str, Any], origins: dict[str, str], layer: dict[str, Any], origin: str
) -> None:
    for key, value in layer.items():
        if key in _NESTED:
            for sub, subvalue in value.items():
                values[key][sub] = copy.deepcopy(subvalue)
                origins[f"{key}.{sub}"] = origin
        elif key == "run_pr_code":
            values[key] = {**_run_pr_code_defaults(), **copy.deepcopy(value)}
            origins[key] = origin
        else:
            values[key] = copy.deepcopy(value)
            origins[key] = origin


@dataclass(frozen=True)
class CheckResult:
    """``config`` is None when ``issues`` is not empty."""

    config: Config | None
    issues: list[Issue]


def check_config(explicit: Path | None = None, env: Mapping[str, str] | None = None) -> CheckResult:
    """Read, validate and normalize every config source, collecting every issue.

    ``explicit`` is the ``--config`` file (it must exist; its ``config.d`` is read next to it);
    without it the default file may be absent. ``env`` defaults to the process environment.
    Semantic checks run only when the schema checks of the sources pass.
    """
    environment = os.environ if env is None else env
    issues: list[Issue] = []
    files = _discover(explicit, issues, environment)
    merged: dict[str, Any] = {}
    total = 0
    for path in files:
        name = _display(path)
        try:
            data = read_private_file(path)
            total += len(data)
            if total > MAX_TOTAL_BYTES:
                raise ConfigFileError(f"the config files together exceed {MAX_TOTAL_BYTES} bytes")
            document = parse_yaml(data)
        except ConfigFileError as exc:
            issues.append(Issue(_FILE_KEY, str(exc), name))
            continue
        found = _issues(_partial_validator(), document, name)
        issues.extend(found)
        if not found:
            merged = merge(merged, document)
    env_layer, env_issues = _env_layer(environment)
    issues.extend(env_issues)
    if not issues:
        issues.extend(schema_issues(merged, "merged config"))
    normalized: dict[str, Any] = {}
    if not issues:
        normalized, semantic = _normalize_document(merged, "config", environment)
        issues.extend(semantic)
    if issues:
        return CheckResult(None, sorted(set(issues)))
    paths = {k: environment[k] for k in _PATH_VARS if k in environment}
    return CheckResult(Config(normalized, env_layer, tuple(files), paths), [])


def load_config(explicit: Path | None = None, env: Mapping[str, str] | None = None) -> Config:
    """``check_config`` that raises ``config_invalid`` (exit 2) naming the first issue's key."""
    result = check_config(explicit, env)
    if result.config is None:
        raise error_from(result.issues)
    return result.config


def error_from(issues: list[Issue]) -> ReviewRunError:
    """The ``config_invalid`` error for a non-empty list of issues."""
    lines = []
    for issue in issues[:MAX_ISSUES_IN_MESSAGE]:
        if issue.key == _FILE_KEY:
            lines.append(f"{issue.source}: {issue.problem}")
        else:
            lines.append(f"{issue.source}: {issue.key} {issue.problem}")
    if len(issues) > MAX_ISSUES_IN_MESSAGE:
        lines.append(f"and {len(issues) - MAX_ISSUES_IN_MESSAGE} more")
    message = "; ".join(lines)
    if len(message) > 500:
        message = message[:497] + "..."
    return ReviewRunError("config_invalid", message, details={"key": issues[0].key})
