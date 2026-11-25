"""Fake ``wtctl`` (pinned 1.2.0): ``create --throwaway``, ``remove`` and ``list --throwaways``.

Throwaway records live in the shared ``state.json`` and a real (empty) directory is made for each
under ``$FAKEBIN_DIR/throwaways/<id>``, so a caller can use it as a cwd; ``remove`` deletes exactly
that directory. Grammar, flags and exit codes follow wtctl's ``docs/interface.md`` (create, list,
remove, global flags, exit codes) and the pinned ``schemas/cli/{create,remove,list,error}.json``.
Worktree (non-throwaway) creation, ``status``, ``show`` and the rest exit 99.
"""

from __future__ import annotations

import calendar
import os
import re
import shutil
import time
from typing import Any

from tests.fakebin._common import (
    ACTOR,
    Call,
    CommandSpec,
    Grammar,
    Out,
    Parsed,
    State,
    Usage,
    actor_check,
    apply_step,
    base32_id,
    decision_check,
    error_out,
    fake_dir,
    flag,
    iso,
    label_check,
    next_step,
    run_tool,
    sha_check,
)

TOOL = "wtctl"
_REPO = r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*(?::[0-9]{1,5})?(?:/[A-Za-z0-9._-]+)+$"  # noqa: E501

_GLOBAL = {
    "--json": flag(),
    "--config": flag(takes=True),
    "--verbose": flag(),
    "--quiet": flag(),
    "--dry-run": flag(),
    "--yes": flag(),
    "--actor": flag(takes=True, check=actor_check),
    "--decision-id": flag(takes=True, repeat=True, check=decision_check),
    "--version": flag(),
    "--help": flag(),
}


def _age_check(value: str) -> str | None:
    """wtctl's DURATION: ``^[1-9][0-9]*[smhdw]$`` (no fractions, no milliseconds)."""
    return None if re.fullmatch(r"[1-9][0-9]{0,8}[smhdw]", value) else "is not a duration"


def _depth_check(value: str) -> str | None:
    """``--depth`` is an integer >= 0 (``0`` is the full history)."""
    return None if re.fullmatch(r"[0-9]{1,9}", value) else "is not an integer >= 0"


def _age_seconds(value: str) -> int:
    return int(value[:-1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[value[-1]]


def _repo_check(value: str) -> str | None:
    return None if re.fullmatch(_REPO[1:-1], value) and "/../" not in value else "is not a repo id"


_CREATE = CommandSpec(
    flags={
        "--throwaway": flag(),
        "--repo": flag(takes=True, check=_repo_check),
        "--ref": flag(takes=True, check=sha_check),
        "--source": flag(takes=True, choices=("auto", "local", "remote")),
        "--depth": flag(takes=True, check=_depth_check),
        "--label": flag(takes=True, repeat=True, check=label_check),
        "--hook": flag(takes=True, repeat=True),
        "--override-escalation": flag(),
        # the worktree form's flags: accepted by the grammar, refused with --throwaway below
        "--branch": flag(takes=True),
        "--detach": flag(),
        "--base": flag(takes=True),
        "--name": flag(takes=True),
        "--holder": flag(takes=True),
        "--no-bootstrap": flag(),
        "--copy-env": flag(),
        "--sparse": flag(takes=True, repeat=True),
    }
)
_REMOVE = CommandSpec(
    flags={
        "--merged-head": flag(takes=True, check=sha_check),
        "--keep-branch": flag(),
        "--holder": flag(takes=True),
        "--force": flag(),
        "--hook": flag(takes=True, repeat=True),
        "--override-escalation": flag(),
    },
    min_args=1,
    max_args=1,
)
_LIST = CommandSpec(
    flags={
        "--repo": flag(takes=True, check=_repo_check),
        "--label": flag(takes=True, repeat=True, check=label_check),
        "--throwaways": flag(),
        "--worktrees": flag(),
        "--older-than": flag(takes=True, check=_age_check),
        "--state": flag(
            takes=True,
            repeat=True,
            choices=(
                "creating",
                "active",
                "unlinked",
                "missing",
                "clone_missing",
                "partial",
                "gone",
                "removed",
                "forgotten",
            ),  # fmt: skip
        ),
        "--all": flag(),
    }
)
GRAMMAR = Grammar(
    tool=TOOL,
    global_flags=_GLOBAL,
    commands={("create",): _CREATE, ("remove",): _REMOVE, ("list",): _LIST},
    unimplemented=frozenset(
        (c,)
        for c in (
            "show status browse bootstrap extract adopt clone forget locate prune reconcile record "
            "validate label baseline cron export"
        ).split()
    ),
    mutually_exclusive=(("--throwaways", "--worktrees"), ("--verbose", "--quiet")),
    duplicates="last_wins",  # plain argparse
)

_TOMBSTONES = ("gone", "removed", "forgotten")


def throwaway_root() -> str:
    return str(fake_dir() / "throwaways")


def new_record(
    counter: int, repo: str, ref: str, source: str, depth: int, labels: list[str]
) -> dict[str, Any]:
    rid = base32_id("wt-", f"throwaway-{counter}")
    path = f"{throwaway_root()}/{rid}"
    now = iso()
    return {
        "id": rid,
        "object_type": "throwaway",
        "owned": None,
        "state": "active",
        "repo": repo,
        "clone_path": None,
        "common_dir": None,
        "admin_dir": None,
        "common_id": None,
        "dir_id": f"1:{1000 + counter}",
        "base_dir": throwaway_root(),
        "root_id": "1:1",
        "path": path,
        "name": None,
        "branch": None,
        "created_branch": None,
        "base": None,
        "base_sha": None,
        "head_at_create": None,
        "ref": ref,
        "source": "remote" if source == "auto" else source,
        "depth": depth,
        "fetch_url": f"https://{repo}.git",
        "remote": None,
        "labels": labels,
        "holders": [],
        "sparse": None,
        "baseline_digest": None,
        "bootstrap": None,
        "created": now,
        "updated": now,
        "missing_since": None,
        "removed_at": None,
        "last_error": None,
    }


def _human_actor(parsed: Parsed) -> bool:
    actor = parsed.get("--actor")
    return actor is not None and actor.startswith("human:") and bool(ACTOR.fullmatch(actor))


def _create(call: Call, parsed: Parsed) -> Out:
    if not parsed.has("--throwaway"):
        if not parsed.has("--repo"):
            raise Usage("--repo is required")
        from tests.fakebin._common import Unimplemented

        raise Unimplemented("create (worktree form)")
    for name in ("--branch", "--detach", "--base", "--name", "--holder", "--no-bootstrap",
                 "--copy-env", "--sparse"):  # fmt: skip
        if parsed.has(name):
            raise Usage(f"{name} cannot be used with --throwaway")
    for name in ("--repo", "--ref"):
        if not parsed.has(name):
            raise Usage(f"{name} is required with --throwaway")
    if parsed.has("--override-escalation") and not _human_actor(parsed):
        raise Usage("--override-escalation is human-only")
    repo = parsed.get("--repo") or ""
    ref = parsed.get("--ref") or ""
    step = next_step(TOOL, "create")
    failing = step is not None and ("error" in step or "hang" in step or "stdout_raw" in step)

    def default() -> Out:
        with State() as state:
            records = state.setdefault("wtctl", {"records": {}, "n": 0})
            records["n"] += 1
            rec = new_record(records["n"], repo, ref, parsed.get("--source", "auto") or "auto",
                             int(parsed.get("--depth", "1") or 1),
                             parsed.all("--label"))  # fmt: skip
            records["records"][rec["id"]] = rec
        os.makedirs(rec["path"], mode=0o700, exist_ok=True)
        return Out(
            doc={
                "record": rec,
                "branch_action": None,
                "bootstrap": None,
                "reused": False,
                "warnings": [],
                "dry_run": parsed.has("--dry-run"),
            }
        )

    if parsed.has("--dry-run") and not failing:
        out = default_dry(parsed, repo, ref)
        return apply_step(TOOL, None, lambda: out, call, schema=f"{TOOL}/cli/create.json")
    return apply_step(TOOL, step, default, call, schema=f"{TOOL}/cli/create.json")


def default_dry(parsed: Parsed, repo: str, ref: str) -> Out:
    rec = new_record(0, repo, ref, parsed.get("--source", "auto") or "auto",
                     int(parsed.get("--depth", "1") or 1), parsed.all("--label"))  # fmt: skip
    return Out(
        doc={
            "record": rec,
            "branch_action": None,
            "bootstrap": None,
            "reused": False,
            "warnings": [],
            "dry_run": True,
        }  # fmt: skip
    )


def _remove(call: Call, parsed: Parsed) -> Out:
    for name in ("--force", "--override-escalation"):
        if parsed.has(name) and not _human_actor(parsed):
            raise Usage(f"{name} is human-only: pass --actor human:<user>")
    target = parsed.args[0]
    step = next_step(TOOL, "remove")

    def default() -> Out:
        with State() as state:
            records = state.setdefault("wtctl", {"records": {}, "n": 0})["records"]
            rec = records.get(target) or next(
                (r for r in records.values() if r["path"] == target), None
            )
            if rec is not None and rec["state"] in _TOMBSTONES and rec["path"] == target:
                rec = None  # only a full id finds a tombstone, never its path
            if rec is None:
                return error_out(TOOL, 10, {"code": "not_found", "message": "no such record"})
            if rec["state"] in _TOMBSTONES:
                return error_out(
                    TOOL,
                    10,
                    {"code": "not_found", "message": f"the record is {rec['state']}",
                     "details": {"state": rec["state"], "removed_at": rec["removed_at"]}},
                )  # fmt: skip
            if not parsed.has("--dry-run") and any(
                parsed.has(f) for f in ("--force", "--merged-head", "--keep-branch", "--holder")
            ):
                raise Usage("--merged-head, --holder, --force and --keep-branch are for worktrees")
            if not parsed.has("--dry-run"):
                rec["state"] = "removed"
                rec["removed_at"] = iso()
                rec["updated"] = rec["removed_at"]
                shutil.rmtree(rec["path"], ignore_errors=True)
            doc = {
                "record": rec,
                "checks": {"in_use": {"status": "pass", "details": []}},
                "overridden": [],
                "branch_deleted": False,
                "branch_kept_reason": None,
                "warnings": [],
            }
        if parsed.has("--dry-run"):
            doc = {
                "id": rec["id"], "object_type": "throwaway", "would_remove": True, "reason": None,
                "human_required": False, "checks": doc["checks"], "force_overrides": [],
                "head": None, "current_branch": None, "warnings": [], "dry_run": True,
            }  # fmt: skip
        return Out(doc=doc)

    return apply_step(TOOL, step, default, call, schema=f"{TOOL}/cli/remove.json")


def _list(call: Call, parsed: Parsed) -> Out:
    step = next_step(TOOL, "list")

    def default() -> Out:
        with State() as state:
            records = list(state.get("wtctl", {"records": {}})["records"].values())
        wanted = set(parsed.all("--state"))
        want_labels = parsed.all("--label")
        older = (
            _age_seconds(parsed.get("--older-than") or "") if parsed.has("--older-than") else None
        )
        kept = []
        for rec in records:
            if parsed.has("--throwaways") and rec["object_type"] != "throwaway":
                continue
            if parsed.has("--worktrees") and rec["object_type"] != "worktree":
                continue
            if parsed.has("--repo") and rec["repo"] != parsed.get("--repo"):
                continue
            if wanted and rec["state"] not in wanted:
                continue
            if not wanted and rec["state"] in _TOMBSTONES:
                continue
            if any(lab not in rec["labels"] for lab in want_labels):
                continue
            if (
                older is not None
                and calendar.timegm(time.strptime(rec["created"], "%Y-%m-%dT%H:%M:%SZ"))
                > time.time() - older
            ):
                continue  # ``--older-than`` compares ``created``
            kept.append(rec)
        kept.sort(key=lambda r: (r["created"], r["id"]), reverse=True)
        return Out(doc={"records": kept, "foreign": []})

    return apply_step(TOOL, step, default, call, schema=f"{TOOL}/cli/list.json")


def handler(call: Call, parsed: Parsed) -> Out:
    return {"create": _create, "remove": _remove, "list": _list}[parsed.command[0]](call, parsed)


def main(argv: list[str]) -> None:
    run_tool(TOOL, GRAMMAR, handler, argv)
