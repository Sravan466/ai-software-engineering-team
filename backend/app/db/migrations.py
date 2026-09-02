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
        "phase_started_at",
        "heartbeat_at",
        "cancel_requested",
        "last_error",
        "approval_mode",
        "cost_cap_usd",
        "gate_kind",
        "gate_note",
    ),
    "phase_results": ("started_at", "completed_at", "total_tokens", "latency_ms"),
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
