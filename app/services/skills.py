import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.skill import EndpointSkill, Skill

logger = logging.getLogger(__name__)


async def load_skills_for_endpoint(
    endpoint_id: str | None, user_id: str, db: AsyncSession
) -> tuple[list[str], list[str]]:
    """Load skill and sample content blocks for a given endpoint.

    Returns (skill_contents, sample_contents) — lists of text blocks to inject.
    """
    if not endpoint_id:
        return [], []

    result = await db.execute(
        select(Skill)
        .join(EndpointSkill, EndpointSkill.skill_id == Skill.id)
        .where(EndpointSkill.endpoint_id == endpoint_id, Skill.user_id == user_id)
        .order_by(Skill.created_at)
    )
    items = result.scalars().all()

    skills = [s.content for s in items if s.type == "skill" and s.content.strip()]
    samples = [s.content for s in items if s.type == "sample" and s.content.strip()]

    if skills:
        logger.debug("Loaded %d skill(s) for endpoint %s", len(skills), endpoint_id)
    if samples:
        logger.debug("Loaded %d sample(s) for endpoint %s", len(samples), endpoint_id)

    return skills, samples


def inject_skills(messages: list[dict], skill_contents: list[str]) -> list[dict]:
    """Append skill instructions to the system message.

    Skills go into the system message alongside character definition and lorebooks.
    """
    if not skill_contents:
        return messages

    combined = "\n\n".join(skill_contents)
    wrapped = f"<skills>\n{combined}\n</skills>"

    if messages and messages[0].get("role") == "system":
        messages[0]["content"] = messages[0]["content"] + "\n\n" + wrapped
    else:
        messages.insert(0, {"role": "system", "content": wrapped})

    return messages


def inject_samples(messages: list[dict], sample_contents: list[str]) -> list[dict]:
    """Insert writing samples as a system message before the last user message.

    Samples go near the end of context so the style reference is fresh.
    """
    if not sample_contents:
        return messages

    combined = "\n\n---\n\n".join(sample_contents)
    wrapped = f"<writing_sample>\n{combined}\n</writing_sample>"

    insert_index = len(messages)
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "user":
            insert_index = i
            break

    messages.insert(insert_index, {"role": "system", "content": wrapped})
    return messages
