"""Command-line entry point for review-run: parser, global flags, dispatch and error output."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import traceback
from collections.abc import Callable
from typing import Any, NoReturn

from . import __version__
from . import runs as runs_mod
from .audit import export_lines
from .durations import DurationError, parse_duration, parse_since
from .errors import DECISION_ID_RE, MAX_DECISION_IDS, PROG, ExitCode, ReviewRunError, printable
from .fsmodes import apply_umask
from .store import Store, default_state_root

DESCRIPTION = "Run a review skill on a pull request and store the resulting review draft."
STATES = ("running", "done", "escalated", "failed")

# Grammars of schemas/defs.json (actor, decision_ids items); the label grammar of architecture
# section 8.1 and the PR and run reference shapes are checked by the commands that use them.
_ACTOR_RE = re.compile(r"(?:human|auto):[A-Za-z0-9._@-]{1,64}", re.ASCII)
_DECISION_ID_RE = DECISION_ID_RE
_HOOK_RE = re.compile(r"[A-Za-z0-9_-]+=.+", re.ASCII | re.DOTALL)


class _Parser(argparse.ArgumentParser):
    """An argparse parser whose errors raise instead of printing usage and exiting."""

    def error(self, message: str) -> NoReturn:
        # The message can echo untrusted argv text: it goes to stderr only, sanitized by the
        # printer, and never into the envelope.
        raise ReviewRunError("usage", "invalid arguments", human_detail=message)


# -- argument types: closed messages that never echo the value -------------------------------


def _duration(text: str) -> object:
    try:
        return parse_duration(text)
    except DurationError:
        raise argparse.ArgumentTypeError("not a valid duration") from None


def _positive_duration(text: str) -> object:
    try:
        return parse_duration(text, positive=True)
    except DurationError:
        raise argparse.ArgumentTypeError("not a valid duration greater than 0") from None


class _TimeoutAction(argparse.Action):
    """``--timeout``: store the timedelta in ``timeout`` and the text as given in
    ``timeout_text`` (the text is what agent-spawn receives)."""

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: object,
        option_string: str | None = None,
    ) -> None:
        text = str(values)
        try:
            delta = parse_duration(text, positive=True)
        except DurationError:
            raise argparse.ArgumentError(self, "not a valid duration greater than 0") from None
        setattr(namespace, self.dest, delta)
        namespace.timeout_text = text


def _since(text: str) -> object:
    try:
        return parse_since(text)
    except DurationError:
        raise argparse.ArgumentTypeError("not a valid duration or RFC 3339 timestamp") from None


def _matching(pattern: re.Pattern[str], what: str) -> Callable[[str], str]:
    def check(text: str) -> str:
        if pattern.fullmatch(text) is None:
            raise argparse.ArgumentTypeError(f"not a valid {what}")
        return text

    return check


def _limit(text: str) -> int:
    if re.fullmatch(r"[1-9][0-9]{0,8}", text, re.ASCII) is None:
        raise argparse.ArgumentTypeError("not a positive integer")
    return int(text)


# -- flag groups -----------------------------------------------------------------------------


def _add_global(parser: argparse.ArgumentParser, *, top: bool) -> None:
    """Global flags: accepted before and after the subcommand. On a subparser they default to
    ``SUPPRESS`` so that an absent flag never overwrites the value parsed before the command."""

    def default(value: object) -> object:
        return value if top else argparse.SUPPRESS

    parser.add_argument(
        "--json", action="store_true", default=default(False), help="print JSON on stdout"
    )
    parser.add_argument(
        "--config", metavar="PATH", default=default(None), help="read this config file"
    )
    parser.add_argument(
        "--verbose", action="store_true", default=default(False), help="more diagnostics"
    )
    parser.add_argument(
        "--quiet", action="store_true", default=default(False), help="no progress output"
    )


def _add_dry_run(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dry-run", action="store_true", help="change nothing; print the plan")


def _add_audit(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--actor", type=_matching(_ACTOR_RE, "actor"), metavar="A")
    parser.add_argument(
        "--decision-id",
        action="append",
        default=[],
        type=_matching(_DECISION_ID_RE, "decision id"),
        metavar="ID",
        help="repeatable",
    )


def _add_hook(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--hook",
        action="append",
        default=[],
        type=_matching(_HOOK_RE, "NAME=CMD hook"),
        metavar="NAME=CMD",
        help="repeatable",
    )


def _add_no_wait(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--no-wait", action="store_true", help="return once the session started")


def _register_run(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("pr")
    parser.add_argument("--skill", metavar="PLUGIN:SKILL")
    parser.add_argument("--mode", choices=("headless", "interactive"))
    parser.add_argument("--label", action="append", default=[], metavar="K=V", help="repeatable")
    parser.add_argument("--previous", metavar="RUN")
    _add_no_wait(parser)
    parser.add_argument("--model", metavar="M")
    parser.add_argument("--timeout", action=_TimeoutAction, default=None, metavar="DUR")
    parser.set_defaults(timeout_text=None)
    _add_hook(parser)
    parser.add_argument("--override-escalation", action="store_true")
    _add_audit(parser)
    _add_dry_run(parser)


def _register_resume(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("run")
    parser.add_argument("--prompt-file", required=True, metavar="PATH|-")
    _add_no_wait(parser)
    _add_hook(parser)
    _add_audit(parser)


def _register_wait(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("run")
    parser.add_argument("--timeout", action=_TimeoutAction, default=None, metavar="DUR")
    parser.set_defaults(timeout_text=None)


def _register_list(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--state", action="append", default=[], choices=STATES)
    parser.add_argument("--pr")
    parser.add_argument("--repo")
    parser.add_argument("--label", action="append", default=[], metavar="K[=V]")
    parser.add_argument("--head", metavar="SHA")
    parser.add_argument("--since", type=_since, metavar="TIME")
    parser.add_argument("--limit", type=_limit, default=100, metavar="N")


def _register_show(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("run")
    parser.add_argument("--no-refresh", action="store_true")
    parser.add_argument("--draft", action="store_true")


def _register_reconcile(parser: argparse.ArgumentParser) -> None:
    _add_dry_run(parser)


def _register_prune(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--older-than", type=_duration, metavar="DUR")
    _add_dry_run(parser)


def _register_cancel(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("run")
    _add_audit(parser)
    _add_dry_run(parser)


def _register_cron(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("action", choices=("install", "uninstall"))
    _add_dry_run(parser)


def _register_export(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--log", action="store_true", help="print log records, not audit records")
    parser.add_argument("--since", type=_since, metavar="TIME")


# One entry per command of the interface.
_COMMANDS: dict[str, tuple[str, Callable[[argparse.ArgumentParser], None]]] = {
    "run": ("run a review skill on a pull request", _register_run),
    "resume": ("answer an escalated run", _register_resume),
    "wait": ("wait for a run", _register_wait),
    "list": ("list runs", _register_list),
    "show": ("show a run", _register_show),
    "reconcile": ("repair state against reality", _register_reconcile),
    "prune": ("delete old terminal runs", _register_prune),
    "cancel": ("end a run without an answer", _register_cancel),
    "cron": ("install or remove the crontab block", _register_cron),
    "export": ("print audit or log records", _register_export),
}


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog=PROG, description=DESCRIPTION, allow_abbrev=False)
    parser.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    _add_global(parser, top=True)
    sub = parser.add_subparsers(dest="command", metavar="<command>", parser_class=_Parser)
    for name, (help_text, register) in _COMMANDS.items():
        child = sub.add_parser(name, help=help_text, description=help_text, allow_abbrev=False)
        _add_global(child, top=False)
        register(child)
    return parser


# -- commands --------------------------------------------------------------------------------

Handler = Callable[[argparse.Namespace], int]


def _stub(name: str) -> Handler:
    def handler(args: argparse.Namespace) -> int:
        raise ReviewRunError("internal", f"{name} is not implemented yet")

    return handler


def _export(args: argparse.Namespace) -> int:
    """Print audit (or log) records as JSONL lines, no envelope."""
    # TODO(#6): --config validation (config_invalid) is wired into this handler by #6; the state
    # root stays default_state_root() until then.
    root = default_state_root()
    try:
        for line in export_lines(root, log=args.log, since=args.since):
            sys.stdout.buffer.write(line)
    except BrokenPipeError:
        _silence_stdout()
    return 0


def _write_stdout(text: str | bytes) -> None:
    data = text.encode("utf-8") if isinstance(text, str) else text
    try:
        sys.stdout.buffer.write(data)
    except BrokenPipeError:
        _silence_stdout()


def _note_corrupt(count: int) -> None:
    """One bounded stderr line: a count only, never stored text."""
    print(f"{PROG}: skipped {count} unreadable run(s)", file=sys.stderr)


def _list(args: argparse.Namespace) -> int:
    """``list``: read-only; no refresh."""
    # TODO(#6): --config validation (config_invalid) is wired into this handler by #6; the state
    # root stays default_state_root() until then.
    with Store.open(default_state_root()) as store:
        result = runs_mod.list_runs(
            store,
            states=args.state,
            pr=args.pr,
            repo=args.repo,
            labels=args.label,
            head=args.head,
            since=args.since,
            limit=args.limit,
            on_corrupt=_note_corrupt,
        )
    if args.json:
        _write_stdout(runs_mod.dump(result))
    else:
        _write_stdout(runs_mod.human_list(result["runs"], result["truncated"]))
    return 0


def _show(args: argparse.Namespace) -> int:
    """``show``: the run object, or with ``--draft`` the stored review.json as stored."""
    with Store.open(default_state_root()) as store:
        if args.draft:
            data = runs_mod.read_draft(store, args.run)
            print(f"{PROG}: printing the stored draft as stored (untrusted text)", file=sys.stderr)
            _write_stdout(data)
            return 0
        obj = runs_mod.show_run(store, args.run)
    _write_stdout(runs_mod.dump(obj) if args.json else runs_mod.human_run(obj))
    return 0


# Each command is replaced by its owning issue's implementation.
_HANDLERS: dict[str, Handler] = {
    "run": _stub("run"),  # TODO(#16): run order and settings; #18 launch; #24 outcome mapping
    "resume": _stub("resume"),  # TODO(#28): resume
    "wait": _stub("wait"),  # TODO(#24): wait
    "list": _list,
    "show": _show,  # TODO(#24): the refresh of show (without --no-refresh)
    "reconcile": _stub("reconcile"),  # TODO(#29): reconcile
    "prune": _stub("prune"),  # TODO(#37): prune
    "cancel": _stub("cancel"),  # TODO(#26): cancel
    "cron": _stub("cron"),  # TODO(#38): cron install and uninstall
    "export": _export,
}


# -- entry point -----------------------------------------------------------------------------


def _has_flag(argv: list[str], flag: str) -> bool:
    """Whether ``flag`` is among the flags (used when parsing itself failed)."""
    flags = argv[: argv.index("--")] if "--" in argv else argv
    return flag in flags


def _wants_json(argv: list[str]) -> bool:
    return _has_flag(argv, "--json")


def _silence_stdout() -> None:
    """Stdout is closed or full: point fd 1 at /dev/null so that nothing later fails on it."""
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(devnull, 1)
        finally:
            os.close(devnull)
    except OSError:
        pass


def _report(err: ReviewRunError, *, as_json: bool) -> int:
    print(err.human(), file=sys.stderr)
    if as_json:
        try:
            print(json.dumps(err.envelope(), ensure_ascii=True))
        except (OSError, ValueError):
            _silence_stdout()
    return err.exit_code


def _run(argv: list[str]) -> int:
    parser = build_parser()
    args: Any = parser.parse_args(argv)
    command: str | None = args.command
    if command is None:
        raise ReviewRunError("usage", "a command is required", human_detail="a command is required")
    if len(getattr(args, "decision_id", [])) > MAX_DECISION_IDS:
        raise ReviewRunError("usage", "too many decision ids", details={"max": MAX_DECISION_IDS})
    return _HANDLERS[command](args)


def _diagnose(exc: BaseException) -> None:
    """``--verbose`` line for an unexpected error: its type and innermost frame, no message."""
    frames = traceback.extract_tb(exc.__traceback__)
    where = f" at {os.path.basename(frames[-1].filename)}:{frames[-1].lineno}" if frames else ""
    print(f"{PROG}: {printable(type(exc).__name__)}{where}", file=sys.stderr)


def _dispatch(argv: list[str]) -> int:
    try:
        return _run(argv)
    except ReviewRunError as err:
        return _report(err, as_json=_wants_json(argv))
    except KeyboardInterrupt:
        return int(ExitCode.SIGINT)
    except Exception as exc:  # an unexpected bug: no traceback, no untrusted text
        code = _report(ReviewRunError("internal", "unexpected error"), as_json=_wants_json(argv))
        if _has_flag(argv, "--verbose"):
            _diagnose(exc)
        return code


def main(argv: list[str] | None = None) -> int:
    apply_umask()
    argv = list(sys.argv[1:] if argv is None else argv)
    code = _dispatch(argv)
    out = sys.stdout
    if out is not None:
        try:
            out.flush()
        except (OSError, ValueError):
            _silence_stdout()
    return code


__all__ = ["ExitCode", "build_parser", "main"]
