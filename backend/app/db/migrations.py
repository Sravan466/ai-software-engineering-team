"""Additive, idempotent schema migrations.

The project has no migration framework: `init_db()` calls `Base.metadata.create_all`,
which creates missing *tables* but never touches an existing one. So a column added to
a model after someone already has a database is simply absent at runtime, and every
query against it fails.

This module closes that gap for the only kind of change made here — adding a nullable
column, or one with a constant default. Each statement is checked against the live
schema first, so running it repeatedly (every boot) is a no-op, and it degrades to a
warning rather than taking the app down if a statement is rejected.

Anything destructive — dropping or retyping a column, backfilling with a query — does
not belong here. That needs a real migration tool.
"""
from __future__ import annotations

from sqlalchemy import Engine, inspect, text

from app.core.logging import get_logger

log = get_logger(__name__)

#: table -> [(column, DDL type + default)]. Every entry must be safe to apply to a
#: populated table: nullable, or NOT NULL with a constant DEFAULT.
ADDITIVE_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "projects": [
        ("phase_started_at", "TIMESTAMP NULL"),
        ("heartbeat_at", "TIMESTAMP NULL"),
        ("cancel_requested", "BOOLEAN NOT NULL DEFAULT 0"),
        ("last_error", "TEXT NULL"),
    ],
    "phase_results": [
        ("started_at", "TIMESTAMP NULL"),
        ("completed_at", "TIMESTAMP NULL"),
        ("total_tokens", "INTEGER NOT NULL DEFAULT 0"),
        ("latency_ms", "INTEGER NOT NULL DEFAULT 0"),
    ],
}


def run_migrations(engine: Engine) -> list[str]:
    """Bring an existing database up to the current models. Returns what it applied."""
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    applied: list[str] = []

    for table, columns in ADDITIVE_COLUMNS.items():
        if table not in tables:
            continue  # create_all just made it with every column present
        existing = {c["name"] for c in inspector.get_columns(table)}
        for name, ddl in columns:
            if name in existing:
                continue
            statement = f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"
            try:
                with engine.begin() as conn:
                    conn.execute(text(statement))
                applied.append(f"{table}.{name}")
                log.info("Migration applied: %s", statement)
            except Exception as e:  # noqa: BLE001 - never block startup on a migration
                log.warning("Migration failed (%s): %s", statement, e)

    return applied
