"""The ``hooks`` of the acting process's config, loaded on first use (shared by ``waiting``,
``cancel``, ``resume`` and the recovery of ``reconcile``)."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from .audit import LogWriter
from .config import load_config
from .errors import ReviewRunError


class LazyHooks(Mapping[str, Any]):
    """The ``hooks`` of the acting process's config, loaded on first use.

    ``finalize_agent`` reads it only when it delivers the hooks of a run it just ended, so a poll
    of a live agent never loads (or fails on) the config. A config that does not load raises
    ``ReviewRunError`` at that point; delivery treats it as not delivered (the events stay in the
    outbox for ``reconcile``) and the finalization stands.
    """

    def __init__(
        self,
        config_path: Path | None,
        repo: str | None,
        log: LogWriter | None = None,
        run_id: str = "",
    ) -> None:
        self._path, self._repo = config_path, repo
        self._log, self._run_id = log, run_id
        self._values: Mapping[str, Any] | None = None
        self._warned = False

    def _hooks(self) -> Mapping[str, Any]:
        if self._values is None:
            try:
                hooks = load_config(self._path).resolve(self._repo).values.get("hooks")
            except ReviewRunError as exc:
                self._warn(exc.code)
                raise
            self._values = hooks if isinstance(hooks, Mapping) else {}
        return self._values

    def _warn(self, code: str) -> None:
        """One warn record, with the closed error code of the failure and a fixed message."""
        if self._warned or self._log is None:
            return
        self._warned = True
        try:
            self._log.write(
                "warning",
                "hook delivery deferred: config did not load",
                level="warn",
                run_id=self._run_id,
                code=code,
            )
        except (OSError, ReviewRunError, ValueError):  # a log line never decides a run
            pass

    def __getitem__(self, key: str) -> Any:
        return self._hooks()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._hooks())

    def __len__(self) -> int:
        return len(self._hooks())

    def __bool__(self) -> bool:  # truth must not load the config
        return True


__all__ = ["LazyHooks"]
