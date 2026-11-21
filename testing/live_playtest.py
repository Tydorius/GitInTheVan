"""Live end-to-end playtest suite for GitInTheVan against a real local LLM
(e.g. llama-swap at http://10.0.0.139:8080/v1 with qwen3-6-35b and deckard).

Boots a throwaway GitInTheVan server with a fresh temporary SQLite database on
a free local port (never touching the live install's data/ directory), wires up
real endpoints, cantrips, lorebooks, verification rules, tag groups, and maps,
and exercises 8 full multi-turn roleplay loops against the live models.

Usage:
    .venv/Scripts/python.exe testing/live_playtest.py
    .venv/Scripts/python.exe testing/live_playtest.py --driver-model qwen3-6-35b --navigator-model deckard
    .venv/Scripts/python.exe testing/live_playtest.py --llm-url http://10.0.0.139:8080 --report report.json
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
PUBLIC_REPO_DEFAULT = pathlib.Path(r"E:\github\GitInTheVan-Public")


@dataclass
class ScenarioResult:
    name: str
    passed: bool
    duration_s: float
    details: list[str] = field(default_factory=list)
    error: str = ""


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_for_health(base_url: str, timeout_s: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_s
    last_err = ""
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"{base_url}/health", timeout=2.0)
            if r.status_code == 200:
                return
            last_err = f"HTTP {r.status_code}"
        except Exception as e:
            last_err = str(e)
        time.sleep(0.4)
    raise RuntimeError(f"GitInTheVan did not become healthy at {base_url}: {last_err}")


def _extract_sse_text(raw: str) -> str:
    chunks: list[str] = []
    for line in raw.splitlines():
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue
        choices = obj.get("choices") or []
        if not choices:
            continue
        delta = choices[0].get("delta") or {}
        if isinstance(delta.get("content"), str):
            chunks.append(delta["content"])
    return "".join(chunks)


class PlaytestRunner:
    def __init__(
        self,
        base_url: str,
        llm_url: str,
        driver_model: str,
        navigator_model: str,
        public_repo: pathlib.Path,
        timeout_s: float = 120.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.llm_url = llm_url.rstrip("/")
        self.driver_model = driver_model
        self.navigator_model = navigator_model
        self.public_repo = public_repo
        self.client = httpx.Client(base_url=self.base_url, timeout=timeout_s)
        self.jwt: str = ""
        self.api_key: str = ""
        self.driver_ep_id: str = ""
        self.nav_ep_id: str = ""

    def close(self) -> None:
        self.client.close()

    @property
    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.jwt}"}

    def proxy_headers(self, chat_id: str = "", extra: dict[str, str] | None = None) -> dict[str, str]:
        h = {"Authorization": f"Bearer {self.api_key}"}
        if chat_id:
            h["X-Chat-Id"] = chat_id
        if extra:
            h.update(extra)
        return h

    def api(self, method: str, path: str, **kwargs) -> httpx.Response:
        headers = dict(self.auth_headers)
        headers.update(kwargs.pop("headers", {}))
        r = self.client.request(method, path, headers=headers, **kwargs)
        return r

    def bootstrap(self) -> None:
        r = self.client.post(
            "/api/auth/setup",
            json={"username": "playtester", "password": "PlaytestPassword123!"},
        )
        assert r.status_code in (200, 201), f"Setup failed: {r.status_code} {r.text}"
        data = r.json()
        self.jwt = data["access_token"]
        self.api_key = data["api_key"]

        # Driver endpoint (qwen3-6-35b)
        r_drv = self.api(
            "POST",
            "/api/endpoints",
            json={
                "name": "LocalDriver",
                "base_url": self.llm_url,
                "api_key": "local-dummy",
                "default_model": self.driver_model,
                "role_tag": "driver",
                "priority": 1,
            },
        )
        assert r_drv.status_code in (200, 201), r_drv.text
        self.driver_ep_id = r_drv.json()["id"]

        # Navigator endpoint (deckard)
        r_nav = self.api(
            "POST",
            "/api/endpoints",
            json={
                "name": "LocalNavigator",
                "base_url": self.llm_url,
                "api_key": "local-dummy",
                "default_model": self.navigator_model,
                "role_tag": "navigator",
                "priority": 1,
            },
        )
        assert r_nav.status_code in (200, 201), r_nav.text
        self.nav_ep_id = r_nav.json()["id"]

        # Configure settings: raise stage cap, disable rate limits for rapid playtest, enable verification & debug.
        r_adm = self.api(
            "PUT",
            "/api/admin/settings",
            json={
                "max_map_stages": 5,
                "rate_limit_api_per_min": 1000,
                "rate_limit_proxy_per_min": 1000,
            },
        )
        assert r_adm.status_code == 200, r_adm.text

        r_set = self.api(
            "PUT",
            "/api/settings",
            json={
                "default_endpoint_id": self.driver_ep_id,
                "debug_mode": True,
                "context_budget_percent": 20.0,
                "context_window_override": 32768,
            },
        )
        assert r_set.status_code == 200, r_set.text

        r_vset = self.api(
            "PUT",
            "/api/verification/settings",
            json={
                "verification_enabled": True,
                "verification_endpoint_id": self.nav_ep_id,
                "verification_model": self.navigator_model,
            },
        )
        assert r_vset.status_code == 200, r_vset.text

    def _cleanup_resources(self) -> None:
        """Disable all cantrips, lorebooks, rules, maps, tag groups between scenarios."""
        for path, key in [
            ("/api/cantrips", "cantrips"),
            ("/api/lorebooks", "lorebooks"),
            ("/api/verification/rules", "rules"),
            ("/api/maps", "maps"),
            ("/api/tag-groups", "groups"),
        ]:
            r = self.api("GET", path)
            if r.status_code != 200:
                continue
            items = r.json().get(key, r.json() if isinstance(r.json(), list) else [])
            for item in items:
                self.api("DELETE", f"{path}/{item['id']}")
        self.api("PUT", "/api/settings", json={"default_map_id": None, "pipeline_order": {"enabled": False, "items": []}})

    def _chat(
        self,
        messages: list[dict[str, Any]],
        *,
        chat_id: str,
        model: str | None = None,
        stream: bool = False,
        max_tokens: int = 128,
        extra_headers: dict[str, str] | None = None,
    ) -> tuple[int, str, dict[str, Any] | None]:
        body: dict[str, Any] = {
            "model": model or self.driver_model,
            "messages": messages,
            "temperature": 0.2,
            "max_tokens": max_tokens,
            "stream": stream,
        }
        r = self.client.post(
            "/v1/chat/completions",
            json=body,
            headers=self.proxy_headers(chat_id, extra_headers),
        )
        if stream:
            return r.status_code, _extract_sse_text(r.text), None
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else None
        text = ""
        if isinstance(data, dict):
            choices = data.get("choices") or []
            if choices:
                text = (choices[0].get("message") or {}).get("content") or ""
        return r.status_code, text, data

    def _get_chat_data_dict(self, conversation_id: str) -> dict[str, Any]:
        r = self.api("GET", "/api/storage/chat-data", params={"conversation_id": conversation_id, "limit": 200})
        assert r.status_code == 200, r.text
        out: dict[str, Any] = {}
        for row in r.json().get("rows", []):
            detail = self.api("GET", f"/api/storage/chat-data/{row['id']}")
            assert detail.status_code == 200, detail.text
            out[row["key"]] = detail.json().get("value")
        return out

    def _get_user_data_dict(self) -> dict[str, Any]:
        r = self.api("GET", "/api/storage/user-data", params={"limit": 200})
        assert r.status_code == 200, r.text
        out: dict[str, Any] = {}
        for row in r.json().get("rows", []):
            detail = self.api("GET", f"/api/storage/user-data/{row['id']}")
            assert detail.status_code == 200, detail.text
            out[row["key"]] = detail.json().get("value")
        return out

    def _latest_debug_stages(self) -> list[dict[str, Any]]:
        r = self.api("GET", "/api/debug", params={"limit": 1})
        assert r.status_code == 200, r.text
        exchanges = r.json().get("exchanges") or []
        assert exchanges, "No debug exchange recorded"
        ex_id = exchanges[0]["id"]
        d = self.api("GET", f"/api/debug/{ex_id}")
        assert d.status_code == 200, d.text
        pd = d.json().get("pipeline_data") or {}
        return pd.get("stages") or []

    # ------------------------------------------------------------------
    # Scenarios
    # ------------------------------------------------------------------

    def scenario_1_continuity_and_prefix_scoping(self) -> list[str]:
        self._cleanup_resources()
        # Seed user_data with two prefixes: play: and other:
        r_seed = self.api(
            "POST",
            "/api/storage/import",
            json={
                "payload": {
                    "format": "gitv-storage-export",
                    "version": 1,
                    "scope": "user",
                    "rows": [
                        {"key": "play:gold", "value": 10},
                        {"key": "play:temp", "value": "delete_me"},
                        {"key": "other:secret", "value": "untouched"},
                    ],
                }
            },
        )
        assert r_seed.status_code in (200, 201), r_seed.text

        # Cantrip scoped ONLY to ["play:"] — reads play:gold, deletes play:temp, tries to read other:secret.
        code = (
            "const gold = Number(context.user_data.get('play:gold') || 0);\n"
            "const sawOther = context.user_data.get('other:secret') != null;\n"
            "context.user_data.set('play:gold', gold + 5);\n"
            "context.user_data.delete('play:temp');\n"
            "const turns = Number(context.chat_data.get('turns') || 0) + 1;\n"
            "context.chat_data.set('turns', turns);\n"
            "context.chat_data.set('saw_other', sawOther);\n"
            "context.inject('[STATE] Gold=' + (gold + 5) + ' Turn=' + turns);\n"
        )
        r = self.api(
            "POST",
            "/api/cantrips",
            json={
                "name": "ScopedState",
                "is_active": True,
                "run_pre_driver": True,
                "user_data_prefixes": ["play:"],
                "code": code,
            },
        )
        assert r.status_code in (200, 201), r.text

        chat_id = "playtest-s1"
        msgs = [
            {"role": "system", "content": "Reply in one short sentence acknowledging the current Gold and Turn."},
            {"role": "user", "content": "Check my pouch."},
        ]
        st1, txt1, _ = self._chat(msgs, chat_id=chat_id)
        assert st1 == 200 and txt1.strip(), f"Turn 1 failed: {st1} {txt1}"

        msgs.append({"role": "assistant", "content": txt1})
        msgs.append({"role": "user", "content": "Check again."})
        st2, txt2, _ = self._chat(msgs, chat_id=chat_id)
        assert st2 == 200 and txt2.strip(), f"Turn 2 failed: {st2} {txt2}"

        ud = self._get_user_data_dict()
        cd = self._get_chat_data_dict(chat_id)
        assert ud.get("play:gold") == 20, f"Expected play:gold=20, got {ud}"
        assert "play:temp" not in ud, f"play:temp delete did not persist: {ud}"
        assert ud.get("other:secret") == "untouched", f"Unseeded key clobbered: {ud}"
        assert cd.get("turns") == 2, f"Expected 2 turns, got {cd}"
        assert cd.get("saw_other") is False, f"Prefix scoping leaked other:secret: {cd}"
        return [
            f"Turn 1 reply: {txt1[:80]!r}",
            f"Turn 2 reply: {txt2[:80]!r}",
            "Verified: play:gold=20, play:temp deleted, other:secret untouched & invisible, turns=2",
        ]

    def scenario_2_lorebooks_and_dynamic(self) -> list[str]:
        self._cleanup_resources()
        # Static lorebook with word_boundary_keys + punctuation key + reserved_tokens
        r_lb = self.api(
            "POST",
            "/api/lorebooks",
            json={
                "name": "WorldCodex",
                "is_active": True,
                "word_boundary_keys": True,
                "reserved_tokens": 2000,
            },
        )
        assert r_lb.status_code in (200, 201), r_lb.text
        lb_id = r_lb.json()["id"]
        r_e1 = self.api(
            "POST",
            f"/api/lorebooks/{lb_id}/entries",
            json={
                "name": "BramOutfitter",
                "keys": ["Bram,"],
                "content": "SECRET_LORE_CODE: IRON-LANTERN-77. Bram sells enchanted lanterns.",
                "insertion_order": 10,
            },
        )
        assert r_e1.status_code in (200, 201), r_e1.text

        # Cantrip that creates a dynamic lorebook entry on turn 1
        r_c = self.api(
            "POST",
            "/api/cantrips",
            json={
                "name": "DynScribe",
                "is_active": True,
                "run_pre_driver": True,
                "code": (
                    "const lb = context.lorebook.create({ name: 'PlayDyn' });\n"
                    "if (!context.chat_data.get('seeded')) {\n"
                    "  lb.add({ name: 'Tower', keys: ['tower'], content: 'DYN_FACT: SILVER-BELL-99. The tower holds a silver bell.' });\n"
                    "  context.chat_data.set('seeded', true);\n"
                    "}\n"
                ),
            },
        )
        assert r_c.status_code in (200, 201), r_c.text

        chat_id = "playtest-s2"
        msgs = [
            {"role": "system", "content": "Answer concisely using any lorebook facts provided."},
            {"role": "user", "content": "Bram, what do you sell?"},
        ]
        st1, txt1, _ = self._chat(msgs, chat_id=chat_id)
        assert st1 == 200, st1
        assert "lantern" in txt1.lower() or "IRON-LANTERN-77" in txt1, f"Punctuation whole-word key 'Bram,' did not inject: {txt1!r}"

        msgs.append({"role": "assistant", "content": txt1})
        msgs.append({"role": "user", "content": "Tell me about the tower."})
        st2, txt2, _ = self._chat(msgs, chat_id=chat_id)
        assert st2 == 200, st2
        final_stages = [s for s in self._latest_debug_stages() if s["name"] == "final_messages"]
        final_blob = json.dumps(final_stages)
        assert "SILVER-BELL-99" in final_blob or "bell" in txt2.lower(), (
            f"Dynamic lorebook entry did not inject on turn 2. Reply={txt2!r}, stages={[s['name'] for s in self._latest_debug_stages()]}"
        )
        return [
            f"Verified: whole-word punctuation key 'Bram,' injected -> {txt1[:70]!r}",
            f"Verified: dynamic lorebook entry injected on turn 2 -> {txt2[:70]!r}",
        ]

    def scenario_3_response_tags_nonstream_and_stream(self) -> list[str]:
        self._cleanup_resources()
        r_c = self.api(
            "POST",
            "/api/cantrips",
            json={
                "name": "DeedTracker",
                "is_active": True,
                "run_pre_driver": False,
                "run_post_navigator": True,
                "response_tags": ["deed"],
                "code": (
                    "const deeds = (context.response_tags && context.response_tags.deed) || [];\n"
                    "const list = context.chat_data.get('deeds') || [];\n"
                    "for (const d of deeds) list.push(d);\n"
                    "context.chat_data.set('deeds', list);\n"
                ),
            },
        )
        assert r_c.status_code in (200, 201), r_c.text

        chat_id = "playtest-s3"
        sys_msg = (
            "Reply with exactly one short sentence AND end your reply with the literal XML tag "
            "<deed>lit_torch</deed> (with no spaces inside the tag)."
        )
        # Non-streaming
        st1, txt1, _ = self._chat(
            [{"role": "system", "content": sys_msg}, {"role": "user", "content": "I light the torch."}],
            chat_id=chat_id,
            stream=False,
        )
        assert st1 == 200, st1
        assert "<deed>" not in txt1 and "lit_torch" not in txt1, f"Response tag leaked (non-stream): {txt1!r}"

        # Streaming
        st2, txt2, _ = self._chat(
            [
                {"role": "system", "content": "Reply with one short sentence ending with <deed>opened_chest</deed>."},
                {"role": "user", "content": "I open the chest."},
            ],
            chat_id=chat_id,
            stream=True,
        )
        assert st2 == 200, st2
        assert "<deed>" not in txt2 and "opened_chest" not in txt2, f"Response tag leaked (stream): {txt2!r}"

        cd = self._get_chat_data_dict(chat_id)
        deeds = cd.get("deeds", [])
        assert "lit_torch" in deeds, f"Non-stream deed not captured: {cd}"
        assert "opened_chest" in deeds, f"Stream deed not captured: {cd}"
        return [
            f"Non-stream visible: {txt1[:70]!r}",
            f"Stream visible: {txt2[:70]!r}",
            f"Captured deeds in chat_data: {deeds}",
        ]

    def scenario_4_macro_expansion_safety(self) -> list[str]:
        self._cleanup_resources()
        chat_id = "playtest-s4"

        # Case A: Unknown user + "Persona: shy" must NOT turn {{user}} into "User" or {{char}} into "shy"
        st1, _, _ = self._chat(
            [
                {"role": "system", "content": "Persona: shy, observant\nReply with 'OK'."},
                {"role": "user", "content": "Hello {{user}} and {{char}}."},
            ],
            chat_id=f"{chat_id}-a",
        )
        assert st1 == 200
        stages_a = [s["name"] for s in self._latest_debug_stages()]
        assert "macro_expansion" not in stages_a, f"Unexpected macro_expansion on unknown names / Persona: shy: {stages_a}"

        # Case B: Explicit headers expand both {{user}} and {{char}}
        st2, txt2, _ = self._chat(
            [
                {"role": "system", "content": "Greet {{user}} as {{char}} in one short sentence."},
                {"role": "user", "content": "Hi {{char}}!"},
            ],
            chat_id=f"{chat_id}-b",
            extra_headers={"X-User-Name": "Hale", "X-Char-Name": "Lyra"},
        )
        assert st2 == 200
        macro_stages = [s for s in self._latest_debug_stages() if s["name"] == "macro_expansion"]
        assert macro_stages, "Expected macro_expansion debug stage"
        meta = macro_stages[0].get("metadata") or {}
        assert meta.get("user_name") == "Hale" and meta.get("char_name") == "Lyra", meta
        return [
            "Verified: unknown {{user}} & 'Persona: shy' triggered 0 macro expansions",
            f"Verified: X-User-Name=Hale & X-Char-Name=Lyra expanded cleanly; reply={txt2[:70]!r}",
        ]

    def scenario_5_hybrid_verification_and_deckard(self) -> list[str]:
        self._cleanup_resources()
        # Rule with regex pre-check that triggers if reply contains FORBIDDEN_TOKEN_XYZ
        r_rule = self.api(
            "POST",
            "/api/verification/rules",
            json={
                "name": "NoForbiddenToken",
                "is_active": True,
                "prompt": "Ensure the reply stays in character and concise.",
                "regex_pattern": r"FORBIDDEN_TOKEN_XYZ",
                "max_retries": 0,
            },
        )
        assert r_rule.status_code in (200, 201), r_rule.text

        chat_id = "playtest-s5"
        # 1. Trigger regex pre-check (should short-circuit with 0 Navigator LLM calls!)
        st1, _, _ = self._chat(
            [
                {"role": "system", "content": "Include the exact string FORBIDDEN_TOKEN_XYZ in your reply."},
                {"role": "user", "content": "Go."},
            ],
            chat_id=f"{chat_id}-regex",
        )
        assert st1 == 200
        stage_names = [s["name"] for s in self._latest_debug_stages()]
        assert "verification_regex_precheck" in stage_names, f"Expected regex pre-check stage: {stage_names}"

        # 2. Clean reply passes through real deckard LLM judge
        st2, txt2, _ = self._chat(
            [
                {"role": "system", "content": "You are a tavern keeper. Reply in one polite sentence."},
                {"role": "user", "content": "Good evening!"},
            ],
            chat_id=f"{chat_id}-judge",
        )
        assert st2 == 200 and txt2.strip()

        # 3. <VERIFY:off> skips verification completely
        st3, _, _ = self._chat(
            [
                {"role": "system", "content": "Include FORBIDDEN_TOKEN_XYZ in your reply."},
                {"role": "user", "content": "<VERIFY:off> Go."},
            ],
            chat_id=f"{chat_id}-off",
        )
        assert st3 == 200
        assert "verification_regex_precheck" not in [s["name"] for s in self._latest_debug_stages()]
        return [
            "Verified: regex pre-check caught FORBIDDEN_TOKEN_XYZ before LLM judge",
            f"Verified: real deckard Navigator judged clean reply: {txt2[:70]!r}",
            "Verified: <VERIFY:off> bypassed verification",
        ]

    def scenario_6_map_postnav_and_final_verification(self) -> list[str]:
        self._cleanup_resources()
        # Verification rule for map final-output pass
        r_rule = self.api(
            "POST",
            "/api/verification/rules",
            json={
                "name": "MapFinalRule",
                "is_active": True,
                "prompt": "Verify the response is coherent prose.",
                "max_retries": 0,
            },
        )
        assert r_rule.status_code in (200, 201), r_rule.text

        # Post-Navigator cantrip capturing <mapdeed> from the map's final output (0.45.1 fix!)
        r_c = self.api(
            "POST",
            "/api/cantrips",
            json={
                "name": "MapPostNavCatcher",
                "is_active": True,
                "run_pre_driver": False,
                "run_post_navigator": True,
                "response_tags": ["mapdeed"],
                "code": (
                    "const vals = (context.response_tags && context.response_tags.mapdeed) || [];\n"
                    "if (vals.length) context.chat_data.set('map_deed', vals[0]);\n"
                ),
            },
        )
        assert r_c.status_code in (200, 201), r_c.text

        # 2-stage map: Stage 1 (qwen3-6-35b) outlines -> Stage 2 (deckard) writes prose + <mapdeed>
        r_m = self.api(
            "POST",
            "/api/maps",
            json={
                "name": "TwoStagePlaytestMap",
                "is_active": True,
                "final_verification_enabled": True,
                "stages": [
                    {
                        "stage_order": 1,
                        "name": "Planner",
                        "system_instructions": "Write a 1-sentence scene beat for entering a dark cavern.",
                        "endpoint_id": self.driver_ep_id,
                        "model_override": self.driver_model,
                        "output_mode": "sanitize",
                        "resources": [],
                    },
                    {
                        "stage_order": 2,
                        "name": "Narrator",
                        "system_instructions": (
                            "Write one vivid sentence based on the beat, and end with the exact hidden tag "
                            "<mapdeed>entered_cavern</mapdeed>."
                        ),
                        "endpoint_id": self.nav_ep_id,
                        "model_override": self.navigator_model,
                        "output_mode": "persist",
                        "resources": [],
                    },
                ],
            },
        )
        assert r_m.status_code in (200, 201), r_m.text
        map_id = r_m.json()["id"]
        assert self.api("PUT", "/api/settings", json={"default_map_id": map_id}).status_code == 200

        chat_id = "playtest-s6"
        st, txt, _ = self._chat(
            [
                {"role": "system", "content": "Roleplay narrator."},
                {"role": "user", "content": "I step into the cavern."},
            ],
            chat_id=chat_id,
            max_tokens=1024,
        )
        assert st == 200 and txt.strip(), f"Map failed: {st} {txt}"
        assert "<mapdeed>" not in txt and "entered_cavern" not in txt, f"Hidden map tag leaked: {txt!r}"

        cd = self._get_chat_data_dict(chat_id)
        captured = cd.get("map_deed")
        assert captured == "entered_cavern", f"Post-Navigator cantrip did not receive map reply tag: {cd}"

        stage_names = [s["name"] for s in self._latest_debug_stages()]
        assert "map_final_verification" in stage_names, f"Missing map_final_verification stage: {stage_names}"
        return [
            f"Map visible reply: {txt[:80]!r}",
            f"Verified: hidden <mapdeed> stripped & captured by Post-Navigator -> {captured!r}",
            "Verified: map_final_verification ran report-only on final output",
        ]

    def scenario_7_tag_groups_and_manual_pipeline_order(self) -> list[str]:
        self._cleanup_resources()
        # Blank-tag inactive cantrip that edits character personality
        r_c = self.api(
            "POST",
            "/api/cantrips",
            json={
                "name": "BlankTagCantrip",
                "is_active": False,
                "tag": "",
                "run_pre_driver": True,
                "code": (
                    "context.character.personality = (context.character.personality || '') + ' [CANTRIP_MARK]';\n"
                    "context.chat_data.set('group_fired', true);\n"
                ),
            },
        )
        assert r_c.status_code in (200, 201), r_c.text
        c_id = r_c.json()["id"]

        # Lorebook activated by group too
        r_lb = self.api(
            "POST",
            "/api/lorebooks",
            json={"name": "GroupBook", "is_active": False, "tag": ""},
        )
        assert r_lb.status_code in (200, 201), r_lb.text
        lb_id = r_lb.json()["id"]
        self.api(
            "POST",
            f"/api/lorebooks/{lb_id}/entries",
            json={"name": "KeepMe", "keys": ["banner"], "content": "LORE_PRESERVED_42", "is_constant": True},
        )

        # TagGroup with prefix-colliding tag 'taggroup-play' + activate_members=True
        r_tg = self.api(
            "POST",
            "/api/tag-groups",
            json={
                "name": "PlayGroup",
                "tag": "taggroup-play",
                "is_active": False,
                "activate_members": True,
                "members": [
                    {"member_type": "cantrip", "member_id": c_id},
                    {"member_type": "lorebook", "member_id": lb_id},
                ],
            },
        )
        assert r_tg.status_code in (200, 201), r_tg.text

        # Enable Manual Pipeline Order: lorebook first, then cantrip (regression check for 0.42.1 stale-messages bug!)
        r_set = self.api(
            "PUT",
            "/api/settings",
            json={
                "pipeline_order": {
                    "enabled": True,
                    "items": [
                        {"type": "lorebook", "id": lb_id},
                        {"type": "cantrip", "id": c_id},
                    ],
                }
            },
        )
        assert r_set.status_code == 200, r_set.text

        chat_id = "playtest-s7"
        st, txt, _ = self._chat(
            [
                {"role": "system", "content": "<Guide's Persona>Helpful.</Guide's Persona>\nRepeat LORE_PRESERVED_42 if present."},
                {"role": "user", "content": "<#taggroup-play#> Show the banner."},
            ],
            chat_id=chat_id,
        )
        assert st == 200
        cd = self._get_chat_data_dict(chat_id)
        assert cd.get("group_fired") is True, f"Colliding taggroup-play did not fire blank-tag member: {cd}"
        final_stages = [s for s in self._latest_debug_stages() if s["name"] == "final_messages"]
        final_blob = json.dumps(final_stages)
        assert "LORE_PRESERVED_42" in final_blob, f"Manual pipeline order lost lorebook injection after card edit: final={final_blob[:300]}"
        assert "[CANTRIP_MARK]" in final_blob, f"Cantrip card edit missing from final_messages: final={final_blob[:300]}"
        return [
            "Verified: colliding tag '<#taggroup-play#>' activated blank-tag inactive cantrip & lorebook",
            "Verified: manual pipeline order preserved LORE_PRESERVED_42 after cantrip mutated personality",
        ]

    def scenario_8_live_mud_world_engine(self) -> list[str]:
        self._cleanup_resources()
        lb_path = self.public_repo / "lorebooks" / "MUD_World_Example.json"
        c_path = self.public_repo / "cantrips" / "MUD_World_Engine.json"
        if not lb_path.exists() or not c_path.exists():
            return ["SKIPPED: MUD kit not found at " + str(self.public_repo)]

        lb_data = json.loads(lb_path.read_text(encoding="utf-8"))
        c_data = json.loads(c_path.read_text(encoding="utf-8"))

        # Create MUD World Example lorebook (inactive) and populate its entries directly
        r_lb = self.api(
            "POST",
            "/api/lorebooks",
            json={"name": lb_data["name"], "is_active": False, "description": lb_data.get("description", "")},
        )
        assert r_lb.status_code in (200, 201), r_lb.text
        lb_id = r_lb.json()["id"]
        for entry in lb_data.get("entries", []):
            r_e = self.api("POST", f"/api/lorebooks/{lb_id}/entries", json=entry)
            assert r_e.status_code in (200, 201), r_e.text

        # Create MUD World Engine cantrip
        r_c = self.api(
            "POST",
            "/api/cantrips",
            json={
                "name": c_data["name"],
                "description": c_data.get("description", ""),
                "llm_instructions": c_data.get("llm_instructions", ""),
                "code": c_data["code"],
                "is_active": True,
                "run_pre_driver": True,
                "run_driver_callable": True,
                "run_post_navigator": True,
                "lorebook_reads": c_data.get("lorebook_reads", ["MUD World Example"]),
                "response_tags": c_data.get("response_tags", ["mud_move", "mud_delta", "mud_time"]),
            },
        )
        assert r_c.status_code in (200, 201), r_c.text

        chat_id = "playtest-s8-mud"
        msgs = [
            {
                "role": "system",
                "content": (
                    "You are a MUD narrator. Describe the current location concisely based on [CURRENT LOCATION]. "
                    "When the player moves or alters the room, include the requested hidden XML tags."
                ),
            },
            {"role": "user", "content": "Look around where I am right now."},
        ]
        # 400 tokens: 180 let the model run out mid-closing-tag, leaving an
        # unterminated <mud_move> that (by design) is never stripped.
        st1, txt1, _ = self._chat(msgs, chat_id=chat_id, max_tokens=400)
        assert st1 == 200 and txt1.strip(), f"MUD turn 1 failed: {st1} {txt1}"

        # Turn 2: instruct LLM to record a coordinate delta and move East
        msgs.append({"role": "assistant", "content": txt1})
        msgs.append(
            {
                "role": "user",
                "content": (
                    "I scorch the fountain and walk East. "
                    '(End your reply with these exact hidden tags: <mud_delta>{"event":"fountain scorched"}</mud_delta> <mud_move>E</mud_move>)'
                ),
            }
        )
        st2, txt2, _ = self._chat(msgs, chat_id=chat_id, max_tokens=400)
        assert st2 == 200 and txt2.strip(), f"MUD turn 2 failed: {st2} {txt2}"
        assert "<mud_delta" not in txt2 and "<mud_move" not in txt2, f"MUD tags leaked: {txt2!r}"

        cd = self._get_chat_data_dict(chat_id)
        loc = cd.get("mud_loc")
        cell_delta = cd.get("cell:overworld:a1") or cd.get("cell:overworld:b1") or {}
        assert loc == "overworld:b1", f"Expected mud_loc='overworld:b1', got {cd}"
        assert cell_delta, f"Expected coordinate-attached cell delta in chat_data, got {cd}"
        return [
            f"MUD Turn 1 (overworld:a1): {txt1[:80]!r}",
            f"MUD Turn 2 (moved -> {loc}): {txt2[:80]!r}",
            f"Persisted cell delta: {cell_delta}",
        ]

    def run_all(self) -> list[ScenarioResult]:
        scenarios = [
            ("1. Continuity + Store Deletes + N6 Prefix Scoping", self.scenario_1_continuity_and_prefix_scoping),
            ("2. Lorebooks (Word-Boundary Punctuation + Reserved Tokens + Dynamic)", self.scenario_2_lorebooks_and_dynamic),
            ("3. Hidden Response Tags + Post-Navigator (JSON & SSE Stream)", self.scenario_3_response_tags_nonstream_and_stream),
            ("4. Macro Expansion Safety (Unknown {{user}} + Persona: shy + Headers)", self.scenario_4_macro_expansion_safety),
            ("5. Hybrid Verification (Regex Pre-Check + deckard Judge + <VERIFY:off>)", self.scenario_5_hybrid_verification_and_deckard),
            ("6. 2-Stage Map + Final-Output Verification + Post-Navigator Tags", self.scenario_6_map_postnav_and_final_verification),
            ("7. Tag Groups (Colliding Prefix + activate_members) + Manual Pipeline Order", self.scenario_7_tag_groups_and_manual_pipeline_order),
            ("8. Live MUD Spatial World Engine (Base + Coordinate Delta Overlay)", self.scenario_8_live_mud_world_engine),
        ]
        results: list[ScenarioResult] = []
        for name, fn in scenarios:
            t0 = time.monotonic()
            try:
                details = fn()
                results.append(ScenarioResult(name=name, passed=True, duration_s=round(time.monotonic() - t0, 2), details=details))
                print(f"[PASS] {name} ({results[-1].duration_s}s)")
                for d in details:
                    print(f"       {d}")
            except Exception as exc:
                results.append(ScenarioResult(name=name, passed=False, duration_s=round(time.monotonic() - t0, 2), error=str(exc)))
                print(f"[FAIL] {name} ({results[-1].duration_s}s): {exc}")
        return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Live end-to-end playtest suite for GitInTheVan.")
    parser.add_argument("--llm-url", default="http://10.0.0.139:8080", help="OpenAI-compatible local LLM server base URL")
    parser.add_argument("--driver-model", default="qwen3-6-35b:nothink", help="Driver LLM model id")
    parser.add_argument("--navigator-model", default="deckard", help="Navigator / Stage-2 LLM model id")
    parser.add_argument("--base-url", default="", help="Use an already-running GitInTheVan URL instead of booting a throwaway server")
    parser.add_argument("--public-repo", default=str(PUBLIC_REPO_DEFAULT), help="Path to GitInTheVan-Public repo for MUD kit playtest")
    parser.add_argument("--report", default="", help="Optional path to write JSON report")
    args = parser.parse_args()

    proc: subprocess.Popen | None = None
    tmp_dir: tempfile.TemporaryDirectory | None = None
    log_handle = None

    try:
        if args.base_url:
            base_url = args.base_url
        else:
            port = _free_port()
            tmp_dir = tempfile.TemporaryDirectory(prefix="gitv_playtest_", ignore_cleanup_errors=True)
            db_path = pathlib.Path(tmp_dir.name) / "playtest.db"
            log_path = pathlib.Path(tmp_dir.name) / "server.log"
            env = os.environ.copy()
            env["GITV_HOST"] = "127.0.0.1"
            env["GITV_PORT"] = str(port)
            env["GITV_DATABASE_URL"] = f"sqlite+aiosqlite:///{db_path.as_posix()}"
            env["GITV_GENERATE_CERTS"] = "false"
            env["GITV_SSL_CERTFILE"] = ""
            env["GITV_SSL_KEYFILE"] = ""
            env["GITV_HTTP_REDIRECT_PORT"] = "0"
            env["GITV_LOG_LEVEL"] = "WARNING"

            base_url = f"http://127.0.0.1:{port}"
            print(f"Booting throwaway GitInTheVan at {base_url} (temp DB: {db_path})...")
            log_handle = log_path.open("w", encoding="utf-8")
            proc = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
                cwd=str(REPO_ROOT),
                env=env,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
            )
            try:
                _wait_for_health(base_url, timeout_s=60.0)
            except Exception:
                log_handle.flush()
                tail = log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
                print(f"--- Throwaway server log tail ---\n{tail}\n--- end ---")
                raise
            print("Throwaway GitInTheVan is healthy.")

        runner = PlaytestRunner(
            base_url=base_url,
            llm_url=args.llm_url,
            driver_model=args.driver_model,
            navigator_model=args.navigator_model,
            public_repo=pathlib.Path(args.public_repo),
        )
        try:
            runner.bootstrap()
            results = runner.run_all()
        finally:
            runner.close()

        passed = sum(1 for r in results if r.passed)
        total = len(results)
        print(f"\n=== PLAYTEST SUMMARY: {passed}/{total} PASSED ===")
        if args.report:
            pathlib.Path(args.report).write_text(
                json.dumps([asdict(r) for r in results], indent=2),
                encoding="utf-8",
            )
            print(f"Report written to {args.report}")
        return 0 if passed == total else 1
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        if log_handle is not None:
            log_handle.close()
        if tmp_dir is not None:
            tmp_dir.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
