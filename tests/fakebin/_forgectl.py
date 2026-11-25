"""Fake ``forgectl`` (pinned 0.56.0): ``show``, ``files``, ``diff`` and ``ci``.

Grammar and outputs follow forgectl's ``docs/interface.md`` (identifiers, global flags, exit codes,
error envelope) and the pinned ``schemas/cli/{show,files,diff,ci,error}.json``.
Commands the real tool has but the fake does not emulate exit 99.
"""

from __future__ import annotations

import re
from typing import Any

from tests.fakebin._common import (
    Call,
    CommandSpec,
    Grammar,
    Out,
    Parsed,
    Usage,
    apply_step,
    duration_check,
    flag,
    next_step,
    run_tool,
    scenario,
)

TOOL = "forgectl"
HEAD = "a1" * 20
BASE = "b2" * 20
MERGE_BASE = "c3" * 20
NEW_HEAD = "d4" * 20

_GLOBAL = {
    "--json": flag(),
    "--config": flag(takes=True),
    "--verbose": flag(),
    "--quiet": flag(),
    "--timeout": flag(takes=True, check=duration_check),
    "--version": flag(),
    "--help": flag(),
}
_READ = CommandSpec(min_args=1, max_args=1)
GRAMMAR = Grammar(
    tool=TOOL,
    global_flags=_GLOBAL,
    commands={("show",): _READ, ("files",): _READ, ("diff",): _READ, ("ci",): _READ},
    unimplemented=frozenset(
        (c,)
        for c in (
            "prs snapshot snapshots poll compare comments reviews access whoami merge review "
            "comment reply resolve unresolve approve browse export"
        ).split()
    ),
)

_SEGMENT = r"[A-Za-z0-9._-]+"
# Hosts in input are lowercased (interface.md, Identifiers): match any case, normalize below.
_HOST = (
    r"(?i:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*(?::[0-9]{1,5})?)"
)
BUILTIN_HOSTS = ("github.com", "gitlab.com")
_SHORT = re.compile(rf"({_HOST})((?:/{_SEGMENT})+)#([1-9][0-9]{{0,9}})")
_GITHUB = re.compile(
    rf"https://({_HOST})/({_SEGMENT})/({_SEGMENT})/pull/([1-9][0-9]{{0,9}})"
    r"(?:/files|/commits(?:/[0-9a-f]{7,40})?|/checks|\.diff|\.patch)?/?(?:[?#][A-Za-z0-9._~=&-]*)?"
)
_GITLAB = re.compile(
    rf"https://({_HOST})/((?:{_SEGMENT}/)+?)(?:-/)?merge_requests/([1-9][0-9]{{0,9}})"
    r"(?:/diffs|/commits|/pipelines)?/?(?:[?#][A-Za-z0-9._~=&-]*)?"
)
MAX_PR_LEN = 2048


def parse_pr(text: str, hosts: list[str]) -> tuple[str, str, int, str]:
    """Return ``(host, path, number, backend)`` or raise ``Usage(invalid_pr)``.

    Matches one anchored ASCII pattern over the whole input, no URL library, as the real tool does.
    A well-formed reference on an unconfigured host is the caller's ``unknown_host`` (exit 20).
    """
    bad = Usage("invalid pull request reference", code="invalid_pr")
    if not text.isascii() or len(text) > MAX_PR_LEN or any(c in text for c in "@\\% \t\n"):
        raise bad
    host = path = number = None
    if m := _SHORT.fullmatch(text):
        host, path, number = m.group(1), m.group(2).lstrip("/"), m.group(3)
    elif m := _GITHUB.fullmatch(text):
        host, path, number = m.group(1), f"{m.group(2)}/{m.group(3)}", m.group(4)
    elif m := _GITLAB.fullmatch(text):
        host, path, number = m.group(1), m.group(2).strip("/"), m.group(3)
    if host is None or path is None or number is None:
        raise bad
    # ``:443`` is normalized away; a leading ``www.`` is stripped only for the built-in hosts.
    host = host.lower().removesuffix(":443")
    if host.startswith("www.") and host.removeprefix("www.") in BUILTIN_HOSTS:
        host = host.removeprefix("www.")
    segments = path.split("/")
    backend = "gitlab" if host.startswith("gitlab") else "github"
    # No owner or GitLab segment starts with "-"; a GitHub repo name (last segment) may.
    checked = segments[:-1] if backend == "github" else segments
    if any(s in (".", "..") for s in segments) or any(s.startswith("-") for s in checked):
        raise bad
    if backend == "github" and len(segments) != 2:
        raise bad
    return host, path, int(number), backend


def default_pr(host: str, path: str, number: int, backend: str) -> dict[str, Any]:
    pr = f"{host}/{path}#{number}"
    return {
        "pr": pr,
        "repo": f"{host}/{path}",
        "repo_id": "R_kgDOFake0001" if backend == "github" else "gid://gitlab/Project/1",
        "pr_id": "PR_kwDOFake0001" if backend == "github" else "gid://gitlab/MergeRequest/1",
        "number": number,
        "url": f"https://{host}/{path}/pull/{number}",
        "backend": backend,
        "title": "Fake pull request",
        "body": "Fake body.",
        "author": {"login": "fake-author", "id": "1001", "is_bot": False},
        "state": "open",
        "draft": False,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-02T00:00:00Z",
        "closed_at": None,
        "merged_at": None,
        "merged_by": None,
        "labels": [],
        "author_association": "CONTRIBUTOR",
        "head": {"sha": HEAD, "ref": "feature", "repo": f"{host}/{path}", "is_fork": False},
        "base": {"ref": "main", "sha": BASE, "is_default": True},
        "merged_head_sha": None,
        "merge_commit_sha": None,
        "head_fetch_ref": f"refs/pull/{number}/head",
        "mergeable": True,
        "blocked_by": [],
        "auto_merge": {"enabled": False, "by": None},
        "merge_queue": False,
        "merge_methods_allowed": ["merge", "squash", "rebase"],
        "additions": 1,
        "deletions": 1,
        "changed_files": 1,
        "review_decision": "none",
        "approvals": 0,
        "approvals_at_head": 0,
        "approvals_left": None,
    }


def default_files(pr: dict[str, Any]) -> dict[str, Any]:
    return {
        "pr": pr["pr"],
        "head_sha": pr["head"]["sha"],
        "files": [
            {
                "path": "src/app.py",
                "previous_path": None,
                "status": "modified",
                "additions": 1,
                "deletions": 1,
            }
        ],
        "files_complete": True,
        "changed_files": 1,
    }


DIFF_TEXT = (
    "diff --git a/src/app.py b/src/app.py\n"
    "index 1111111..2222222 100644\n"
    "--- a/src/app.py\n"
    "+++ b/src/app.py\n"
    "@@ -1,3 +1,3 @@\n"
    " import os\n"
    "-print('old')\n"
    "+print('new')\n"
    " \n"
)


def default_diff(pr: dict[str, Any]) -> dict[str, Any]:
    return {
        "pr": pr["pr"],
        "head_sha": pr["head"]["sha"],
        "base_sha": pr["base"]["sha"],
        "merge_base_sha": MERGE_BASE,
        "diff": DIFF_TEXT,
    }


def default_ci(pr: dict[str, Any]) -> dict[str, Any]:
    return {
        "pr": pr["pr"],
        "head_sha": pr["head"]["sha"],
        "ci": {
            "state": "passing",
            "head_sha": pr["head"]["sha"],
            "for_current_head": True,
            "complete": True,
            "checks": [
                {
                    "name": "build",
                    "state": "passing",
                    "required": True,
                    "counted": True,
                    "url": "https://ci.example.com/build/1",
                    "finished_at": "2026-01-02T00:00:00Z",
                }
            ],
        },
    }


def handler(call: Call, parsed: Parsed) -> Out:
    command = parsed.command[0]
    cfg = scenario(TOOL)
    hosts: list[str] = cfg.get("hosts", ["github.com", "gitlab.com"])
    if parsed.has("--verbose") and parsed.has("--quiet"):
        raise Usage("--verbose and --quiet are mutually exclusive")
    host, path, number, backend = parse_pr(parsed.args[0], hosts)
    step = next_step(TOOL, command)
    if host not in hosts:
        step = {
            "error": {
                "code": "refused",
                "message": "unknown host",
                "reason": "unknown_host",
                "details": None,
            },
            "exit": 20,
        }
    pr = default_pr(host, path, number, backend)
    if "pr" in cfg:  # a scenario-wide PR object, merged into the default
        from tests.fakebin._common import deep_merge

        pr = deep_merge(pr, cfg["pr"])
        pr.update(pr=f"{host}/{path}#{number}", repo=f"{host}/{path}", number=number)

    def default() -> Out:
        if not parsed.has("--json"):
            return Out(text=f"{pr['pr']} fake human output (use --json)\n")
        builders = {"show": lambda p: p, "files": default_files, "diff": default_diff,
                    "ci": default_ci}  # fmt: skip
        return Out(doc=builders[command](pr))

    return apply_step(TOOL, step, default, call, schema=f"{TOOL}/cli/{command}.json")


def main(argv: list[str]) -> None:
    run_tool(TOOL, GRAMMAR, handler, argv)
