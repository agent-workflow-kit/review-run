"""The ``state.db`` schema: version 1 and the additive migration rules (interface.md, Upgrades).

A migration is a tuple of single SQL statements. Only additive statements are allowed
(``CREATE TABLE``, ``CREATE INDEX``, ``ALTER TABLE ... ADD COLUMN``): older code keeps working on a
newer database, and nothing is ever dropped, renamed or rewritten. ``check_additive`` is applied to
every migration before it runs.
"""

from __future__ import annotations

import re

STATES = ("running", "done", "escalated", "failed")
PHASES = ("prefetch", "code_run", "session", "finalize")

# Every column of ``runs`` except ``run_id``, in table order. JSON_COLUMNS hold JSON text.
JSON_COLUMNS = frozenset({"hooks", "decision_ids", "anchor", "summary", "failure", "warnings"})

_V1: tuple[str, ...] = (
    """CREATE TABLE schema_version (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        version INTEGER NOT NULL
    )""",
    # Every reserved run id, never deleted (a vetoed or escalated attempt's id is not reused).
    """CREATE TABLE ids (
        run_id TEXT PRIMARY KEY CHECK (
            length(run_id) = 11 AND substr(run_id, 1, 3) = 'rr-'
        ),
        reserved_at TEXT NOT NULL
    )""",
    """CREATE TABLE runs (
        run_id TEXT PRIMARY KEY REFERENCES ids (run_id),
        pr TEXT NOT NULL,
        repo TEXT NOT NULL,
        head_sha TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('running', 'done', 'escalated', 'failed')),
        phase TEXT CHECK (phase IN ('prefetch', 'code_run', 'session', 'finalize')),
        reason TEXT,
        exit_code INTEGER,
        mode TEXT NOT NULL,
        skill TEXT NOT NULL,
        previous_run TEXT,
        agent_id TEXT,
        csession_id TEXT,
        awaited_run INTEGER,
        escalated_run INTEGER,
        finalized_run INTEGER,
        hooks TEXT NOT NULL DEFAULT '[]',
        actor TEXT,
        decision_ids TEXT NOT NULL DEFAULT '[]',
        draft_id TEXT,
        anchor TEXT,
        created_at TEXT NOT NULL,
        escalated_at TEXT,
        escalation_reason TEXT,
        escalation_exit_code INTEGER,
        escalation_at TEXT,
        escalation_awaited_run INTEGER,
        cancel_requested_at TEXT,
        cancel_actor TEXT,
        cancel_exit_code INTEGER,
        finished_at TEXT,
        artifact_path TEXT,
        artifact_sha256 TEXT,
        artifact_size INTEGER,
        original_sha256 TEXT,
        summary TEXT,
        failure TEXT,
        warnings TEXT NOT NULL DEFAULT '[]',
        teardown TEXT CHECK (teardown IN ('done', 'pending', 'failed')),
        runner_pid INTEGER,
        runner_start TEXT,
        launcher_pid INTEGER,
        launcher_start TEXT,
        launch_started_at TEXT,
        throwaway_id TEXT,
        config_sha256 TEXT,
        CHECK ((state = 'running') = (phase IS NOT NULL))
    )""",
    "CREATE INDEX runs_created ON runs (created_at, run_id)",
    "CREATE INDEX runs_pr ON runs (pr)",
    "CREATE INDEX runs_repo ON runs (repo)",
    "CREATE INDEX runs_head ON runs (head_sha)",
    "CREATE INDEX runs_state ON runs (state)",
    # A multiset: the same pair may repeat; position keeps the order given.
    """CREATE TABLE labels (
        run_id TEXT NOT NULL REFERENCES runs (run_id) ON DELETE CASCADE,
        position INTEGER NOT NULL,
        key TEXT NOT NULL,
        value TEXT NOT NULL,
        PRIMARY KEY (run_id, position)
    )""",
    "CREATE INDEX labels_kv ON labels (key, value)",
    # The outbox of hook events and audit records of one transition (interface.md, Hooks).
    """CREATE TABLE events (
        event_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES runs (run_id) ON DELETE CASCADE,
        hook TEXT NOT NULL,
        payload TEXT NOT NULL,
        audit TEXT NOT NULL DEFAULT '[]',
        audit_pending INTEGER NOT NULL DEFAULT 1,
        attempts INTEGER NOT NULL DEFAULT 0,
        delivered INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    )""",
    "CREATE INDEX events_run ON events (run_id)",
    # One row per lease name (``run:<id>``, ``reconcile``, ``prune``).
    """CREATE TABLE leases (
        name TEXT PRIMARY KEY,
        token TEXT NOT NULL,
        holder TEXT NOT NULL,
        acquired_at TEXT NOT NULL,
        expires_at TEXT NOT NULL
    )""",
)

# Version 2: the counters of ``reconcile`` (failed teardown retries per run, audit passes per
# event),
# so that ``stuck_teardowns`` and ``stuck_audit`` count repeated failures (interface.md, reconcile).
_V2: tuple[str, ...] = (
    """CREATE TABLE repair_attempts (
        kind TEXT NOT NULL CHECK (kind IN ('teardown', 'audit')),
        key TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (kind, key)
    )""",
)

# Index i holds the statements that take the schema from version i to i + 1.
MIGRATIONS: tuple[tuple[str, ...], ...] = (_V1, _V2)
SCHEMA_VERSION = len(MIGRATIONS)

_ADDITIVE = re.compile(
    r"^\s*(?:"
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?\w+"
    r"|CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?\w+"
    r"|ALTER\s+TABLE\s+\w+\s+ADD\s+COLUMN\s+\w+"
    r")",
    re.IGNORECASE,
)


def check_additive(statements: tuple[str, ...]) -> None:
    """Raise ``ValueError`` unless every statement is additive and a single statement."""
    for sql in statements:
        if ";" in sql.strip().rstrip(";") or not _ADDITIVE.match(sql):
            raise ValueError(f"migration statement is not additive: {sql.split()[:4]}")
