"""Shared API dependencies."""
from __future__ import annotations

from fastapi import Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.base import get_db
from app.db.models import Project


def get_project(project_id: str, db: Session = Depends(get_db)) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")
    return project
