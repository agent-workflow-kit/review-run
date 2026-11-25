"""Pre-fetch: the resolve step, the anchor, head consistency and failure mapping (issue #12).

Two entry points, matching the two places the run order reads the PR (interface.md, Run order
steps 2 and 5, Pre-fetch, Resolve step failures):

* ``resolve_pr`` runs ``forgectl show`` before any run record exists and returns the PR facts;
  its failures are ``ReviewRunError`` envelopes (exits 2, 10, 12 and 20).
* ``prefetch`` runs, once the run record exists, ``pr-comment draft new`` (the anchor) and
  ``forgectl files``, ``diff`` and ``ci``, each compared with the anchor head, retrying the whole
  sequence once on a mismatch. Its failures are ``PrefetchFailure`` (the run ends ``failed`` with
  reason ``prefetch_failed``, ``stale_head`` or ``too_large``).

Everything a sibling prints is untrusted. Sibling error text is never copied anywhere: only
closed words (``^[a-z][a-z0-9_]{0,63}$``: a code or a reason) and one integer survive. Output
that is not strict JSON of the expected shape is a failure, never a crash. All processes run
through the gateway; the PR argument is passed to forgectl only as the interface specifies and
everything after the resolve step uses the canonical ``pr`` forgectl returned.

The returned text fields (title, body, labels, paths, the diff, check names) are raw and
untrusted; the prompt and manifest builders (#13, #14) sanitize them where they surface.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from review_run import gateway
from review_run.errors import ReviewRunError
from review_run.fsmodes import write_private_file
from review_run.store import Expect, Store

#: Seconds allowed to each forgectl and pr-comment read (interface.md, Finalize recovery).
CALL_TIMEOUT = 60.0
MAX_PR_ARG = 2048
_MAX_RETRY_AFTER = 2**31 - 1

# Reasons of a failed pre-fetch (the Reasons table).
PREFETCH_FAILED = "prefetch_failed"
STALE_HEAD = "stale_head"
TOO_LARGE = "too_large"

_WORD = re.compile(r"[a-z][a-z0-9_]{0,63}")
_SHA = re.compile(r"[0-9a-f]{40}")
_DRAFT_ID = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")
_HOST = r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*(?::[0-9]{1,5})?"
_REPO = re.compile(rf"{_HOST}(?:/(?!\.\.?(?:/|$))[A-Za-z0-9._-]+)+")
_PR = re.compile(rf"{_HOST}(?:/(?!\.\.?(?:/|#))[A-Za-z0-9._-]+)+#[1-9][0-9]{{0,9}}")
_PR_ARG = re.compile(r"https://[!-~]+|[!-~]+/[!-~]+#[1-9][0-9]{0,9}")
_STATUSES = frozenset({"added", "modified", "deleted", "renamed"})
_CI_STATES = frozenset({"passing", "failing", "pending", "none"})
_CHECK_STATES = frozenset({"passing", "failing", "pending", "skipped"})
_RETRYABLE_CODES = frozenset({"rate_limited", "network_error"})


# --------------------------------------------------------------------------------------------
# results
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PrFacts:
    """What ``forgectl show`` says (untrusted text fields; canonical ``pr`` and ``repo``)."""

    pr: str
    repo: str
    number: int
    state: str
    head_sha: str
    head_ref: str
    base_ref: str
    title: str
    body: str | None
    author_login: str | None
    labels: tuple[str, ...]
    draft: bool
    is_bot: bool | None
    is_fork: bool | None
    base_is_default: bool | None

    def run_facts(self) -> dict[str, bool | None]:
        """The four facts stored in ``pr.json`` and ``meta.review_run.pr``."""
        return {
            "draft": self.draft,
            "is_bot": self.is_bot,
            "is_fork": self.is_fork,
            "base_is_default": self.base_is_default,
        }


@dataclass(frozen=True)
class Anchor:
    """The head, base ref and shas the draft (and so the review) is anchored to."""

    head_sha: str
    base_ref: str
    base_sha: str | None
    merge_base_sha: str | None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "head_sha": self.head_sha,
            "base_ref": self.base_ref,
            "base_sha": self.base_sha,
            "merge_base_sha": self.merge_base_sha,
        }


@dataclass(frozen=True)
class FileEntry:
    path: str
    previous_path: str | None
    status: str
    additions: int | None
    deletions: int | None


@dataclass(frozen=True)
class CiCheck:
    name: str
    state: str


@dataclass(frozen=True)
class Ci:
    state: str
    complete: bool
    for_current_head: bool
    checks: tuple[CiCheck, ...]


@dataclass(frozen=True)
class Prefetch:
    """The consistent read of one PR at ``anchor.head_sha``: every read below was checked against
    it. ``diff`` is the whole unified diff (``files_complete`` was true), raw and untrusted."""

    pr: str
    draft_id: str
    anchor: Anchor
    files: tuple[FileEntry, ...]
    diff: str
    ci: Ci
    attempts: int


@dataclass(frozen=True)
class PreviousReview:
    """A done run's stored ``review.json``, re-verified against its recorded sha256."""

    run_id: str
    sha256: str
    review: dict[str, Any]


class PrefetchFailure(Exception):
    """The pre-fetch failed after the run record existed; the run ends ``failed``.

    ``reason`` is ``prefetch_failed``, ``stale_head`` or ``too_large`` (exit 12 for all three).
    The text is trusted: closed words only.
    """

    exit_code = 12

    def __init__(
        self,
        reason: str,
        *,
        tool: str | None = None,
        lower_code: str | None = None,
        lower_reason: str | None = None,
        retryable: bool = False,
        retry_after_s: int | None = None,
    ) -> None:
        super().__init__(f"pre-fetch failed: {reason}" + (f" ({lower_code})" if lower_code else ""))
        self.reason = reason
        self.tool = tool
        self.lower_code = lower_code
        self.lower_reason = lower_reason
        self.retryable = retryable
        self.retry_after_s = retry_after_s

    def failure(self) -> dict[str, Any]:
        """The run's ``failure`` object (``schemas/defs.json`` ``failure``)."""
        return {
            "tool": self.tool,
            "lower_code": self.lower_code,
            "lower_reason": self.lower_reason,
            "retryable": self.retryable,
            "retry_after_s": self.retry_after_s,
            "errors": [],
        }


# --------------------------------------------------------------------------------------------
# calling a sibling and classifying its answer
# --------------------------------------------------------------------------------------------


class LowerFailure(Exception):
    """A sibling call failed. ``exit_code`` is ``None`` for a failure the gateway synthesized.

    ``code`` and ``reason`` are closed words or ``None``; the text of a sibling's message is never
    kept.
    """

    def __init__(
        self,
        tool: str,
        exit_code: int | None,
        code: str,
        reason: str | None = None,
        retry_after_s: int | None = None,
    ) -> None:
        super().__init__(f"{tool} failed ({code})")
        self.tool = tool
        self.exit_code = exit_code
        self.code = code
        self.reason = reason
        self.retry_after_s = retry_after_s

    @property
    def lower_code(self) -> str:
        """A refusal (exit 20) is named by its reason, as agent-spawn's ``lower_refused`` is."""
        return self.reason if self.exit_code == 20 and self.reason else self.code

    @property
    def retryable(self) -> bool:
        return self.lower_code in _RETRYABLE_CODES


def _word(value: object) -> str | None:
    return value if isinstance(value, str) and _WORD.fullmatch(value) else None


def _retry_after(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, int):  # may be beyond float range: never convert it
        return None if value < 0 else min(value, _MAX_RETRY_AFTER)
    try:
        if not math.isfinite(value) or value < 0:
            return None
        return min(math.ceil(value), _MAX_RETRY_AFTER)
    except OverflowError:
        return None


def _reject_constant(_name: str) -> Any:
    raise ValueError("non-finite number")


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate key")
        out[key] = value
    return out


def strict_json(text: str) -> Any:
    """Parse one JSON document, refusing duplicate keys and non-finite numbers.

    Raises ``ValueError`` for anything unparseable, including deep nesting and huge integers.
    """
    try:
        return json.loads(
            text, parse_constant=_reject_constant, object_pairs_hook=_reject_duplicates
        )
    except RecursionError:
        raise ValueError("nesting too deep") from None


def _envelope(stdout: str) -> tuple[str | None, str | None, int | None]:
    """``(code, reason, retry_after_s)`` of an error envelope, each closed or ``None``."""
    try:
        doc = strict_json(stdout)
    except ValueError:
        return None, None, None
    error = doc.get("error") if isinstance(doc, dict) else None
    if not isinstance(error, dict):
        return None, None, None
    return (
        _word(error.get("code")),
        _word(error.get("reason")),
        _retry_after(error.get("retry_after")),
    )


def call_json(tool: str, args: list[str], *, timeout: float = CALL_TIMEOUT) -> Any:
    """Run ``<tool> <args>`` through the gateway and return its parsed stdout (exit 0 only).

    Raises ``LowerFailure`` for a gateway failure, a non-zero exit (with the envelope's closed
    code and reason when it printed one) and an exit 0 whose stdout is not strict JSON
    (``invalid_output``).
    """
    try:
        done = gateway.run([tool, *args], timeout=timeout)
    except gateway.GatewayError as exc:
        raise LowerFailure(tool, None, exc.lower_code) from None
    if done.returncode != 0:
        code, reason, retry_after = _envelope(done.stdout)
        raise LowerFailure(tool, done.returncode, code or "failed", reason, retry_after)
    try:
        return strict_json(done.stdout)
    except ValueError:
        raise LowerFailure(tool, 0, "invalid_output") from None


# --------------------------------------------------------------------------------------------
# strict readers of the sibling documents (extra keys are ignored, used keys are checked)
# --------------------------------------------------------------------------------------------


class _Bad(Exception):
    """A document did not have the expected shape."""


def _need(cond: bool) -> None:
    if not cond:
        raise _Bad


def _obj(value: object) -> dict[str, Any]:
    _need(isinstance(value, dict))
    assert isinstance(value, dict)
    return value


def _str(value: object, *, null: bool = False) -> Any:
    if value is None and null:
        return None
    _need(isinstance(value, str))
    return value


def _flag(value: object, *, null: bool = False) -> Any:
    if value is None and null:
        return None
    _need(isinstance(value, bool))
    return value


def _int(value: object, *, null: bool = False, minimum: int = 0) -> Any:
    if value is None and null:
        return None
    _need(isinstance(value, int) and not isinstance(value, bool) and value >= minimum)
    return value


def _sha(value: object, *, null: bool = False) -> Any:
    if value is None and null:
        return None
    _need(isinstance(value, str) and bool(_SHA.fullmatch(value)))
    return value


def _line(value: object) -> str:
    """A non-empty, one-line, printable string (a path or a ref)."""
    _need(isinstance(value, str) and 0 < len(value) <= 4096)
    assert isinstance(value, str)
    _need(all(unicodedata.category(c)[0] not in "CZ" or c == " " for c in value))
    return value


def _ref(value: object, *, limit: int = 1024) -> str:
    """A branch name: forgectl's grammar excludes controls and whitespace (and caps at 1024;
    ``anchor.base_ref`` is capped at 255 by the schema)."""
    _need(isinstance(value, str) and 0 < len(value) <= limit)
    assert isinstance(value, str)
    _need(all(unicodedata.category(c)[0] not in "CZ" for c in value))
    return value


def _parse_show(doc: object) -> PrFacts:
    d = _obj(doc)
    pr, repo = _str(d.get("pr")), _str(d.get("repo"))
    _need(bool(_PR.fullmatch(pr)) and bool(_REPO.fullmatch(repo)))
    number = _int(d.get("number"), minimum=1)
    _need(pr == f"{repo}#{number}")
    state = d.get("state")
    _need(state in ("open", "closed", "merged"))
    author, head, base = _obj(d.get("author")), _obj(d.get("head")), _obj(d.get("base"))
    labels = d.get("labels")
    _need(isinstance(labels, list) and all(isinstance(x, str) for x in labels))
    assert isinstance(labels, list)
    return PrFacts(
        pr=pr,
        repo=repo,
        number=number,
        state=str(state),
        head_sha=_sha(head.get("sha")),
        head_ref=_ref(head.get("ref")),
        base_ref=_ref(base.get("ref")),
        title=_str(d.get("title")),
        body=_str(d.get("body"), null=True),
        author_login=_str(author.get("login"), null=True),
        labels=tuple(labels),
        draft=_flag(d.get("draft")),
        is_bot=_flag(author.get("is_bot"), null=True),
        is_fork=_flag(head.get("is_fork"), null=True),
        base_is_default=_flag(base.get("is_default"), null=True),
    )


def _parse_draft_new(doc: object, pr: str) -> tuple[str, Anchor]:
    d = _obj(doc)
    _need(d.get("schema_version") == 1 and not isinstance(d.get("schema_version"), bool))
    draft_id = _str(d.get("draft_id"))
    _need(bool(_DRAFT_ID.fullmatch(draft_id)))
    _str(d.get("pr"))
    if d["pr"] != pr:
        raise LowerFailure("pr-comment", 0, "draft_pr_mismatch")
    a = _obj(d.get("anchor"))
    _need(all(k in a for k in ("head_sha", "base_ref", "base_sha", "merge_base_sha")))
    anchor = Anchor(
        head_sha=_sha(a.get("head_sha")),
        base_ref=_ref(a.get("base_ref"), limit=255),
        base_sha=_sha(a.get("base_sha"), null=True),
        merge_base_sha=_sha(a.get("merge_base_sha"), null=True),
    )
    return draft_id, anchor


def _parse_head(doc: object, pr: str) -> tuple[dict[str, Any], str]:
    d = _obj(doc)
    _need(d.get("pr") == pr)
    return d, _sha(d.get("head_sha"))


def _parse_files(d: dict[str, Any]) -> tuple[tuple[FileEntry, ...], bool]:
    items = d.get("files")
    _need(isinstance(items, list))
    assert isinstance(items, list)
    files = []
    for raw in items:
        f = _obj(raw)
        status = f.get("status")
        _need(status in _STATUSES)
        files.append(
            FileEntry(
                path=_line(f.get("path")),
                previous_path=None if f.get("previous_path") is None else _line(f["previous_path"]),
                status=str(status),
                additions=_int(f.get("additions"), null=True),
                deletions=_int(f.get("deletions"), null=True),
            )
        )
    _int(d.get("changed_files"), null=True)
    return tuple(files), _flag(d.get("files_complete"))


def _parse_ci(d: dict[str, Any]) -> Ci:
    ci = _obj(d.get("ci"))
    state = ci.get("state")
    _need(state in _CI_STATES)
    checks = ci.get("checks")
    _need(isinstance(checks, list))
    assert isinstance(checks, list)
    parsed = []
    for raw in checks:
        c = _obj(raw)
        _need(c.get("state") in _CHECK_STATES)
        parsed.append(CiCheck(name=_str(c.get("name")), state=str(c["state"])))
    return Ci(
        state=str(state),
        complete=_flag(ci.get("complete")),
        for_current_head=_flag(ci.get("for_current_head")),
        checks=tuple(parsed),
    )


def _read(tool: str, args: list[str], parse: Callable[[Any], Any]) -> Any:
    """``call_json`` then ``parse``; a document of the wrong shape is ``invalid_output``."""
    doc = call_json(tool, args)
    try:
        return parse(doc)
    except _Bad:
        raise LowerFailure(tool, 0, "invalid_output") from None


# --------------------------------------------------------------------------------------------
# the resolve step
# --------------------------------------------------------------------------------------------


def check_pr_argument(arg: object) -> str:
    """The shape check of run order step 1: a URL or ``host/<project path>#N``, one line, ASCII.

    Nothing else is checked here (forgectl parses it); an argument that could be read as an
    option never reaches a process. Raises ``usage`` (exit 2) without echoing the argument.
    """
    if (
        not isinstance(arg, str)
        or not arg.isascii()
        or len(arg) > MAX_PR_ARG
        or arg.startswith("-")
        or not _PR_ARG.fullmatch(arg)
    ):
        raise ReviewRunError("usage", "the pull request argument has an invalid shape")
    return arg


def resolve_error(failure: LowerFailure) -> ReviewRunError:
    """Map a failed ``forgectl show`` to its envelope (interface.md, Resolve step failures)."""
    tool = failure.tool
    if failure.exit_code == 10:
        return ReviewRunError(
            "not_found", "the pull request was not found", tool=tool, lower_code="not_found"
        )
    if failure.exit_code == 20 and failure.reason in ("unknown_host", "not_open"):
        return ReviewRunError(
            "refused",
            f"the pull request was refused: {failure.reason}",
            reason=failure.reason,
            tool=tool,
        )
    if failure.exit_code == 2:
        return ReviewRunError(
            "usage", f"{tool} rejected the pull request argument ({failure.lower_code})", tool=tool,
            lower_code=failure.lower_code,
        )  # fmt: skip
    details = None if failure.retry_after_s is None else {"retry_after": failure.retry_after_s}
    return ReviewRunError(
        "failed",
        f"{tool} failed ({failure.lower_code})",
        reason="lower_failed",
        tool=tool,
        lower_code=failure.lower_code,
        details=details,
    )


def resolve_pr(arg: str) -> PrFacts:
    """Run order step 2, first call: ``forgectl show <pr> --json``.

    Returns the facts of an open PR. A PR whose state is not ``open`` is refused (exit 20,
    ``not_open``, ``details.state``). Raises ``ReviewRunError`` for every failure.
    """
    check_pr_argument(arg)
    try:
        facts: PrFacts = _read("forgectl", ["show", arg, "--json"], _parse_show)
    except LowerFailure as failure:
        raise resolve_error(failure) from None
    if facts.state != "open":
        raise ReviewRunError(
            "refused",
            "the pull request is not open",
            reason="not_open",
            tool="forgectl",
            details={"state": facts.state},
        )
    return facts


# --------------------------------------------------------------------------------------------
# the pre-fetch
# --------------------------------------------------------------------------------------------


def _failure(failure: LowerFailure) -> PrefetchFailure:
    """A failed read after the run exists: ``prefetch_failed`` with the sibling's closed code,
    except the two refusals that have their own reasons."""
    reason = PREFETCH_FAILED
    if failure.exit_code == 20 and failure.reason == "stale_head":
        return PrefetchFailure(STALE_HEAD, tool=failure.tool, lower_code="stale_head",
                               lower_reason="stale_head", retryable=True)  # fmt: skip
    if failure.exit_code == 20 and failure.reason == "too_large":
        return PrefetchFailure(TOO_LARGE, tool=failure.tool, lower_code="too_large",
                               lower_reason="too_large")  # fmt: skip
    return PrefetchFailure(
        reason,
        tool=failure.tool,
        lower_code=failure.lower_code,
        lower_reason=failure.reason if failure.exit_code == 20 else None,
        retryable=failure.retryable,
        retry_after_s=failure.retry_after_s,
    )


class _Moved(Exception):
    """A read reported another head than the anchor: retry the whole pre-fetch."""


def _attempt(
    pr: str, hard_ceiling_bytes: int
) -> tuple[str, Anchor, tuple[FileEntry, ...], str, Ci]:
    draft_id, anchor = _read(
        "pr-comment", ["draft", "new", pr, "--json"], lambda d: _parse_draft_new(d, pr)
    )
    head = anchor.head_sha

    doc, got = _read("forgectl", ["files", pr, "--json"], lambda d: _parse_head(d, pr))
    if got != head:
        raise _Moved
    try:
        files, complete = _parse_files(doc)
    except _Bad:
        raise LowerFailure("forgectl", 0, "invalid_output") from None
    if not complete:
        raise PrefetchFailure(TOO_LARGE, tool="forgectl", lower_code="files_incomplete")

    try:
        doc, got = _read("forgectl", ["diff", pr, "--json"], lambda d: _parse_head(d, pr))
    except LowerFailure as failure:
        if failure.exit_code is None and failure.code == "output_too_large":
            # the gateway's output cap equals the default hard ceiling: that is "too large"
            raise PrefetchFailure(TOO_LARGE, tool="forgectl", lower_code="hard_ceiling") from None
        raise
    if got != head:
        raise _Moved
    diff = doc.get("diff")
    if not isinstance(diff, str):
        raise LowerFailure("forgectl", 0, "invalid_output")
    try:
        size = len(diff.encode("utf-8", errors="surrogatepass"))
    except UnicodeEncodeError:  # defensive; the offending text is never put in a message
        raise LowerFailure("forgectl", 0, "invalid_output") from None
    if size > hard_ceiling_bytes:
        raise PrefetchFailure(TOO_LARGE, tool="forgectl", lower_code="hard_ceiling")

    doc, got = _read("forgectl", ["ci", pr, "--json"], lambda d: _parse_head(d, pr))
    if got != head:
        raise _Moved
    try:
        ci = _parse_ci(doc)
    except _Bad:
        raise LowerFailure("forgectl", 0, "invalid_output") from None
    return draft_id, anchor, files, diff, ci


def prefetch(pr: str, *, hard_ceiling_bytes: int) -> Prefetch:
    """Run order step 5: the anchor and the consistent reads of the PR ``pr`` (the canonical id
    from ``resolve_pr``).

    The anchor comes from ``pr-comment draft new`` (a null ``merge_base_sha`` is accepted); the
    files, the diff and the CI are each checked against its head. A head that differs retries the
    whole sequence once, from ``draft new``; a second difference raises ``stale_head``. Nothing
    is returned or stored unless every read of one attempt agreed. ``files_complete: false``,
    a ``too_large`` refusal of ``diff`` and a diff above ``hard_ceiling_bytes`` raise
    ``too_large`` at once; every other failure is ``prefetch_failed``.

    TODO(#13): the manifest also fails ``too_large`` for more than 9999 files and
    ``prefetch_failed`` (``diff_files_mismatch``) when the diff and the file list disagree.
    """
    if not isinstance(pr, str) or not _PR.fullmatch(pr):
        raise ValueError("pr must be a canonical host/path#N")
    for attempt in (1, 2):
        try:
            draft_id, anchor, files, diff, ci = _attempt(pr, hard_ceiling_bytes)
        except _Moved:
            continue
        except LowerFailure as failure:
            raise _failure(failure) from None
        return Prefetch(pr, draft_id, anchor, files, diff, ci, attempt)
    raise PrefetchFailure(STALE_HEAD, lower_code="head_moved", retryable=True)


def persist_prefetch(
    store: Store, run_id: str, expect: Expect, facts: PrFacts, result: Prefetch
) -> bool:
    """Persist what finalize needs: ``pr.json`` (the four PR facts) in the run directory, then
    ``draft_id`` and ``anchor`` on the run's row in one compare-and-set on ``expect``.

    ``False`` when the compare-and-set on ``expect`` (state, phase and awaited run) did not match;
    nothing but the harmless ``pr.json`` is left then. A cancellation request is not part of that
    key; it is the orchestrator's concern. The row is the commit point.
    """
    # TODO(#16): check cancel_requested_at around persist_prefetch
    run_dir = store.run_dir(run_id)
    data = json.dumps(facts.run_facts(), sort_keys=True).encode("ascii") + b"\n"
    write_private_file(run_dir / "pr.json", data)
    return store.update_fields(
        run_id, expect, draft_id=result.draft_id, anchor=result.anchor.to_dict()
    )


# --------------------------------------------------------------------------------------------
# --previous
# --------------------------------------------------------------------------------------------


def _previous_failure(code: str) -> PrefetchFailure:
    return PrefetchFailure(PREFETCH_FAILED, lower_code=code)


def load_previous(store: Store, run_id: str, *, max_bytes: int) -> PreviousReview:
    """Read the stored ``review.json`` of the ``done`` run ``run_id`` and re-verify it against the
    row's ``artifact.sha256`` (and size). Raises ``PrefetchFailure`` (``prefetch_failed``) with
    ``lower_code`` ``previous_missing`` (no such done run, no recorded hash, or no file),
    ``previous_modified`` (size or hash differ, or the file is a link or too large) or
    ``previous_invalid`` (not one strict JSON object).
    """
    run = store.get_run(run_id)
    if run is None or run.state != "done" or not run.artifact_sha256:
        raise _previous_failure("previous_missing")
    path = store.run_dir(run_id) / "review.json"
    try:
        # O_NONBLOCK: a FIFO must not block the open; it is refused as non-regular below
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        raise _previous_failure("previous_missing") from None
    except OSError:
        raise _previous_failure("previous_modified") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
            raise _previous_failure("previous_modified")
        with os.fdopen(fd, "rb", closefd=False) as fh:
            data = fh.read(max_bytes + 1)
    finally:
        os.close(fd)
    if len(data) > max_bytes or hashlib.sha256(data).hexdigest() != run.artifact_sha256:
        raise _previous_failure("previous_modified")
    if run.artifact_size is not None and run.artifact_size != len(data):
        raise _previous_failure("previous_modified")
    try:
        review = strict_json(data.decode("utf-8"))
    except ValueError:  # includes UnicodeDecodeError
        raise _previous_failure("previous_invalid") from None
    if not isinstance(review, dict):
        raise _previous_failure("previous_invalid")
    return PreviousReview(run_id, run.artifact_sha256, review)


__all__ = [
    "Anchor",
    "Ci",
    "CiCheck",
    "FileEntry",
    "LowerFailure",
    "PrFacts",
    "Prefetch",
    "PrefetchFailure",
    "PreviousReview",
    "call_json",
    "check_pr_argument",
    "load_previous",
    "persist_prefetch",
    "prefetch",
    "resolve_error",
    "resolve_pr",
    "strict_json",
]
