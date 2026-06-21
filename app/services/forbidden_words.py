from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models.forbidden_word import ForbiddenWord
from app.models.user_settings import UserSettings

logger = logging.getLogger(__name__)


@dataclass
class ForbiddenMatch:
    phrase: str
    count: int
    positions: list[int]


@dataclass
class ForbiddenScanResult:
    matches: list[ForbiddenMatch]

    @property
    def has_matches(self) -> bool:
        return len(self.matches) > 0

    @property
    def summary(self) -> str:
        if not self.matches:
            return ""
        lines = ["[FORBIDDEN CONTENT DETECTED]"]
        lines.append("The following forbidden phrases were found in the response:")
        for m in self.matches:
            pos_str = ", ".join(str(p) for p in m.positions[:5])
            if len(m.positions) > 5:
                pos_str += f" (+{len(m.positions) - 5} more)"
            lines.append(f'- "{m.phrase}" ({m.count} occurrence(s) at: {pos_str})')
        lines.append("[/FORBIDDEN CONTENT DETECTED]")
        return "\n".join(lines)


async def load_forbidden_words(db: AsyncSession, user_id: str) -> list[ForbiddenWord]:
    result = await db.execute(
        select(ForbiddenWord)
        .where(ForbiddenWord.user_id == user_id)
        .order_by(ForbiddenWord.created_at)
    )
    return list(result.scalars().all())


async def scan_response(
    content: str, user_id: str
) -> ForbiddenScanResult:
    async with async_session() as db:
        settings_result = await db.execute(
            select(UserSettings).where(UserSettings.user_id == user_id)
        )
        settings = settings_result.scalar_one_or_none()
        if not settings or not settings.forbidden_words_enabled:
            return ForbiddenScanResult(matches=[])

        words = await load_forbidden_words(db, user_id)
        if not words:
            return ForbiddenScanResult(matches=[])

        case_sensitive = settings.forbidden_words_case_sensitive
        return _scan(content, words, case_sensitive)


def _scan(
    content: str, words: list[ForbiddenWord], case_sensitive: bool
) -> ForbiddenScanResult:
    matches: list[ForbiddenMatch] = []

    for word in words:
        phrase = word.phrase.strip()
        if not phrase:
            continue

        if word.is_regex:
            try:
                flags = 0 if case_sensitive else re.IGNORECASE
                positions = [m.start() for m in re.finditer(phrase, content, flags)]
            except re.error:
                logger.warning("Invalid regex in forbidden word: %s", phrase[:80])
                continue
        else:
            search_content = content if case_sensitive else content.lower()
            search_phrase = phrase if case_sensitive else phrase.lower()
            positions = []
            start = 0
            while True:
                idx = search_content.find(search_phrase, start)
                if idx == -1:
                    break
                positions.append(idx)
                start = idx + len(search_phrase)

        if positions:
            matches.append(ForbiddenMatch(
                phrase=phrase, count=len(positions), positions=positions,
            ))

    return ForbiddenScanResult(matches=matches)
