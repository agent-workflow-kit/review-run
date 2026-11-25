"""Hooks: specs, environment, payloads, ``pre-run`` outcomes and at-least-once outbox delivery.

Interface: ``docs/interface.md`` section Hooks (normative). Rules kept here:

* A hook entry runs as ``sh -c <cmd>`` through the gateway (the only module that starts
  processes), in its own process group; the JSON payload is the child's stdin. Nothing from the
  payload is ever in argv or env. The environment is the caller's (the interface: hooks inherit
  review-run's environment, review-run configures no token variable to strip) with exactly four
  review-run variables set: ``REVIEW_RUN_HOOK``, ``REVIEW_RUN_ID``, ``REVIEW_RUN_OBJECT_TYPE``
  and, for post hooks, ``REVIEW_RUN_EVENT_ID``. Stale values of these four are replaced.
* A timeout ends the hook's own process group: SIGTERM, a grace of ``TERM_GRACE_S``, SIGKILL
  (the gateway does this for the group it created).
* A hook's stdout and stderr are untrusted and are never logged, audited, printed or kept in a
  failure: only closed codes and counts leave this module. The one use of stdout is the
  ``decision_id`` of a ``pre-run`` hook, accepted only by the id grammar.
* ``pre-run`` fails closed: any exit but 0 and 13, a timeout, or a hook that cannot start is a
  veto. Post hooks never veto and never change a run beyond the ``hook_failed`` warning.
* Delivery is at least once. Per event: take the event's lease, append its audit records, record
  the attempt, run the entries, then mark it delivered. A crash anywhere repeats the entries with
  the same ``event_id``. After ``MAX_ATTEMPTS`` failed passes the failing entries are abandoned
  (run warning ``hook_failed`` and a log record) and the event is marked delivered; the audit
  records are appended without that bound.

Seams: ``run`` (#11) calls ``run_pre_run`` and stores ``hooks.add_flag_hooks`` output with the run;
the code that ends or escalates a run builds the payloads here into ``NewEvent`` rows and calls
``deliver_pending`` right after the commit; ``reconcile`` (#11) calls it for the redelivery pass and
reports ``stuck_audit``. An ``async`` entry's timeout is enforced by the gateway's supervisor
process, which outlives this one (a hook that exits leaving background descendants gives those no
timeout: a documented residual). An invalid stored ``--hook`` entry is never skipped: ``pre-run``
vetoes and a post hook counts a failed attempt (fail closed).
TODO(#11): wire the CLI and the finalize paths to these functions.
"""

from __future__ import annotations

import dataclasses
import json
import math
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any, Final, Literal

from . import gateway
from .audit import AuditWriter, Caller, LogWriter, flush_outbox, make_audit_record
from .durations import DurationError, parse_duration
from .errors import DECISION_ID_RE, MAX_DECISION_IDS, ReviewRunError
from .schema_anchors import python_anchors
from .store import Store

HOOK_NAMES: Final = ("pre-run", "post-run", "post-escalate")
POST_HOOKS: Final = ("post-run", "post-escalate")
DEFAULT_TIMEOUT_S: Final = 30.0
TERM_GRACE_S: Final = 5.0
MAX_ATTEMPTS: Final = 5
ESCALATE_EXIT: Final = 13
SHELL: Final = ("/bin/sh", "-c")
MAX_DECISION_STDOUT: Final = 64 * 1024
MIN_LEASE_SECONDS: Final = 300
LEASE_SLACK_SECONDS: Final = 60

_OWN_ENV: Final = (
    "REVIEW_RUN_HOOK",
    "REVIEW_RUN_ID",
    "REVIEW_RUN_OBJECT_TYPE",
    "REVIEW_RUN_EVENT_ID",
)
_RUN_ID_RE: Final = re.compile(r"rr-[a-z2-7]{8}\Z")
_EVENT_ID_RE: Final = re.compile(r"[A-Za-z0-9_-]{8,64}\Z")


# -- specs ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class HookSpec:
    """One hook entry: a shell command, its timeout in seconds and whether it is detached."""

    cmd: str
    timeout: float = DEFAULT_TIMEOUT_S
    is_async: bool = False
    #: A stored entry that could not be read: it never runs; ``pre-run`` vetoes and a post hook
    #: counts a failed attempt.
    invalid: bool = False


def _clean_cmd(cmd: object) -> str | None:
    if not isinstance(cmd, str) or not cmd:
        return None
    if any((ord(c) < 32 and c not in "\n\t") or ord(c) == 127 for c in cmd):
        return None
    return cmd


def _config_spec(hook: str, entry: object) -> HookSpec | None:
    if not isinstance(entry, Mapping):
        return None
    cmd = _clean_cmd(entry.get("cmd"))
    if cmd is None:
        return None
    text = entry.get("timeout")
    try:
        timeout = (
            DEFAULT_TIMEOUT_S
            if text is None
            else parse_duration(str(text), positive=True).total_seconds()
        )
    except DurationError:
        return None
    return HookSpec(cmd, timeout, bool(entry.get("async")) and hook != "pre-run")


def specs_for(
    hook: str, config_hooks: Mapping[str, Any] | None, stored: Iterable[object] = ()
) -> list[HookSpec]:
    """The entries of ``hook``: the config's list in order, then the run's stored ``--hook``
    entries (``NAME=CMD``, split at the first ``=``; always synchronous with the default
    timeout). An invalid config entry is skipped (config validation refuses it at load). A stored
    entry that is not ``NAME=CMD`` with a known name and a clean command fails closed: it becomes
    an ``invalid`` spec in every hook's list, never run and never silently dropped."""
    out: list[HookSpec] = []
    for entry in (config_hooks or {}).get(hook) or []:
        if (found := _config_spec(hook, entry)) is not None:
            out.append(found)
    for item in stored:
        parsed = _stored_entry(item)
        if parsed is None:
            out.append(HookSpec("", invalid=True))
        elif parsed[0] == hook:
            out.append(HookSpec(parsed[1]))
    return out


def _stored_entry(item: object) -> tuple[str, str] | None:
    if not isinstance(item, str):
        return None
    name, sep, cmd = item.partition("=")
    clean = _clean_cmd(cmd)
    if not sep or name not in HOOK_NAMES or clean is None:
        return None
    return name, clean


def add_flag_hooks(stored: Sequence[str], new: Iterable[str]) -> list[str]:
    """``stored`` plus ``new`` in order, an identical entry kept once. A ``--hook`` entry that is
    not ``NAME=CMD`` with a hook name and a clean command is a usage error (exit 2); the entry's
    text is never echoed."""
    out = list(stored)
    for item in new:
        if _stored_entry(item) is None:
            raise ReviewRunError(
                "usage", "invalid --hook entry: expected NAME=CMD with a hook name"
            )
        if item not in out:
            out.append(item)
    return out


def event_id_for(run_id: str, hook: str, n: int) -> str:
    """``<run id>-<hook>-<n>``: ``n`` is 1 for ``post-run``, the escalation's run number for
    ``post-escalate``."""
    if hook not in POST_HOOKS or isinstance(n, bool) or not isinstance(n, int) or n < 1:
        raise ValueError("an event id needs a post hook and a positive number")
    if not _RUN_ID_RE.match(run_id):
        raise ValueError("not a run id")
    return f"{run_id}-{hook}-{n}"


# -- environment ---------------------------------------------------------------------------


def hook_env(
    hook: str,
    object_id: str,
    *,
    event_id: str | None = None,
    base: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The child's complete environment: ``base`` (default: ours) with the four variables set."""
    if hook not in HOOK_NAMES:
        raise ValueError("unknown hook")
    if not _RUN_ID_RE.match(object_id):
        raise ValueError("object_id is not a run id")
    if (hook in POST_HOOKS) != (event_id is not None):
        raise ValueError("a post hook has an event id and pre-run has none")
    if event_id is not None and not _EVENT_ID_RE.match(event_id):
        raise ValueError("not an event id")
    env = {k: v for k, v in (os.environ if base is None else base).items() if k not in _OWN_ENV}
    env["REVIEW_RUN_HOOK"] = hook
    env["REVIEW_RUN_ID"] = object_id
    env["REVIEW_RUN_OBJECT_TYPE"] = "run"
    if event_id is not None:
        env["REVIEW_RUN_EVENT_ID"] = event_id
    return env


# -- payloads ------------------------------------------------------------------------------


def _schemas_dir() -> Path:
    here = Path(__file__).resolve().parent
    for candidate in (here / "schemas", here.parents[1] / "schemas"):
        if (candidate / "defs.json").is_file():
            return candidate
    raise FileNotFoundError("schemas/defs.json")


@cache
def _validators() -> dict[str, Any]:
    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource

    root = _schemas_dir()
    registry: Registry[Any] = Registry()
    docs: dict[str, Any] = {}
    for path in sorted(root.rglob("*.json")):
        doc = python_anchors(json.loads(path.read_text(encoding="utf-8")))
        registry = registry.with_resource(doc["$id"], Resource.from_contents(doc))
        docs[str(path.relative_to(root))] = doc
    return {
        name: Draft202012Validator(docs[f"hooks/{name}.json"], registry=registry)
        for name in HOOK_NAMES
    }


def validate_payload(hook: str, payload: Mapping[str, Any]) -> None:
    """Check ``payload`` against ``schemas/hooks/<hook>.json``; ``ValueError`` (without the
    offending value) when it does not match. Unknown keys are refused, so a payload cannot carry
    anything the schema does not name."""
    if hook not in HOOK_NAMES:
        raise ValueError("unknown hook")
    validator = _validators()[hook]
    errors = sorted(validator.iter_errors(dict(payload)), key=lambda e: list(e.absolute_path))
    if errors:
        where = "/".join(str(p) for p in errors[0].absolute_path) or "(root)"
        raise ValueError(f"the {hook} payload does not match its schema at {where}")


def _check_event_id(hook: str, run_id: str, event_id: str) -> None:
    prefix = f"{run_id}-{hook}-"
    number = event_id[len(prefix) :] if event_id.startswith(prefix) else ""
    if not number.isascii() or not number.isdigit() or int(number) < 1:
        raise ValueError("the event id does not belong to this run and hook")
    if hook == "post-run" and number != "1":
        raise ValueError("the event id does not belong to this run and hook")


def _common(
    hook: str,
    run_id: str,
    repo: str,
    labels: Sequence[str],
    actor: str,
    decision_ids: Sequence[str],
) -> dict[str, Any]:
    return {
        "tool": "review-run",
        "hook": hook,
        "object_type": "run",
        "object_id": run_id,
        "repo": repo,
        "labels": list(labels),
        "actor": actor,
        "decision_ids": list(decision_ids),
    }


def pre_run_payload(
    *,
    run_id: str,
    repo: str,
    labels: Sequence[str],
    pr: str,
    skill: str,
    mode: str,
    previous_run: str | None,
    head_sha: str,
    pr_facts: Mapping[str, Any],
    will_run_code: bool,
    code_run_sandbox: str | None,
    override_escalation: bool,
    session_sandbox: str,
    subagents: bool,
    permission_profile: str,
    setting_sources: Sequence[str],
    sizes: Mapping[str, Any],
    actor: str,
    decision_ids: Sequence[str],
) -> dict[str, Any]:
    payload = _common("pre-run", run_id, repo, labels, actor, decision_ids)
    payload.update(
        pr=pr,
        skill=skill,
        mode=mode,
        previous_run=previous_run,
        head_sha=head_sha,
        pr_facts=dict(pr_facts),
        will_run_code=will_run_code,
        code_run_sandbox=code_run_sandbox,
        override_escalation=override_escalation,
        session_sandbox=session_sandbox,
        subagents=subagents,
        permission_profile=permission_profile,
        setting_sources=list(setting_sources),
        sizes=dict(sizes),
    )
    validate_payload("pre-run", payload)
    return payload


_SUMMARY_KEYS: Final = (
    "verdict",
    "severity_counts",
    "risk",
    "coverage_status",
    "code_run_status",
    "head_moved",
)


def post_run_payload(
    *,
    run_id: str,
    repo: str,
    labels: Sequence[str],
    event_id: str,
    state: str,
    reason: str | None,
    exit_code: int,
    pr: str,
    agent_id: str | None,
    draft_id: str | None,
    anchor_head_sha: str | None,
    actor: str,
    decision_ids: Sequence[str],
    warnings: Sequence[str],
    summary: Mapping[str, Any] | None = None,
    session_sandbox: str | None = None,
    tools_checked: bool | None = None,
    tools_unexpected: int | None = None,
) -> dict[str, Any]:
    """The ``post-run`` payload. ``summary`` is the run's ``summary`` object: only the six
    fields the schema names are taken from it (never ``risk_reasons`` or any text). The ``done``
    only fields are dropped for a ``failed`` run."""
    _check_event_id("post-run", run_id, event_id)
    payload = _common("post-run", run_id, repo, labels, actor, decision_ids)
    payload.update(
        event_id=event_id,
        state=state,
        reason=reason,
        exit_code=exit_code,
        pr=pr,
        agent_id=agent_id,
        draft_id=draft_id,
        anchor_head_sha=anchor_head_sha,
        warnings=list(warnings),
    )
    if state == "done":
        if summary is None:
            raise ValueError("a done run's payload needs its summary")
        for key in _SUMMARY_KEYS:
            payload[key] = summary.get(key)
        payload.update(
            session_sandbox=session_sandbox,
            tools_checked=tools_checked,
            tools_unexpected=tools_unexpected,
        )
    validate_payload("post-run", payload)
    return payload


def post_escalate_payload(
    *,
    run_id: str,
    repo: str,
    labels: Sequence[str],
    event_id: str,
    reason: str,
    pr: str,
    agent_id: str | None,
    actor: str,
    decision_ids: Sequence[str],
    session_sandbox: str,
    warnings: Sequence[str],
) -> dict[str, Any]:
    _check_event_id("post-escalate", run_id, event_id)
    payload = _common("post-escalate", run_id, repo, labels, actor, decision_ids)
    payload.update(
        event_id=event_id,
        reason=reason,
        pr=pr,
        agent_id=agent_id,
        session_sandbox=session_sandbox,
        warnings=list(warnings),
    )
    validate_payload("post-escalate", payload)
    return payload


# -- one hook call -------------------------------------------------------------------------


@dataclass(frozen=True)
class EntryResult:
    """The outcome of one entry. ``code`` is a closed code: ``ok``, ``exit``, ``timeout``,
    ``tool_missing``, ``output_too_large``, ``invalid`` or ``error``. ``stdout`` is untrusted and
    only read by the ``pre-run`` decision-id parser; it is not part of ``repr``."""

    ok: bool
    code: str
    returncode: int | None = None
    stdout: str = field(default="", repr=False)


def run_spec(
    spec: HookSpec,
    hook: str,
    payload: Mapping[str, Any],
    *,
    env: Mapping[str, str],
    shell: Sequence[str] = SHELL,
) -> EntryResult:
    """Run one entry with ``payload`` as JSON on stdin. A synchronous entry is ok when it exits
    0; an ``async`` one when it started (it cannot be observed after that)."""
    if spec.invalid:
        return EntryResult(False, "invalid")
    argv = [*shell, spec.cmd]
    stdin = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
    try:
        if spec.is_async:
            gateway.spawn_detached(
                argv, timeout=spec.timeout, stdin=stdin, env=env, term_grace=TERM_GRACE_S
            )
            return EntryResult(True, "ok")
        done = gateway.run(
            argv, timeout=spec.timeout, stdin=stdin, env=env, term_grace=TERM_GRACE_S
        )
    except gateway.CallTimeout:
        return EntryResult(False, "timeout")
    except gateway.ToolMissing:
        return EntryResult(False, "tool_missing")
    except gateway.OutputTooLarge:
        return EntryResult(False, "output_too_large")
    except gateway.GatewayError:
        return EntryResult(False, "error")
    except ValueError:
        return EntryResult(False, "invalid")
    except (OSError, RuntimeError):  # e.g. no thread, no space for the stdin file
        return EntryResult(False, "error")
    if done.returncode == 0:
        return EntryResult(True, "ok", 0, done.stdout)
    return EntryResult(False, "exit", done.returncode, done.stdout)


# -- pre-run -------------------------------------------------------------------------------


@dataclass(frozen=True)
class PreRunResult:
    outcome: Literal["continue", "veto", "escalate"]
    hook: str | None = None  # "pre-run" for a veto or an escalation
    decision_ids: tuple[str, ...] = ()
    overridden: bool = False  # an escalation that ``--override-escalation`` let pass


def _decision_ids(stdout: str) -> list[str]:
    if not stdout or len(stdout) > MAX_DECISION_STDOUT:
        return []
    try:
        doc = json.loads(stdout.strip())
    except (ValueError, RecursionError):  # deeply nested stdout is not a decision id
        return []
    if not isinstance(doc, dict):
        return []
    value = doc.get("decision_id")
    items = value if isinstance(value, list) else [value]
    return [i for i in items if isinstance(i, str) and DECISION_ID_RE.fullmatch(i)]


def run_pre_run(
    specs: Sequence[HookSpec],
    payload: Mapping[str, Any],
    *,
    object_id: str,
    override_escalation: bool = False,
    base_env: Mapping[str, str] | None = None,
    shell: Sequence[str] = SHELL,
) -> PreRunResult:
    """Run the ``pre-run`` entries in order. Exit 0 continues; 13 escalates and the rest still
    run; any other exit, a timeout or a hook that cannot start vetoes at once (later entries do
    not run, and a veto after an escalation is a veto). ``override_escalation`` (the caller has
    checked ``require_human_override``) turns an escalation into a pass, never a veto."""
    validate_payload("pre-run", payload)
    env = hook_env("pre-run", object_id, base=base_env)
    ids: list[str] = []
    escalated = False

    def collect(result: EntryResult) -> tuple[str, ...]:
        for item in _decision_ids(result.stdout):
            if item not in ids and len(ids) < MAX_DECISION_IDS:
                ids.append(item)
        return tuple(ids)

    for spec in specs:
        result = run_spec(spec, "pre-run", payload, env=env, shell=shell)
        collect(result)
        if result.ok:
            continue
        if result.code == "exit" and result.returncode == ESCALATE_EXIT:
            escalated = True
            continue
        return PreRunResult("veto", "pre-run", tuple(ids))
    if not escalated:
        return PreRunResult("continue", None, tuple(ids))
    if override_escalation:
        return PreRunResult("continue", None, tuple(ids), overridden=True)
    return PreRunResult("escalate", "pre-run", tuple(ids))


def pre_run_audit_record(
    caller: Caller,
    result: PreRunResult,
    run_id: str,
    pr: str,
    inputs: Mapping[str, Any],
    **kwargs: Any,
) -> dict[str, Any]:
    """The audit ``run`` record of a vetoed or escalated attempt (no run record exists): result
    ``vetoed`` with exit 11 or ``escalated`` with exit 13, target the reserved ``run_id`` and
    ``pr``. The hook's decision ids are added to the caller's (deduplicated, at most
    ``MAX_DECISION_IDS``). ``inputs`` is the record's ``inputs`` object (``schemas/audit.json``);
    ``kwargs`` go to ``make_audit_record`` (``attempt_id``, ``ts``, ``now``). A ``continue`` outcome
    has no such record (``ValueError``)."""
    if result.outcome == "veto":
        status, exit_code = "vetoed", 11
    elif result.outcome == "escalate":
        status, exit_code = "escalated", ESCALATE_EXIT
    else:
        raise ValueError("only a veto or an escalation has a pre-run audit record")
    ids = list(caller.decision_ids)
    for item in result.decision_ids:
        if item not in ids and len(ids) < MAX_DECISION_IDS:
            ids.append(item)
    return make_audit_record(
        "run",
        caller=dataclasses.replace(caller, decision_ids=tuple(ids[:MAX_DECISION_IDS])),
        run_id=run_id,
        pr=pr,
        inputs=inputs,
        result={"status": status, "reason": None, "exit_code": exit_code},
        **kwargs,
    )


def require_human_override(caller: Caller) -> None:
    """``run --override-escalation`` is human-only: the resulting actor must be ``human:<name>``
    (``--actor human:<name>``, or derived from a TTY when no ``--actor`` is given; a TTY with
    ``--actor auto:<name>`` is refused). Otherwise a usage error (exit 2)."""
    if not caller.actor.startswith("human:"):
        raise ReviewRunError(
            "usage", "--override-escalation needs a human actor (a TTY or --actor human:<name>)"
        )


# -- delivery ------------------------------------------------------------------------------


@dataclass(frozen=True)
class DeliveryResult:
    """``delivered``, ``retry`` (left pending), ``abandoned`` (failed ``MAX_ATTEMPTS`` times),
    ``busy`` (another process holds the event) or ``skipped`` (already delivered)."""

    event_id: str
    status: Literal["delivered", "retry", "abandoned", "busy", "skipped"]


def event_lease_name(event_id: str) -> str:
    return f"event:{event_id}"


def _lease_seconds(specs: Sequence[HookSpec]) -> int:
    total = sum(s.timeout + TERM_GRACE_S for s in specs if not s.is_async)
    return max(MIN_LEASE_SECONDS, math.ceil(total) + LEASE_SLACK_SECONDS)


def deliver_pending(
    store: Store,
    audit: AuditWriter,
    log: LogWriter,
    config_hooks: Mapping[str, Any] | None,
    *,
    run_id: str | None = None,
    base_env: Mapping[str, str] | None = None,
    shell: Sequence[str] = SHELL,
) -> list[DeliveryResult]:
    """One delivery pass over the undelivered events (of ``run_id``, or all), oldest first.

    The audit records of every pending event are appended first, without a bound. If that fails
    the hooks are still delivered and the failure is raised afterwards. Each event is delivered
    under its own lease, so concurrent passes never run the same event at once; an event another
    process finished meanwhile is skipped. ``config_hooks`` is the current config's
    ``hooks`` (resolved values).
    """
    audit_error: Exception | None = None
    try:
        flush_outbox(store, audit, run_id)
    except (OSError, ReviewRunError) as exc:
        audit_error = exc
    results = [
        _deliver_event(
            store, log, config_hooks, str(ev["event_id"]), str(ev["run_id"]), base_env, shell
        )
        for ev in store.pending_events(run_id)
        if not ev["delivered"]
    ]
    if audit_error is not None:
        raise audit_error
    return results


def _deliver_event(
    store: Store,
    log: LogWriter,
    config_hooks: Mapping[str, Any] | None,
    event_id: str,
    run_id: str,
    base_env: Mapping[str, str] | None,
    shell: Sequence[str],
) -> DeliveryResult:
    run = store.get_run(run_id)
    if run is None:
        return DeliveryResult(event_id, "skipped")
    first = next((e for e in store.pending_events(run_id) if e["event_id"] == event_id), None)
    hook = str(first["hook"]) if first else ""
    specs = specs_for(hook, config_hooks, run.hooks) if hook in POST_HOOKS else []
    lease = store.acquire_lease(event_lease_name(event_id), ttl_seconds=_lease_seconds(specs))
    if lease is None:
        return DeliveryResult(event_id, "busy")
    try:
        event = next((e for e in store.pending_events(run_id) if e["event_id"] == event_id), None)
        if event is None or event["delivered"]:
            return DeliveryResult(event_id, "skipped")
        return _run_event(store, log, event, run.hooks, specs, base_env, shell)
    finally:
        store.release_lease(lease)


def _run_event(
    store: Store,
    log: LogWriter,
    event: Mapping[str, Any],
    stored_hooks: Sequence[object],
    specs: Sequence[HookSpec],
    base_env: Mapping[str, str] | None,
    shell: Sequence[str],
) -> DeliveryResult:
    event_id, run_id, hook = str(event["event_id"]), str(event["run_id"]), str(event["hook"])
    attempt = int(event["attempts"]) + 1
    if hook not in POST_HOOKS:
        log.write(
            "hook_skipped", f"event {event_id} names an unknown hook", level="warn", run_id=run_id
        )
        _abandon(store, log, run_id, hook or "unknown", [(0, "unknown_hook")], attempt)
        store.mark_event(event_id, delivered=True)
        return DeliveryResult(event_id, "abandoned")
    payload = event["payload"]
    try:
        validate_payload(hook, payload)
        if payload.get("event_id") != event_id or payload.get("object_id") != run_id:
            raise ValueError("the payload does not belong to its outbox row")
    except ValueError:
        # A payload that does not match its schema never gets better: abandon it at once.
        _abandon(store, log, run_id, hook, [(0, "payload_invalid")], attempt)
        store.mark_event(event_id, delivered=True)
        return DeliveryResult(event_id, "abandoned")
    if not specs:
        store.mark_event(event_id, delivered=True)
        return DeliveryResult(event_id, "delivered")
    store.record_attempt(event_id)
    env = hook_env(hook, run_id, event_id=event_id, base=base_env)
    final = attempt >= MAX_ATTEMPTS
    failures: list[tuple[int, str]] = []
    for index, spec in enumerate(specs):
        if spec.is_async:
            continue
        result = run_spec(spec, hook, payload, env=env, shell=shell)
        if not result.ok:
            failures.append((index, result.code))
            _log_failure(log, run_id, hook, index, result.code, attempt)
    if not failures or final:
        # Detached entries start only on the pass that ends the event, so a retried event does
        # not start them again and again.
        for index, spec in enumerate(specs):
            if spec.is_async:
                result = run_spec(spec, hook, payload, env=env, shell=shell)
                if not result.ok:
                    failures.append((index, result.code))
                    _log_failure(log, run_id, hook, index, result.code, attempt)
    if failures and not final:
        return DeliveryResult(event_id, "retry")
    if failures:
        _abandon(store, log, run_id, hook, failures, attempt)
        store.mark_event(event_id, delivered=True)
        return DeliveryResult(event_id, "abandoned")
    store.mark_event(event_id, delivered=True)
    return DeliveryResult(event_id, "delivered")


def _log_failure(log: LogWriter, run_id: str, hook: str, index: int, code: str, n: int) -> None:
    log.write(
        "hook_failed",
        f"{hook} entry {index} failed ({code}), attempt {n}",
        level="warn",
        run_id=run_id,
        code=code,
        count=n,
    )


def _abandon(
    store: Store,
    log: LogWriter,
    run_id: str,
    hook: str,
    failures: Sequence[tuple[int, str]],
    attempts: int,
) -> None:
    for index, code in failures:
        log.write(
            "hook_failed",
            f"{hook} entry {index} abandoned after {attempts} attempts",
            level="error",
            run_id=run_id,
            code=code,
            count=attempts,
        )
    store.add_warning(run_id, "hook_failed")
