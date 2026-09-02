from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.user import User


class ResourceSnapshot(Base):
    """One stored version of one user-authored resource (Phase 25).

    `resource_id` carries no foreign key on purpose: the snapshot taken just
    before a delete is exactly the one a user needs afterwards, so it must
    outlive the row it describes. `resource_name` is denormalised for the same
    reason -- a deleted object's history has to stay readable.

    `pre_edit` snapshots are taken automatically in the routers' update and
    delete paths, which is what gives assistant writes an undo without any
    assistant-specific code. `manual` snapshots are pinned by the user and are
    never pruned, the model `DebugExchange.saved` uses for debug runs.
    """

    __tablename__ = "resource_snapshots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # One of app.services.snapshots.SNAPSHOT_TYPES; matches the deeplink
    # vocabulary in frontend/src/lib/deeplink.ts so a row can link to its object.
    resource_type: Mapped[str] = mapped_column(String(32), nullable=False, default="", server_default="")
    resource_id: Mapped[str] = mapped_column(String(36), nullable=False, default="", server_default="", index=True)
    resource_name: Mapped[str] = mapped_column(String(128), nullable=False, default="", server_default="")

    # sha256 over content_json itself, not resource_identity.content_hash --
    # that one deliberately ignores description, tag and the activation flags,
    # so it cannot answer "did this version change?".
    content_hash: Mapped[str] = mapped_column(String(71), nullable=False, default="", server_default="")
    content_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}", server_default="{}")

    source: Mapped[str] = mapped_column(String(16), nullable=False, default="pre_edit", server_default="pre_edit")
    label: Mapped[str] = mapped_column(String(128), nullable=False, default="", server_default="")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(UTC), server_default=text("CURRENT_TIMESTAMP"),
    )

    user: Mapped[User] = relationship()
