from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.user import User


class Endpoint(Base):
    __tablename__ = "endpoints"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    base_url: Mapped[str] = mapped_column(String(512), nullable=False)
    api_key: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    api_base_path: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="", server_default="")
    default_model: Mapped[str] = mapped_column(String(128), nullable=False, default="", server_default="")
    bypass_method: Mapped[str] = mapped_column(String(32), nullable=False, default="none")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    role_tag: Mapped[str] = mapped_column(
        String(32), nullable=False, default="default", server_default="default"
    )
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    custom_tag: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", server_default=""
    )
    parameters_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]", server_default="[]"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    user: Mapped[User] = relationship(back_populates="endpoints")
    # Eager by default. Routing loads endpoints and then closes the session
    # before any upstream call, so a lazy collection would raise MissingGreenlet
    # exactly where per-model parameters need to be read -- on the proxy hot
    # path. One extra SELECT per endpoint query is the right trade.
    models: Mapped[list[EndpointModel]] = relationship(
        back_populates="endpoint",
        cascade="all, delete-orphan",
        order_by="EndpointModel.sort_order",
        lazy="selectin",
    )


class EndpointModel(Base):
    """A curated model on an endpoint.

    Model discovery via `GET /api/endpoints/{id}/models` is a live probe and
    stays that way; this is the user's chosen subset, which is what the model
    dropdowns are built from and what per-model parameters hang off. A model
    named here is not required to exist upstream, and a model absent from here
    can still be used -- every model field keeps a free-text escape hatch.
    """

    __tablename__ = "endpoint_models"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    endpoint_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("endpoints.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(
        String(512), nullable=False, default="", server_default=""
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    parameters_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]", server_default="[]"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    endpoint: Mapped[Endpoint] = relationship(back_populates="models")
