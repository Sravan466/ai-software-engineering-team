"""Shared API dependencies: the signed-in account, and what it may reach.

Everything is looked up *through* its owner. Another account's project, document or
source is answered exactly as one that doesn't exist — a 404, never a 403 — so an
id can't be probed to learn that something is there.
"""
from __future__ import annotations

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.db.base import get_db
from app.db.models import Project, User
from app.router.router import ModelRouter, routers


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    """The signed-in account. The auth middleware has already refused anyone else."""
    user_id = getattr(request.state, "user_id", None)
    user = db.get(User, user_id) if user_id else None
    if user is None or not user.claimed:
        raise HTTPException(status_code=401, detail="Sign in to continue.")
    return user


def current_router(user: User = Depends(current_user)) -> ModelRouter:
    """The signed-in account's own router: its keys, its sources, its choices."""
    return routers.for_user(user.id)


def get_project(
    project_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Project:
    project = db.get(Project, project_id)
    if project is None or project.owner_id != user.id:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")
    return project


def require_owner(user: User = Depends(current_user)) -> User:
    """For what is shared by every account on this install — the skill library."""
    if not user.is_owner:
        raise HTTPException(
            status_code=403,
            detail="Only the account that owns this install can change this. It's shared by everyone here.",
        )
    return user
