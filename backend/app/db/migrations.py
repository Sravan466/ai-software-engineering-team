"""Additive, idempotent schema migrations.

The project has no migration framework: `init_db()` calls `Base.metadata.create_all`,
which creates missing *tables* but never touches an existing one. So a column added to
a model after someone already has a database is simply absent at runtime, and every
query against it fails.

This module closes that gap for the only kind of change made here — adding a nullable
column, or one with a constant default. Two properties matter:

  • **It cannot drift from the models.** The type and default are compiled from the
    live `Column` object for the connected dialect, rather than hand-written SQL. A
    hand-written `BOOLEAN NOT NULL DEFAULT 0` is valid on SQLite and a type error on
    Postgres, and `TIMESTAMP` there means `timestamp without time zone` where the
    model asked for `timestamptz` — so the only safe source of that text is the model.

  • **It is safe to run on every boot.** Each column is checked against the live schema
    first, so a second run is a no-op, and a rejected statement warns rather than
    taking the app down.

Anything destructive — dropping or retyping a column, backfilling with a query — does
not belong here. That needs a real migration tool.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import Engine, inspect, text

from app.core.logging import get_logger
from app.db.base import Base

log = get_logger(__name__)

#: table -> column names that may be added to an existing table. Every entry must be
#: safe to apply to a populated one: nullable, or NOT NULL with a constant default.
ADDITIVE_COLUMNS: dict[str, tuple[str, ...]] = {
    "projects": (
        # Nullable: filled for every existing row by the accounts migration
        # (`app.db.accounts`), which runs right after this.
        "owner_id",
        "phase_started_at",
        "heartbeat_at",
        "cancel_requested",
        # Nullable (#41): a build with no driver holds no claim, and the first claim
        # after the upgrade writes one.
        "run_token",
        "last_error",
        # Both nullable (#63): a build that failed before failures had kinds has none,
        # and its page falls back to the plain `last_error`.
        "last_error_kind",
        "last_error_provider",
        "approval_mode",
        "cost_cap_usd",
        "gate_kind",
        "gate_note",
        "charter",
        "remediation_rounds",
        # Nullable: a run started before skills existed was told nothing about them,
        # and writing an empty override set into it would claim a choice nobody made.
        "skill_overrides",
        # Nullable: only a build paused for a user's computer names one.
        "paused_device_id",
        # Nullable: a build from before the fix loop has no rounds to report, and
        # its `remediation_rounds` count still says what it used.
        "auto_fix",
        # Nullable: a build from before the database question was never asked it,
        # and "later" would claim an answer nobody gave.
        "database_status",
        # Both nullable: a build from before #59 was never asked about connectors,
        # and an empty choice or status would claim an answer nobody gave.
        "integrations_choice",
        "integrations_status",
        # All nullable: a build from before #55 was never pushed or deployed from
        # here, and any value would claim somewhere it went.
        "github_repo",
        "github_branch",
        "github_pushed_at",
        "deploy_target",
        "deploy_url",
        "deploy_status",
        "deploy_id",
        "deployed_at",
        "deploy_error",
        # Nullable (#75): only a deploy that failed on Vercel since then has a log kept.
        "deploy_log",
        # Nullable (#78): a build whose preview was never opened remembers nothing about it.
        "preview_app",
        # All nullable (#79): a finished build from before versions gets its v1 the first
        # time it is read (`versions.ensure_first`), and nothing was deployed or pushed
        # as a version before then. The `versions` and `change_requests` tables are new,
        # so `create_all` makes them.
        "current_version_id",
        "deployed_version",
        "github_pushed_version",
        "deploying_version",
        "decisions",
    ),
    # Nullable (#79): a version recorded before it was counted is counted when listed.
    "versions": ("file_count",),
    "phase_results": (
        # Nullable (#63): only a phase that fell back past a refused key has one.
        "fallback_note",
        "started_at",
        "completed_at",
        "total_tokens",
        "latency_ms",
        "schema_status",
        "schema_note",
        "stack_status",
        "stack_note",
        # Nullable: a row written before the compile gate existed cannot say whether
        # its code built, and writing "ok" into it would claim a check nobody ran.
        "build_status",
        "build_note",
        # Nullable (#75): a row from before real builds was only parsed, and an empty
        # record would claim a build that never ran.
        "build_run",
        # Nullable (#76): a QA row from before tests were run never ran them, and an
        # empty record would claim a suite nobody executed.
        "test_run",
        # Nullable (#77): a Warden row from before the scanners never ran them, and an
        # empty record would claim a scan that found nothing.
        "scan",
        # Nullable for the same reason as `build_status`: a phase that ran before the
        # skill library existed cannot say which skills it had, and an empty list
        # would read as "it was offered skills and took none".
        "skills_used",
        # Nullable (#80): a phase that ran before hand-offs were recorded cannot say
        # what it was shown, and an empty record would claim it was shown nothing.
        "handoff",
        # Nullable: a row written before calls recorded where they ran cannot say,
        # and the API answers for it from the provider's name instead.
        "is_local",
    ),
    # Nullable for the same reason: a mockup drawn by the single-shot generator has
    # no pages, sections or checks to report.
    "preview_revisions": (
        "report",
        # Both nullable: a revision from before undo became a pointer has the one
        # before it as its parent, and with no head recorded the newest is live.
        "parent_id",
        "head_at",
        # Nullable (#78): a sketch from before can't say which Frontend attempt it shows.
        "built_from",
    ),
    # `cost_known` is nullable rather than defaulted to true: an existing row cannot
    # say whether its zero was a price or a gap, and claiming it was a price would
    # write a fact nobody checked into every historical event.
    "usage_events": ("cost_known", "is_local", "owner_id"),
    "knowledge_docs": ("owner_id",),
    # Both nullable: a finding settled before the fix loop was fixed by hand or not at
    # all, and a waiver recorded before reasons had kinds keeps its free-text reason.
    # All nullable (#77): a finding from before the scanners is the model's, and has
    # no tool, rule or line to record — the API reads a null source as "model".
    "security_dispositions": (
        "fixed_round",
        "waive_kind",
        "source",
        "tool",
        "rule_id",
        "rule_url",
        "cwe",
        "path",
        "line",
        "fingerprint",
        "rules",
    ),
}


def _add_column_sql(engine: Engine, table: str, name: str) -> Optional[str]:
    """`ALTER TABLE … ADD COLUMN …`, typed and defaulted for the connected dialect."""
    column = Base.metadata.tables[table].columns.get(name)
    if column is None:
        log.warning("Migration skipped: %s.%s is not on the model.", table, name)
        return None

    dialect = engine.dialect
    sql = f"ALTER TABLE {table} ADD COLUMN {name} {column.type.compile(dialect=dialect)}"

    if column.nullable:
        return sql

    # A NOT NULL column added to a populated table needs a default for the existing
    # rows. Render it through the type's own literal processor so each dialect gets
    # the spelling it accepts (`0` on SQLite, `false` on Postgres).
    default = getattr(column.default, "arg", None)
    if default is None or callable(default):
        log.warning(
            "Migration skipped: %s.%s is NOT NULL without a constant default.", table, name
        )
        return None
    try:
        literal = column.type.literal_processor(dialect=dialect)(default)
    except Exception as e:  # noqa: BLE001 - unsupported literal for this dialect
        log.warning("Migration skipped: cannot render default for %s.%s: %s", table, name, e)
        return None

    return f"{sql} DEFAULT {literal} NOT NULL"


def run_migrations(engine: Engine) -> list[str]:
    """Bring an existing database up to the current models. Returns what it applied."""
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    applied: list[str] = []

    for table, columns in ADDITIVE_COLUMNS.items():
        if table not in tables:
            continue  # create_all just made it with every column present
        existing = {c["name"] for c in inspector.get_columns(table)}
        for name in columns:
            if name in existing:
                continue
            statement = _add_column_sql(engine, table, name)
            if statement is None:
                continue
            try:
                with engine.begin() as conn:
                    conn.execute(text(statement))
                applied.append(f"{table}.{name}")
                log.info("Migration applied: %s", statement)
            except Exception as e:  # noqa: BLE001 - never block startup on a migration
                log.warning("Migration failed (%s): %s", statement, e)

    return applied
