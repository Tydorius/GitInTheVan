import pytest
from sqlalchemy import select

from app.models.conversation_hash import ConversationHash
from app.services.conversation import (
    _prior_messages_for_hash,
    compute_hash,
    record_post_hash,
    resolve_conversation,
)
from tests.conftest import TestSessionLocal

USER = "user-conv"


def _msg(role: str, content: str) -> dict:
    return {"role": role, "content": content}


SYS = _msg("system", "You are a character.")
U1 = _msg("user", "Hello there.")
A1 = _msg("assistant", "Hi! How can I help?")
U2 = _msg("user", "Tell me a story.")
A2 = _msg("assistant", "Once upon a time...")
U3 = _msg("user", "Continue.")


# ============================================================================
# _prior_messages_for_hash slicing
# ============================================================================

class TestPriorMessagesForHash:
    def test_excludes_current_user_and_preceding_assistant(self):
        msgs = [SYS, U1, A1, U2, A2, U3]
        assert _prior_messages_for_hash(msgs) == [SYS, U1, A1, U2]

    def test_excludes_only_last_when_preceding_not_assistant(self):
        msgs = [SYS, U1, U2]
        assert _prior_messages_for_hash(msgs) == [SYS, U1]

    def test_two_messages(self):
        assert _prior_messages_for_hash([SYS, U1]) == [SYS]

    def test_empty(self):
        assert _prior_messages_for_hash([]) == []

    def test_single_message(self):
        assert _prior_messages_for_hash([U1]) == []

    def test_trailing_assistant_only(self):
        msgs = [SYS, U1, A1]
        assert _prior_messages_for_hash(msgs) == [SYS, U1]


# ============================================================================
# resolve_conversation: chaining and edit tolerance
# ============================================================================

class TestResolveConversation:
    @pytest.mark.asyncio
    async def test_new_conversation_when_no_history(self):
        chat_id, is_new = await resolve_conversation([SYS, U1], USER)
        assert is_new is True
        assert chat_id

    @pytest.mark.asyncio
    async def test_chains_across_turns(self):
        request1 = [SYS, U1]
        chat_id, is_new = await resolve_conversation(request1, USER)
        assert is_new is True

        await record_post_hash(request1, A1["content"], chat_id, USER)

        request2 = [SYS, U1, A1, U2]
        chat_id2, is_new2 = await resolve_conversation(request2, USER)
        assert is_new2 is False
        assert chat_id2 == chat_id

    @pytest.mark.asyncio
    async def test_tolerates_edit_of_last_assistant_message(self):
        request1 = [SYS, U1]
        chat_id, _ = await resolve_conversation(request1, USER)
        await record_post_hash(request1, A1["content"], chat_id, USER)

        a1_edited = _msg("assistant", "Completely different edited response!")
        request2 = [SYS, U1, a1_edited, U2]
        chat_id2, is_new2 = await resolve_conversation(request2, USER)

        assert is_new2 is False, "Edited LLM response must not break conversation chain"
        assert chat_id2 == chat_id

    @pytest.mark.asyncio
    async def test_chains_multiple_turns(self):
        r1 = [SYS, U1]
        cid, _ = await resolve_conversation(r1, USER)
        await record_post_hash(r1, A1["content"], cid, USER)

        r2 = [SYS, U1, A1, U2]
        cid2, is_new2 = await resolve_conversation(r2, USER)
        assert cid2 == cid and is_new2 is False
        await record_post_hash(r2, A2["content"], cid, USER)

        r3 = [SYS, U1, A1, U2, A2, U3]
        cid3, is_new3 = await resolve_conversation(r3, USER)
        assert cid3 == cid and is_new3 is False

    @pytest.mark.asyncio
    async def test_new_conversation_when_no_match(self):
        chat_id, is_new = await resolve_conversation([SYS, U1, A1, U2], USER)
        assert is_new is True

    @pytest.mark.asyncio
    async def test_fork_by_editing_older_message_creates_new_chain(self):
        r1 = [SYS, U1]
        cid_main, _ = await resolve_conversation(r1, USER)
        await record_post_hash(r1, A1["content"], cid_main, USER)

        r2 = [SYS, U1, A1, U2]
        assert (await resolve_conversation(r2, USER))[0] == cid_main
        await record_post_hash(r2, A2["content"], cid_main, USER)

        r3 = [SYS, U1, A1, U2, A2, U3]
        assert (await resolve_conversation(r3, USER))[0] == cid_main
        await record_post_hash(r3, _msg("assistant", "r3 bot"), cid_main, USER)

        a1_prime = _msg("assistant", "A rewritten older LLM response.")
        fork_req = [SYS, U1, a1_prime, U2, A2, U3]
        cid_fork, is_new_fork = await resolve_conversation(fork_req, USER)
        assert is_new_fork is True
        assert cid_fork != cid_main
        await record_post_hash(fork_req, _msg("assistant", "fork bot"), cid_fork, USER)

        fork_cont = [SYS, U1, a1_prime, U2, A2, U3, _msg("assistant", "fork bot"), _msg("user", "more")]
        cid_fork2, is_new2 = await resolve_conversation(fork_cont, USER)
        assert is_new2 is False
        assert cid_fork2 == cid_fork


# ============================================================================
# Legacy fallback: old-scheme hashes (with bot response) still resolve
# ============================================================================

class TestLegacyFallback:
    @pytest.mark.asyncio
    async def test_old_scheme_hash_resolves_via_fallback(self):
        legacy_chat_id = "legacy-chat-123"
        legacy_request = [SYS, U1]
        legacy_bot = A1["content"]
        legacy_hash = compute_hash(legacy_request + [_msg("assistant", legacy_bot)], 4)

        async with TestSessionLocal() as db:
            db.add(ConversationHash(
                user_id=USER, hash_value=legacy_hash, internal_chat_id=legacy_chat_id,
            ))
            await db.commit()

        next_request = [SYS, U1, A1, U2]
        resolved, is_new = await resolve_conversation(next_request, USER)
        assert is_new is False
        assert resolved == legacy_chat_id

    @pytest.mark.asyncio
    async def test_new_scheme_preferred_over_legacy(self):
        chat_new = "new-scheme-chat"
        chat_legacy = "legacy-scheme-chat"

        request1 = [SYS, U1]
        new_hash = compute_hash(request1, 4)
        legacy_hash = compute_hash(request1 + [A1], 4)

        async with TestSessionLocal() as db:
            db.add(ConversationHash(user_id=USER, hash_value=new_hash, internal_chat_id=chat_new))
            db.add(ConversationHash(user_id=USER, hash_value=legacy_hash, internal_chat_id=chat_legacy))
            await db.commit()

        next_request = [SYS, U1, A1, U2]
        resolved, _ = await resolve_conversation(next_request, USER)
        assert resolved == chat_new


# ============================================================================
# record_post_hash: stores request-message hash, dedups
# ============================================================================

class TestRecordPostHash:
    @pytest.mark.asyncio
    async def test_records_request_hash(self):
        request = [SYS, U1]
        h = await record_post_hash(request, A1["content"], "chat-rec", USER)
        assert h == compute_hash(request, 4)

        async with TestSessionLocal() as db:
            rows = (await db.execute(
                select(ConversationHash).where(
                    ConversationHash.user_id == USER
                )
            )).scalars().all()
            assert len(rows) == 1
            assert rows[0].internal_chat_id == "chat-rec"

    @pytest.mark.asyncio
    async def test_does_not_duplicate_existing_hash(self):
        request = [SYS, U1]
        await record_post_hash(request, A1["content"], "chat-dedup", USER)
        await record_post_hash(request, "different bot text", "chat-dedup", USER)

        async with TestSessionLocal() as db:
            rows = (await db.execute(
                select(ConversationHash).where(
                    ConversationHash.user_id == USER
                )
            )).scalars().all()
            assert len(rows) == 1
