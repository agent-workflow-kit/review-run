"""Manifest, per-file inputs, hunk splitting and size caps (issue #13).

A pure function of (the ``files`` list, the unified diff, the caps): no I/O, no clock, no
randomness, so the same input always gives the same output. The caller (the run orchestration)
writes ``manifest.json`` and the ``inputs/*.patch`` files, adds the fence lines (#14) and renders
``manifest_table`` inside the ``manifest`` fence.

Scope: #14 writes ``inputs/*.patch`` and ``manifest.json`` (from ``Manifest.to_json()``), adds the
fences, the nonce and the meta; #18 wires ``build_manifest`` into the run orchestration (it is not
called from ``prefetch()``).

Public API::

    build_manifest(files, diff, *, max_file_bytes, max_total_bytes, max_files,
                   hard_ceiling_bytes) -> Manifest        # raises PrefetchFailure
    Manifest.entries / .inputs                            # ManifestEntry / InputFile tuples
    Manifest.to_json()          # manifest.json: a list of schemas/defs.json manifest_entry
    Manifest.coverage_preview() # defs.json coverage, as if the session reviewed every provided file
    Manifest.sizes(diff_bytes)  # the dry run's ``sizes``
    manifest_table(manifest)    # the prompt's table, every untrusted cell sanitized

Failures (``PrefetchFailure``, exit 12): ``prefetch_failed`` with ``lower_code``
``diff_files_mismatch``; ``too_large`` with ``too_many_files`` (more than 9999 files),
``hard_ceiling`` (the diff is above ``hard_ceiling_bytes``) or ``no_file_fits`` (something was
skipped and nothing was provided).

Section boundaries are the lines that start with ``diff --git `` (a content line of a hunk always
starts with a space, ``+``, ``-`` or a backslash, so it can never be one). Hunk counts are never
trusted: only the lines starting ``@@ `` are used, so a header that lies about its counts cannot
hide or forge a section or a hunk boundary. The header block of a section (up to its first hunk)
is parsed on the raw text, so the paths are compared exactly as forgectl reports them; everything
that leaves this module is sanitized first.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from review_run.prefetch import PREFETCH_FAILED, TOO_LARGE, FileEntry, PrefetchFailure
from review_run.sanitizer import sanitize

MAX_FILES_IN_PR = 9999
PATH_BUDGET = 4096  # interface.md, Prompt files: each manifest path
SKIPPED_ROWS_SHOWN = 50
MAX_PATH_CHARS = 4096  # schemas/defs.json manifest_entry: path and old_path maxLength

PROVIDED = "provided"
TRUNCATED = "truncated"
SKIPPED_SIZE_CAP = "skipped_size_cap"
SKIPPED_FILE_CAP = "skipped_file_cap"
BINARY = "binary"
EMPTY = "empty"

_DISPLAY = {
    SKIPPED_SIZE_CAP: "skipped(size_cap)",
    SKIPPED_FILE_CAP: "skipped(file_cap)",
}
_COVERAGE_REASON = {
    TRUNCATED: "truncated",
    SKIPPED_SIZE_CAP: "size_cap",
    SKIPPED_FILE_CAP: "file_cap",
}
_ENTRY_KEYS = frozenset(
    {
        "file",
        "path",
        "status",
        "old_path",
        "additions",
        "deletions",
        "bytes",
        "inputs",
        "disposition",
    }
)
_DISPOSITIONS = frozenset({PROVIDED, TRUNCATED, SKIPPED_SIZE_CAP, SKIPPED_FILE_CAP, BINARY, EMPTY})
_FILE_ID = re.compile(r"f[0-9]{4}", re.ASCII)


def _is_count(value: object, *, null: bool = False) -> bool:
    if value is None:
        return null
    return type(value) is int and value >= 0  # noqa: E721 (a bool is not a count)


_COLUMNS = (
    "file",
    "path",
    "status",
    "additions",
    "deletions",
    "bytes",
    "inputs",
    "disposition",
)
_GIT = "diff --git "
_HUNK = "@@ "


# --------------------------------------------------------------------------------------------
# results
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class InputFile:
    """One per-file input: ``name`` (``f0001.patch`` or ``f0001-2.patch``) and its sanitized
    text, at most ``max_file_bytes`` bytes. The fence lines are not part of ``text``."""

    name: str
    text: str


@dataclass(frozen=True)
class ManifestEntry:
    """One changed file. ``path`` and ``old_path`` are raw and untrusted. ``bytes`` is the size
    of the whole sanitized section; ``disposition`` is one of ``provided``, ``truncated``,
    ``skipped_size_cap``, ``skipped_file_cap``, ``binary``, ``empty``."""

    file: str
    path: str
    old_path: str | None
    status: str
    additions: int | None
    deletions: int | None
    bytes: int
    inputs: tuple[str, ...]
    disposition: str


@dataclass(frozen=True)
class Manifest:
    entries: tuple[ManifestEntry, ...]
    inputs: tuple[InputFile, ...]

    def to_json(self) -> list[dict[str, Any]]:
        """``manifest.json``: the entries as ``manifest_entry`` objects (raw, untrusted text)."""
        return [
            {
                "file": e.file,
                "path": e.path,
                "status": e.status,
                "old_path": e.old_path,
                "additions": e.additions,
                "deletions": e.deletions,
                "bytes": e.bytes,
                "inputs": list(e.inputs),
                "disposition": e.disposition,
            }
            for e in self.entries
        ]

    @classmethod
    def from_json(cls, document: object) -> Manifest:
        """Rebuild the entries from ``manifest.json`` (a list of ``manifest_entry`` objects), for
        a finalizer in another process. The input files are not stored there, so ``inputs`` is
        empty. Raises ``ValueError`` for anything that is not the closed shape ``to_json``
        writes."""
        if not isinstance(document, list):
            raise ValueError("manifest.json is a list")
        entries: list[ManifestEntry] = []
        for item in document:
            if not isinstance(item, dict) or set(item) != _ENTRY_KEYS:
                raise ValueError("a manifest entry has the closed set of keys")
            names = item["inputs"]
            old = item["old_path"]
            if (
                not isinstance(item["file"], str)
                or not _FILE_ID.fullmatch(item["file"])
                or not isinstance(item["path"], str)
                or not item["path"]
                or not (old is None or (isinstance(old, str) and old))
                or item["status"] not in ("added", "modified", "deleted", "renamed")
                or item["disposition"] not in _DISPOSITIONS
                or not isinstance(names, list)
                or not all(isinstance(n, str) for n in names)
                or not _is_count(item["bytes"])
                or not all(_is_count(item[k], null=True) for k in ("additions", "deletions"))
            ):
                raise ValueError("a manifest entry is malformed")
            entries.append(
                ManifestEntry(
                    file=item["file"],
                    path=item["path"],
                    old_path=old,
                    status=item["status"],
                    additions=item["additions"],
                    deletions=item["deletions"],
                    bytes=item["bytes"],
                    inputs=tuple(names),
                    disposition=item["disposition"],
                )
            )
        if len({e.file for e in entries}) != len(entries):
            raise ValueError("manifest file ids are unique")
        return cls(tuple(entries), ())

    def coverage_preview(self) -> dict[str, Any]:
        """The ``coverage`` object if the session reviewed every provided entry: ``reviewed`` is
        the ``provided`` ids, ``skipped`` the ``truncated`` and skipped ones; ``binary`` and
        ``empty`` entries are in neither list."""
        reviewed = [e.file for e in self.entries if e.disposition == PROVIDED]
        skipped = [
            {"file": e.file, "reason": _COVERAGE_REASON[e.disposition]}
            for e in self.entries
            if e.disposition in _COVERAGE_REASON
        ]
        return {
            "status": "partial" if skipped else "full",
            "reviewed": reviewed,
            "skipped": skipped,
            "ignored_ids": 0,
        }

    def sizes(self, diff_bytes: int) -> dict[str, int]:
        """The dry run's ``sizes``; ``provided_files`` counts entries (``provided`` and
        ``truncated``), ``provided_bytes`` the bytes of the input files."""
        return {
            "files": len(self.entries),
            "diff_bytes": diff_bytes,
            "provided_bytes": sum(len(i.text.encode("utf-8")) for i in self.inputs),
            "provided_files": sum(1 for e in self.entries if e.inputs),
        }


# --------------------------------------------------------------------------------------------
# failures
# --------------------------------------------------------------------------------------------


def _mismatch(detail: str | None = None) -> PrefetchFailure:
    failure = PrefetchFailure(PREFETCH_FAILED, lower_code="diff_files_mismatch")
    if detail:  # closed words and a file number only, never a path
        failure.args = (f"{failure.args[0]}: {detail}",)
    return failure


def _too_large(code: str) -> PrefetchFailure:
    return PrefetchFailure(TOO_LARGE, lower_code=code)


# --------------------------------------------------------------------------------------------
# header parsing (raw text; never raises anything but PrefetchFailure)
# --------------------------------------------------------------------------------------------

_ESCAPES = {
    "a": 7,
    "b": 8,
    "f": 12,
    "n": 10,
    "r": 13,
    "t": 9,
    "v": 11,
    '"': 34,
    "\\": 92,
}


def _unquote_at(text: str, start: int) -> tuple[str, int] | None:
    """Parse the git-quoted string whose opening quote is ``text[start]``; return it and the
    index after the closing quote. Octal escapes are bytes of UTF-8 (invalid UTF-8 is None)."""
    out = bytearray()
    i = start + 1
    n = len(text)
    while i < n:
        c = text[i]
        if c == '"':
            try:
                return out.decode("utf-8"), i + 1
            except UnicodeDecodeError:
                return None
        if c != "\\":
            out += c.encode("utf-8", errors="surrogatepass")
            i += 1
            continue
        i += 1
        if i >= n:
            return None
        e = text[i]
        if e in _ESCAPES:
            out.append(_ESCAPES[e])
            i += 1
        elif e in "0123" and i + 2 < n and text[i + 1] in "01234567" and text[i + 2] in "01234567":
            out.append(int(text[i : i + 3], 8))
            i += 3
        else:
            return None
    return None


def _path_token(token: str, *, tab_suffix: bool) -> str | None:
    """A path as git prints it: quoted (then nothing, or a TAB suffix, may follow) or plain
    (``tab_suffix``: cut at the first TAB)."""
    if token.startswith('"'):
        got = _unquote_at(token, 0)
        if got is None:
            return None
        value, end = got
        if end != len(token) and not (tab_suffix and token[end] == "\t"):
            return None
        return value
    return token.split("\t", 1)[0] if tab_suffix else token


def _strip_prefix(path: str | None, prefix: str) -> str | None:
    if path is None or not path.startswith(prefix):
        return None
    return path[len(prefix) :]


def _parse_git_line(rest: str) -> tuple[str, str] | None:
    """``a/<old> b/<new>`` of the ``diff --git`` line, or None when it cannot be told apart
    (unquoted paths that differ: only the rename lines can say)."""
    if rest.startswith('"'):
        got = _unquote_at(rest, 0)
        if got is None or got[1] >= len(rest) or rest[got[1]] != " ":
            return None
        a, b_tok = got[0], rest[got[1] + 1 :]
        b = _path_token(b_tok, tab_suffix=False)
    elif ' "' in rest:
        cut = rest.index(' "')
        a, b = rest[:cut], _path_token(rest[cut + 1 :], tab_suffix=False)
    else:
        n = len(rest)
        if n < 5 or (n - 5) % 2:
            return None
        half = (n - 5) // 2
        a, b = rest[: 2 + half], rest[3 + half :]
        if rest[2 + half] != " " or a[2:] != b[2:]:
            return None
    old, new = _strip_prefix(a, "a/"), _strip_prefix(b, "b/")
    if old is None or new is None:
        return None
    return old, new


@dataclass
class _Header:
    old: list[str]  # every path the header names for the old side
    new: list[str]
    has_hunk: bool
    binary_marker: bool


def _parse_header(raw: str) -> _Header:
    """Parse the header block of one section (``raw`` has LF line ends): up to the first line
    that starts ``@@ `` (``GIT binary patch`` also ends it). Raises the mismatch failure for any
    path that cannot be read or any header line given twice."""
    pos = 0
    end = len(raw)
    git_rest: str | None = None
    rename_from: str | None = None
    rename_to: str | None = None
    minus: str | None = None
    plus: str | None = None
    binary = False
    has_hunk = False
    first = True
    seen: set[str] = set()
    while pos <= end:
        nl = raw.find("\n", pos)
        line = raw[pos : end if nl < 0 else nl]
        pos = end + 1 if nl < 0 else nl + 1
        if first:
            first = False
            git_rest = line[len(_GIT) :]
            continue
        if line.startswith(_HUNK):
            has_hunk = True
            break
        if line == "GIT binary patch":
            binary = True
            break
        if line.startswith("Binary files "):
            binary = True
            continue
        for key in ("rename from ", "rename to ", "--- ", "+++ "):
            if line.startswith(key):
                if key in seen:
                    raise _mismatch()
                seen.add(key)
                value = line[len(key) :]
                if key == "rename from ":
                    rename_from = _path_token(value, tab_suffix=False)
                    if rename_from is None:
                        raise _mismatch()
                elif key == "rename to ":
                    rename_to = _path_token(value, tab_suffix=False)
                    if rename_to is None:
                        raise _mismatch()
                else:
                    token = _path_token(value, tab_suffix=True)
                    if token is None:
                        raise _mismatch()
                    side = "a/" if key == "--- " else "b/"
                    if token != "/dev/null":
                        got = _strip_prefix(token, side)
                        if got is None:
                            raise _mismatch()
                        if key == "--- ":
                            minus = got
                        else:
                            plus = got
                break
    if (rename_from is None) != (rename_to is None):
        raise _mismatch()
    assert git_rest is not None
    git = _parse_git_line(git_rest)
    if git is None and rename_from is None:
        raise _mismatch()
    old: list[str] = []
    new: list[str] = []
    if git is not None:
        old.append(git[0])
        new.append(git[1])
    if rename_from is not None and rename_to is not None:
        old.append(rename_from)
        new.append(rename_to)
    if minus is not None:
        old.append(minus)
    if plus is not None:
        new.append(plus)
    return _Header(old, new, has_hunk, binary)


def _split_sections(diff: str, count: int) -> list[str]:
    """The raw sections, split at lines that start ``diff --git ``; the count must be ``count``."""
    if not diff:
        starts: list[int] = []
    elif diff.startswith(_GIT):
        starts = [0]
        pos = 0
        marker = "\n" + _GIT
        while True:
            i = diff.find(marker, pos)
            if i < 0:
                break
            starts.append(i + 1)
            pos = i + 1
    else:
        raise _mismatch()
    if len(starts) != count:
        raise _mismatch()
    ends = [*starts[1:], len(diff)][: len(starts)]
    return [diff[s:e] for s, e in zip(starts, ends, strict=True)]


# --------------------------------------------------------------------------------------------
# splitting
# --------------------------------------------------------------------------------------------


def _size(text: str) -> int:
    return len(text.encode("utf-8"))


def _cut(text: str, limit: int) -> str:
    """The longest prefix of ``text`` of at most ``limit`` bytes that ends at a line boundary;
    when the first line alone is longer, a cut on a character boundary that never lies inside a
    ``<U+XXXX>`` marker. Empty when nothing fits."""
    if limit <= 0:
        return ""
    prefix = text.encode("utf-8")[:limit].decode("utf-8", errors="ignore")
    nl = prefix.rfind("\n")
    if nl >= 0:
        return prefix[: nl + 1]
    i = prefix.rfind("<U+", max(0, len(prefix) - 10))
    if i >= 0 and ">" not in prefix[i:]:
        prefix = prefix[:i]
    return prefix


def _cut_hunk(unit: str, limit: int) -> str:
    """``_cut`` for one oversize hunk unit (the file header may precede its ``@@ `` line): the
    result always holds at least one content byte after the ``@@ `` line, or is empty when not
    even that fits. When no whole content line fits, the first one is cut on a character
    boundary (never inside a ``<U+XXXX>`` marker)."""
    at = 0 if unit.startswith(_HUNK) else unit.find("\n" + _HUNK) + 1
    nl = unit.find("\n", at)
    head_len = len(unit) if nl < 0 else nl + 1
    piece = _cut(unit, limit)
    if len(piece) > head_len:
        return piece
    room = limit - _size(unit[:head_len])
    if room <= 0:
        return ""
    body = _cut(unit[head_len:], room)
    return unit[:head_len] + body if body else ""


def _units(text: str) -> list[str]:
    """Split at hunk starts; the header block travels with the first hunk."""
    starts: list[int] = []
    pos = 0
    marker = "\n" + _HUNK
    while True:
        i = text.find(marker, pos)
        if i < 0:
            break
        starts.append(i + 1)
        pos = i + 1
    if len(starts) < 2:
        return [text]
    bounds = [0, *starts[1:], len(text)]
    return [text[a:b] for a, b in zip(bounds, bounds[1:], strict=False)]


def _split(text: str, limit: int) -> tuple[list[str], bool]:
    """The parts of a sanitized patch of more than ``limit`` bytes: whole hunks packed greedily
    into parts of at most ``limit`` bytes; a hunk above ``limit`` gets a part of its own, cut at
    a line boundary (inside its first line when none fits; nothing when not even one content byte
    fits after the header), the rest is dropped, and the file is truncated."""
    parts: list[str] = []
    cur: list[str] = []
    cur_bytes = 0
    truncated = False
    for unit in _units(text):
        size = _size(unit)
        if size > limit:
            if cur:
                parts.append("".join(cur))
                cur, cur_bytes = [], 0
            piece = _cut_hunk(unit, limit)
            truncated = True
            if piece:
                parts.append(piece)
        elif cur_bytes + size <= limit:
            cur.append(unit)
            cur_bytes += size
        else:
            parts.append("".join(cur))
            cur, cur_bytes = [unit], size
    if cur:
        parts.append("".join(cur))
    return parts, truncated


# --------------------------------------------------------------------------------------------
# build
# --------------------------------------------------------------------------------------------


def build_manifest(
    files: tuple[FileEntry, ...] | list[FileEntry],
    diff: str,
    *,
    max_file_bytes: int,
    max_total_bytes: int,
    max_files: int,
    hard_ceiling_bytes: int,
) -> Manifest:
    """Split ``diff`` into the manifest entries and per-file inputs (interface.md, Manifest and
    inputs).

    The raw size caps run first (more than 9999 files, a diff above the hard ceiling), then every
    section's paths are checked against ``files`` (all of them, before anything is sanitized or
    split), then the sections are sanitized one by one, measured, split and admitted until the
    first patch that fits neither ``max_total_bytes`` nor ``max_files``. A patch is admitted whole
    or as all its parts. When both caps would be exceeded the reason is ``size_cap``. A file whose
    parts all come out empty (a cap smaller than one character) is skipped as ``size_cap``.
    Sizes are UTF-8 bytes of the sanitized text. Nothing provided with something skipped raises
    ``too_large``; nothing provided with only ``binary`` and ``empty`` entries (or no files at
    all) is a valid, empty result. A section with no hunk is ``binary`` (a binary marker, or
    both counts null), ``empty`` (no change counted: each count 0 or null) or a
    ``diff_files_mismatch`` (a count above 0: the diff lacks the patch forgectl counted). A path
    or previous path above 4096 characters is a
    ``diff_files_mismatch`` too (it could not be written to ``manifest.json``).
    """
    if len(files) > MAX_FILES_IN_PR:
        raise _too_large("too_many_files")
    if len(diff.encode("utf-8", errors="surrogatepass")) > hard_ceiling_bytes:
        raise PrefetchFailure(TOO_LARGE, tool="forgectl", lower_code="hard_ceiling")
    raws = _split_sections(diff, len(files))
    headers = []
    for number, (entry, raw) in enumerate(zip(files, raws, strict=True), start=1):
        header = _parse_header(raw.replace("\r\n", "\n"))
        if len(entry.path) > MAX_PATH_CHARS or (
            entry.previous_path is not None and len(entry.previous_path) > MAX_PATH_CHARS
        ):
            raise _mismatch(f"path of file {number} is longer than {MAX_PATH_CHARS} characters")
        want_old = entry.previous_path if entry.previous_path is not None else entry.path
        if any(p != entry.path for p in header.new) or any(p != want_old for p in header.old):
            raise _mismatch()
        if not header.has_hunk and not header.binary_marker:
            if (entry.additions or 0) + (entry.deletions or 0) > 0:
                raise _mismatch(f"file {number} has counted changes but no patch")
        headers.append(header)

    entries: list[ManifestEntry] = []
    inputs: list[InputFile] = []
    total = 0
    stop: str | None = None
    for index, (entry, raw, header) in enumerate(zip(files, raws, headers, strict=True), start=1):
        fid = f"f{index:04d}"
        text = sanitize(raw)
        nbytes = _size(text)
        names: tuple[str, ...] = ()
        if not header.has_hunk:
            nulls = entry.additions is None and entry.deletions is None
            disposition = BINARY if (nulls or header.binary_marker) else EMPTY  # counts checked
        elif stop is not None:
            disposition = stop
        else:
            if nbytes <= max_file_bytes:
                parts, truncated, named = [text], False, [f"{fid}.patch"]
            else:
                parts, truncated = _split(text, max_file_bytes)
                named = [f"{fid}-{k}.patch" for k in range(1, len(parts) + 1)]
            provided = sum(_size(p) for p in parts)
            if not parts or total + provided > max_total_bytes:
                stop = disposition = SKIPPED_SIZE_CAP
            elif len(inputs) + len(parts) > max_files:
                stop = disposition = SKIPPED_FILE_CAP
            else:
                total += provided
                names = tuple(named)
                inputs.extend(InputFile(n, p) for n, p in zip(named, parts, strict=True))
                disposition = TRUNCATED if truncated else PROVIDED
        entries.append(
            ManifestEntry(
                file=fid,
                path=entry.path,
                old_path=entry.previous_path,
                status=entry.status,
                additions=entry.additions,
                deletions=entry.deletions,
                bytes=nbytes,
                inputs=names,
                disposition=disposition,
            )
        )
    if not inputs and stop is not None:
        raise _too_large("no_file_fits")
    return Manifest(tuple(entries), tuple(inputs))


# --------------------------------------------------------------------------------------------
# the prompt table
# --------------------------------------------------------------------------------------------


def _cell(value: str, budget: int | None = None) -> str:
    """One table cell: sanitized with ``|`` and LF marked (``<U+007C>``, ``<U+000A>``) before
    anything can be neutralized, so the cell is one line and no fence look-alike can be
    rebuilt. Only the sanitizer's own ``[... N bytes omitted ...]`` line breaks remain; each is
    followed by the ``[...`` of that line, so replacing them with a space joins no ``<`` to
    ``untrusted``."""
    return sanitize(value, budget=budget, mark_extra="|\n").replace("\n", " ")


def manifest_table(manifest: Manifest) -> str:
    """The manifest table of the prompt (the body of the ``manifest`` fence, no trailing LF).

    A header row, a separator row, then the rows of provided, truncated, binary and empty entries
    and of the first 50 skipped ones, in file order, then ``N more skipped`` when more were
    skipped. Every path is sanitized with a 4 KiB budget and ``|`` marked, so a path can neither
    start a cell nor a row; every other cell is generated here from numbers and closed words.
    """
    lines = [
        "| " + " | ".join(_COLUMNS) + " |",
        "| " + " | ".join("---" for _ in _COLUMNS) + " |",
    ]
    skipped = 0
    shown_skipped = 0
    for e in manifest.entries:
        if e.disposition in (SKIPPED_SIZE_CAP, SKIPPED_FILE_CAP):
            skipped += 1
            if shown_skipped >= SKIPPED_ROWS_SHOWN:
                continue
            shown_skipped += 1
        path = _cell(e.path, PATH_BUDGET)
        if e.old_path is not None:
            path += f" (renamed from {_cell(e.old_path, PATH_BUDGET)})"
        cells = [
            e.file,
            path,
            e.status,
            "-" if e.additions is None else str(e.additions),
            "-" if e.deletions is None else str(e.deletions),
            str(e.bytes),
            ",".join(e.inputs) if e.inputs else "-",
            _DISPLAY.get(e.disposition, e.disposition),
        ]
        lines.append("| " + " | ".join(cells) + " |")
    if skipped > shown_skipped:
        lines.append(f"{skipped - shown_skipped} more skipped")
    return "\n".join(lines)


__all__ = [
    "BINARY",
    "EMPTY",
    "PROVIDED",
    "SKIPPED_FILE_CAP",
    "SKIPPED_SIZE_CAP",
    "TRUNCATED",
    "InputFile",
    "Manifest",
    "ManifestEntry",
    "build_manifest",
    "manifest_table",
]
