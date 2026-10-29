"""Command-line entry point for review-run."""

from __future__ import annotations

import argparse
import sys

from . import __version__

PROG = "review-run"
DESCRIPTION = "Run a review skill on a pull request and store the resulting review draft."
USAGE_ERROR = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=DESCRIPTION,
    )
    parser.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON output")
    parser.add_subparsers(dest="command", metavar="<command>")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command: str | None = args.command
    if command is None:
        parser.print_help(sys.stderr)
        return USAGE_ERROR
    return 0
