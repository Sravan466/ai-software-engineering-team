"""The accounts migration: give everything that existed before accounts an owner.

Before accounts, every project, document and usage row belonged to whoever ran the
backend, and the settings lived in three global files. The first boot with accounts
hands all of that to one account — the install's owner — so nothing is lost and
nothing has to be set up again:

  1. `users.owner_id`-style columns are added by the additive migrations first
     (`app.db.migrations`); this indexes them, so reading "my projects" stays fast.
  2. If there is anything to adopt — a row with no owner, or a global settings file
     — and no account exists yet, one is created to hold it: the owner, with no
     email and no password. Nobody can sign in to it. The first person to set up
     the install claims it (`app.api.routes.auth`), and with it every existing build.
  3. Every row with no owner is given that account, in one transaction.
  4. The three global settings files are copied into the owner's directory and the
     originals renamed aside (never deleted), so a key saved before accounts keeps
     working for the owner and for nobody else.

It is safe to run on every boot. Rows are only adopted on the boot that creates the
owner: after that, a row with no owner is a bug to find, not something to hand to
whoever owns the install — so it is logged and left unreachable instead.
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path

from sqlalchemy import Engine, func, inspect, select, text, update
from sqlalchemy.orm import Session

from app.core import model_roles, model_settings, secrets_store, userdata
from app.core.atomic import write_private
from app.core.logging import get_logger
from app.db.models import KnowledgeDoc, Project, UsageEvent, User

log = get_logger(__name__)

#: Tables whose rows carry an owner, and the column that says who.
OWNED = (Project, KnowledgeDoc, UsageEvent)


def _legacy_files() -> list[tuple[Path, str]]:
    """(global file, its name in an account's directory), for each that exists."""
    pairs = [
        (secrets_store._PATH, "providers.local.json"),
        (model_roles._PATH, "model_roles.local.json"),
        (model_settings._PATH, "model_settings.local.json"),
    ]
    return [(src, name) for src, name in pairs if src.is_file()]


def _index_owner_columns(engine: Engine) -> None:
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    for model in OWNED:
        table = model.__tablename__
        if table not in tables:
            continue
        existing = {ix["name"] for ix in inspector.get_indexes(table)}
        name = f"ix_{table}_owner_id"
        if name in existing:
            continue
        try:
            with engine.begin() as conn:
                conn.execute(text(f"CREATE INDEX IF NOT EXISTS {name} ON {table} (owner_id)"))
        except Exception as e:  # noqa: BLE001 - an index is an optimisation, not a blocker
            log.warning("Could not index %s.owner_id: %s", table, e)


def _orphans(db: Session) -> int:
    return sum(
        db.execute(select(func.count()).select_from(model).where(model.owner_id.is_(None))).scalar_one()
        for model in OWNED
    )


def _move_settings_to(owner_id: str) -> list[str]:
    moved: list[str] = []
    stamp = time.strftime("%Y%m%d%H%M%S")
    for src, name in _legacy_files():
        dest = userdata.path(owner_id, name)
        if dest.exists():
            log.warning(
                "%s was left where it is: the owner already has their own %s.", src, name
            )
            continue
        write_private(dest, src.read_text(encoding="utf-8"))
        aside = src.with_name(f"{src.name}.moved-to-account-{stamp}")
        shutil.move(str(src), str(aside))
        moved.append(name)
        log.info("Moved %s into the owner's settings (the original is kept as %s).", src, aside)
    return moved


def migrate(engine: Engine) -> dict:
    """Bring a database from before accounts under one owner. Returns what it did."""
    _index_owner_columns(engine)
    report: dict = {"created_owner": False, "adopted": 0, "moved": []}
    with Session(engine) as db:
        has_users = db.execute(select(func.count()).select_from(User)).scalar_one() > 0
        orphans = _orphans(db)
        files = _legacy_files()
        if not has_users:
            if not orphans and not files:
                return report  # a fresh install: the first sign-up becomes the owner
            owner = User(is_owner=True, display_name="Owner")
            db.add(owner)
            db.flush()
            for model in OWNED:
                result = db.execute(
                    update(model).where(model.owner_id.is_(None)).values(owner_id=owner.id)
                )
                report["adopted"] += result.rowcount or 0
            db.commit()
            report["created_owner"] = True
            log.info(
                "Accounts: created the install's owner account and gave it %d existing rows. "
                "The first person to set up this install claims it.",
                report["adopted"],
            )
        elif orphans:
            log.warning(
                "Accounts: %d rows have no owner. They are not shown to anyone; this is a "
                "bug in whatever wrote them.",
                orphans,
            )
        owner = db.execute(
            select(User).where(User.is_owner.is_(True)).order_by(User.created_at)
        ).scalars().first()
        if owner is not None and files:
            report["moved"] = _move_settings_to(owner.id)
    return report
