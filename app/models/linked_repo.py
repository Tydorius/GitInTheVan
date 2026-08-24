from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class LinkedRepo(Base):
    __tablename__ = "linked_repos"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    branch: Mapped[str] = mapped_column(String(128), nullable=False, default="main")
    token: Mapped[str] = mapped_column(Text, nullable=False, default="")
    is_local: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default="0")
    is_global: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default="0")
    last_synced: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    descriptions_cache: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )


class InstalledItem(Base):
    __tablename__ = "installed_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    repo_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("linked_repos.id", ondelete="CASCADE"), nullable=True
    )
    file_path: Mapped[str] = mapped_column(Text, nullable=False)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    author: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    installed_version: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    installed_commit: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    local_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    # Provenance. repo_id alone cannot identify a resource across installs (it is
    # a local UUID) and is CASCADE-deleted when a repo is unlinked, so the
    # normalized URL is stored alongside it. file_path may address a resource
    # inside a file -- "maps/pipeline.json:Dice Controller" -- for resources a
    # pack ships only as part of a map.
    source_url: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    content_hash: Mapped[str] = mapped_column(String(80), default="", server_default="", nullable=False)
    # Set on resources pulled in as part of a parent install (a map's stage
    # resources). Empty for anything the user installed directly.
    parent_item_id: Mapped[str] = mapped_column(String(36), default="", server_default="", nullable=False)
    # "embedded" (content shipped inside the parent) or "linked" (resolved from
    # the repo at install time, so it tracks upstream).
    link_mode: Mapped[str] = mapped_column(String(16), default="embedded", server_default="embedded", nullable=False)

    is_fork: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    scan_result: Mapped[str] = mapped_column(Text, nullable=False, default="")
    update_available: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False,
        onupdate=lambda: datetime.now(UTC),
    )
