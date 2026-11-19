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


# ============================================================================
# Conversation continuity under prompt injection (0.36.0 regression fix)
# ============================================================================


class TestContinuityUnderInjection:
    """0.33.0 regression: the post-hash anchor was captured AFTER lorebook
    injection and cantrips, so any injection landing inside the 4-message
    hash window (a before_last_message entry, writing samples) made the next
    request resolve as a brand-new conversation -- chat_data, memories and
    summaries silently lost every turn. The anchor is now captured from the
    pre-injection message list, exactly what resolve_conversation hashes.
    """

    async def test_before_last_message_injection_does_not_orphan_the_chat(
        self, admin_client, httpx_mock
    ):
        import json as _json

        client, _, gitv_key = admin_client
        resp = await client.post("/api/endpoints", json={
            "name": "Cont", "base_url": "http://continuity.test", "api_key": "sk-test",
        })
        assert resp.status_code in (200, 201)

        lb_resp = await client.post("/api/lorebooks", json={
            "name": "Tail Lore", "is_active": True,
        })
        lb_id = lb_resp.json()["id"]
        await client.post(f"/api/lorebooks/{lb_id}/entries", json={
            "name": "AlwaysHere",
            "keys": ["meadow"],
            "content": "The meadow smells of rain.",
            "position": "before_last_message",
            "insertion_order": 10,
            "is_constant": False,
            "is_selective": False,
        })

        def _upstream(content: str) -> dict:
            return {
                "id": "chatcmpl-test", "object": "chat.completion", "created": 1,
                "model": "test-model",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                             "finish_reason": "stop"}],
            }

        # Turn 1: the reply carries a memory write. The client sees the
        # cleaned text, so turn 2's history contains the cleaned assistant
        # message -- exactly like a real client.
        reply1 = ("<memstore key=\"flower\">a red tulip</memstore>\n"
                  "You walk into the meadow.")
        httpx_mock.add_response(url="http://continuity.test/v1/chat/completions",
                                json=_upstream(reply1))
        turn1 = {
            "model": "test-model",
            "messages": [
                {"role": "system", "content": "You are a narrator."},
                {"role": "user", "content": "We walk to the meadow."},
            ],
            "stream": False,
        }
        resp1 = await client.post("/v1/chat/completions", json=turn1,
                                  headers={"Authorization": f"Bearer {gitv_key}"})
        assert resp1.status_code == 200
        # The entry injected inside the hash window on turn 1 (proof the
        # bug's precondition held in this run).
        sent1 = _json.loads(httpx_mock.get_requests()[0].content)
        assert "The meadow smells of rain." in _json.dumps(sent1["messages"])

        # Turn 2: same conversation, next user message. If the anchor
        # matched, the memory written in turn 1 is injected into this
        # request as [PERSISTENT MEMORY] -- the observable that the chat
        # was NOT orphaned.
        httpx_mock.add_response(url="http://continuity.test/v1/chat/completions",
                                json=_upstream("The tulip is still there."))
        turn2 = {
            "model": "test-model",
            "messages": [
                {"role": "system", "content": "You are a narrator."},
                {"role": "user", "content": "We walk to the meadow."},
                {"role": "assistant", "content": "You walk into the meadow."},
                {"role": "user", "content": "What do I see?"},
            ],
            "stream": False,
        }
        resp2 = await client.post("/v1/chat/completions", json=turn2,
                                  headers={"Authorization": f"Bearer {gitv_key}"})
        assert resp2.status_code == 200

        sent2 = _json.loads(httpx_mock.get_requests()[-1].content)
        sent2_text = _json.dumps(sent2["messages"])
        assert "a red tulip" in sent2_text, (
            "Memory from turn 1 missing: the conversation was orphaned by "
            "in-window injection (the 0.33.0 regression)"
        )
