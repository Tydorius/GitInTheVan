"""The Activation Hierarchy, pinned per resource type.

This file is the executable form of the README's "Activation Hierarchy"
section. It exists because the
hierarchy was previously implicit, and four of the five resource types quietly
disagreed with it and with each other: `map` and `memory-rule` were not even
recognised tag types, and the cantrip, map and memory-rule loaders filtered
`is_active` in SQL, so a tag could never switch on a resource that was off --
which is the entire point of tagging.

The rules, restated:

1. Off is the default.
2. Activation is a union. Nothing deactivates.
3. Proximity wins: a tag in the message activates regardless of the Active flag.
4. The Active flag is the blanket source, used when no tag has spoken.

Maps and memory rules are the documented exception to rule 4 only: exactly one
can win, so their blanket source is an explicit single-valued setting rather
than a per-resource Active flag. They still obey rules 1-3.
"""

from __future__ import annotations

import pytest

from app.services.tagging import (
    TAG_TYPES,
    extract_all_tags_from_messages,
    parse_tag,
    should_activate_resource,
    tag_matches_resource,
)
from tests.conftest import TestSessionLocal

OWNER = "user-1"
OTHER = "user-2"


def tags_for(text: str) -> list[dict]:
    return extract_all_tags_from_messages([{"role": "user", "content": text}])


def activate(resource_tag, resource_type, is_active, text, *, is_public=False, owner=OWNER):
    return should_activate_resource(
        resource_tag, resource_type, is_active, is_public, owner, OWNER, tags_for(text)
    )


class TestTagParsing:
    """Every addressable resource type must round-trip through parse_tag.

    A type missing from TAG_TYPES parses as "unknown" and its tags silently
    match nothing, which is how map and memory-rule tags came to do nothing.
    """

    @pytest.mark.parametrize("raw,expected_type,expected_name", [
        ("lore-world", "lore", "world"),
        ("cantrip-dice", "cantrip", "dice"),
        ("verify-tone", "verify", "tone"),
        ("taggroup-combat", "taggroup", "combat"),
        ("map-overthink", "map", "overthink"),
        ("memory-rule-longform", "memory-rule", "longform"),
    ])
    def test_each_type_parses(self, raw, expected_type, expected_name):
        parsed = parse_tag(raw)
        assert parsed["type"] == expected_type
        assert parsed["name"] == expected_name
        assert parsed["owner"] is None

    def test_hyphenated_names_survive(self):
        assert parse_tag("map-gambling-hall")["name"] == "gambling-hall"
        assert parse_tag("cantrip-dice-roller-v2")["name"] == "dice-roller-v2"

    def test_a_multi_word_type_beats_a_shorter_prefix(self):
        """`memory-rule-x` must not parse as some other type named `memory`."""
        assert parse_tag("memory-rule-x")["type"] == "memory-rule"

    def test_owner_prefixed_tags(self):
        parsed = parse_tag("alice-cantrip-dice")
        assert parsed == {"type": "cantrip", "name": "dice", "owner": "alice"}

    def test_an_unknown_type_matches_nothing(self):
        assert parse_tag("nonsense-thing")["type"] == "unknown"
        assert not activate("thing", "cantrip", False, "<#nonsense-thing#>")

    def test_every_declared_type_is_parseable(self):
        """Guards against a type being added to TAG_TYPES in a form parse_tag
        cannot actually produce."""
        for resource_type in TAG_TYPES:
            assert parse_tag(f"{resource_type}-example")["type"] == resource_type


class TestRuleOneOffIsTheDefault:
    def test_an_inactive_untagged_resource_never_activates(self):
        assert not activate("", "cantrip", False, "plain message")

    def test_an_inactive_tagged_resource_does_not_activate_without_its_tag(self):
        assert not activate("dice", "cantrip", False, "plain message")

    def test_an_unrelated_tag_does_not_activate_it(self):
        assert not activate("dice", "cantrip", False, "<#cantrip-weather#>")

    def test_a_tag_of_a_different_type_does_not_activate_it(self):
        """`<#lore-dice#>` must not activate a *cantrip* named dice."""
        assert not activate("dice", "cantrip", False, "<#lore-dice#>")


class TestRuleThreeProximityWins:
    """A tag in the message is the most immediate expression of intent."""

    @pytest.mark.parametrize("resource_type,tag_text", [
        ("lore", "<#lore-world#>"),
        ("cantrip", "<#cantrip-world#>"),
        ("verify", "<#verify-world#>"),
    ])
    def test_a_matching_tag_activates_an_inactive_resource(self, resource_type, tag_text):
        assert activate("world", resource_type, False, tag_text)

    def test_a_group_tag_activates_members(self):
        """The group resolver rewrites group tags into typed ones, but a raw
        taggroup tag is honoured here too so ordering cannot matter."""
        assert activate("dice", "cantrip", False, "<#taggroup-dice#>")

    def test_a_tag_in_any_message_counts(self):
        """Persona, system prompt and message text are all just messages by the
        time activation runs."""
        tags = extract_all_tags_from_messages([
            {"role": "system", "content": "You are a bard. <#cantrip-dice#>"},
            {"role": "user", "content": "roll for me"},
        ])
        assert should_activate_resource("dice", "cantrip", False, False, OWNER, OWNER, tags)


class TestRuleFourActiveIsTheBlanketSource:
    def test_an_active_resource_runs_with_no_tags_present(self):
        assert activate("", "cantrip", True, "plain message")

    def test_an_active_tagged_resource_still_runs_without_its_tag(self):
        """Rule 2: a tag adds a way to activate; it never takes one away."""
        assert activate("dice", "cantrip", True, "plain message")

    def test_an_active_resource_runs_when_an_unrelated_tag_is_present(self):
        assert activate("dice", "cantrip", True, "<#cantrip-weather#>")


class TestRuleTwoUnionNeverSubtracts:
    @pytest.mark.parametrize("is_active", [True, False])
    @pytest.mark.parametrize("text", [
        "plain",
        "<#cantrip-dice#>",
        "<#cantrip-other#>",
        "<#taggroup-dice#>",
    ])
    def test_adding_a_matching_tag_never_turns_a_resource_off(self, is_active, text):
        """For any state, adding the resource's own tag can only move the answer
        toward True -- never away from it."""
        without = activate("dice", "cantrip", is_active, text)
        with_tag = activate("dice", "cantrip", is_active, text + " <#cantrip-dice#>")
        assert with_tag >= without
        assert with_tag is True


class TestOwnershipAndSharing:
    def test_another_users_private_resource_is_not_activated_by_your_tag(self):
        assert not activate("dice", "cantrip", False, "<#cantrip-dice#>", owner=OTHER)

    def test_another_users_public_resource_is_activated(self):
        assert activate("dice", "cantrip", False, "<#cantrip-dice#>",
                        is_public=True, owner=OTHER)

    def test_an_owner_prefixed_tag_addresses_another_users_resource(self):
        assert activate("dice", "cantrip", False, "<#bob-cantrip-dice#>", owner=OTHER)


class TestSelectionResources:
    """Maps and memory rules: one wins, so Active cannot also mean blanket.

    Their blanket source is an explicit single-valued setting instead --
    `user_settings.default_map_id`, or the untagged default memory rule.
    """

    def test_a_matching_tag_selects_regardless_of_active(self):
        assert tag_matches_resource("overthink", "map", False, OWNER, OWNER,
                                    tags_for("<#map-overthink#>"))

    def test_an_unrelated_tag_does_not_select_a_map(self):
        """The bug this replaces: an active tagged map was selected by *any*
        tag in the request, including an unrelated cantrip tag."""
        assert not tag_matches_resource("overthink", "map", False, OWNER, OWNER,
                                        tags_for("<#cantrip-dice#>"))

    def test_no_tags_selects_nothing(self):
        assert not tag_matches_resource("overthink", "map", False, OWNER, OWNER,
                                        tags_for("plain message"))

    def test_an_untagged_map_is_never_selected_by_a_tag(self):
        assert not tag_matches_resource("", "map", False, OWNER, OWNER,
                                        tags_for("<#map-overthink#>"))

    def test_ownership_still_applies(self):
        assert not tag_matches_resource("overthink", "map", False, OTHER, OWNER,
                                        tags_for("<#map-overthink#>"))
        assert tag_matches_resource("overthink", "map", True, OTHER, OWNER,
                                    tags_for("<#map-overthink#>"))


@pytest.mark.asyncio
class TestLoadersDoNotPreEmptTheHierarchy:
    """A loader that filters `is_active` in SQL removes rule 3 entirely: a
    resource excluded before the question is asked can never be activated by its
    tag. These pin that each loader returns candidates, not decisions.
    """

    async def _user(self, client) -> str:
        return (await client.get("/api/auth/me")).json()["id"]

    async def test_cantrip_loader_returns_inactive_tagged_candidates(self, admin_client):
        from app.models.cantrip import Cantrip
        from app.services.cantrip import _load_active_cantrips

        client, _, _ = admin_client
        uid = await self._user(client)

        async with TestSessionLocal() as db:
            db.add(Cantrip(user_id=uid, name="Tagged Off", code="",
                           tag="dice", is_active=False, run_pre_driver=True))
            db.add(Cantrip(user_id=uid, name="Untagged Off", code="",
                           tag="", is_active=False, run_pre_driver=True))
            await db.commit()

        async with TestSessionLocal() as db:
            loaded = {c.name for c in await _load_active_cantrips(db, uid, "pre_driver")}

        assert "Tagged Off" in loaded, "a tagged inactive cantrip must reach the tag check"
        assert "Untagged Off" not in loaded, "an untagged inactive cantrip cannot be activated"

    async def test_map_loader_returns_inactive_tagged_candidates(self, admin_client):
        from app.models.map import Map
        from app.services.map_pipeline import resolve_map

        client, _, _ = admin_client
        uid = await self._user(client)

        async with TestSessionLocal() as db:
            db.add(Map(user_id=uid, name="Tagged Off", tag="overthink", is_active=False))
            await db.commit()

        selected = await resolve_map(uid, tags_for("<#map-overthink#>"))
        assert selected is not None and selected.name == "Tagged Off"

    async def test_an_unrelated_tag_selects_no_map(self, admin_client):
        from app.models.map import Map
        from app.services.map_pipeline import resolve_map

        client, _, _ = admin_client
        uid = await self._user(client)

        async with TestSessionLocal() as db:
            db.add(Map(user_id=uid, name="Active Tagged", tag="something", is_active=True))
            await db.commit()

        assert await resolve_map(uid, tags_for("<#cantrip-dice#>")) is None
        assert await resolve_map(uid, tags_for("no tags")) is None

    async def test_memory_rule_loader_returns_inactive_tagged_candidates(self, admin_client):
        from app.models.memory_rule import MemoryRule
        from app.services.summarization import resolve_memory_rule

        client, _, _ = admin_client
        uid = await self._user(client)

        async with TestSessionLocal() as db:
            db.add(MemoryRule(user_id=uid, name="Tagged Off", tag="longform",
                              is_active=False))
            await db.commit()

        async with TestSessionLocal() as db:
            rule = await resolve_memory_rule(db, uid, tags_for("<#memory-rule-longform#>"))
        assert rule is not None and rule.name == "Tagged Off"

    async def test_an_unrelated_tag_selects_no_memory_rule(self, admin_client):
        from app.models.memory_rule import MemoryRule
        from app.services.summarization import resolve_memory_rule

        client, _, _ = admin_client
        uid = await self._user(client)

        async with TestSessionLocal() as db:
            db.add(MemoryRule(user_id=uid, name="Active Tagged", tag="something",
                              is_active=True))
            await db.commit()

        async with TestSessionLocal() as db:
            assert await resolve_memory_rule(db, uid, tags_for("<#cantrip-dice#>")) is None
