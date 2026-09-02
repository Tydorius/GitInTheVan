"""The per-turn context handed to every assistant module.

Built once in the router, where the request still exists, and carried through
the loop. It holds the caller's raw bearer token because the executor reaches
the management API in-process as that caller -- there is no service account and
no privilege the user does not already have.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolContext:
    """Everything a tool needs, and nothing a tool must not have.

    `admin` is a detached snapshot of AdminSettings read before the stream
    started, so no DB session is held open for the life of the response.
    `secrets` holds the user's live endpoint API keys, used only to scrub them
    back out of tool results.
    """

    user_id: str
    is_admin: bool
    bearer_token: str
    app: Any
    conversation_id: str
    admin: Any
    secrets: tuple[str, ...] = ()
    route: dict[str, Any] = field(default_factory=dict)
