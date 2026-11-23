from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.services.cantrip_context import build_context
from app.services.deno_runner import (
    DENO_PATH,
    CantripTimeoutError,
    DenoWorkerPool,
    deno_pool,
    run_cantrip,
)

pytestmark = pytest.mark.skipif(not DENO_PATH, reason="Deno not available")


def _make_context(last_message: str = "Hello") -> dict[str, Any]:
    return build_context(
        messages=[{"role": "user", "content": last_message}],
        conversation_id="pool-test-conv",
        user_name="TestUser",
        character_name="TestChar",
        character_description="A test character",
        first_message="Greetings!",
    )


async def _wait_for_idle(pool: DenoWorkerPool, expected: int, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if pool.status()["idle_count"] == expected:
            return
        await asyncio.sleep(0.05)
    assert pool.status()["idle_count"] == expected


class _FakePassengerHandler:
    """Minimal PassengerHandler stub for testing interactive warm workers."""

    def __init__(self, reply_text: str = "PASSENGER-OK", max_calls: int = 3) -> None:
        self.enabled = True
        self.max_calls = max_calls
        self.reply_text = reply_text
        self.calls: list[dict[str, str]] = []

    def watchdog_ms(self, script_timeout_ms: int) -> int:
        return script_timeout_ms + 5000

    async def call(
        self,
        tag: str,
        instructions: str,
        context: str,
        model: str,
    ) -> tuple[str | None, str]:
        self.calls.append(
            {
                "tag": tag,
                "instructions": instructions,
                "context": context,
                "model": model,
            }
        )
        return self.reply_text, "ok"


@pytest.fixture(autouse=True)
async def _reset_global_pool():
    """Keep each test isolated on the module-level deno_pool singleton."""
    await deno_pool.shutdown()
    deno_pool.min_idle = 1
    deno_pool.max_idle = 4
    deno_pool.idle_ttl_seconds = 60
    deno_pool.total_warm_hits = 0
    deno_pool.total_cold_spawns = 0
    yield
    await deno_pool.shutdown()
    deno_pool.min_idle = 1
    deno_pool.max_idle = 4
    deno_pool.idle_ttl_seconds = 60
    deno_pool.total_warm_hits = 0
    deno_pool.total_cold_spawns = 0


@pytest.mark.asyncio
async def test_single_use_worker_isolation():
    """Two consecutive cantrips run in fresh isolates: global state never leaks."""
    deno_pool.ensure_warm(2)
    await _wait_for_idle(deno_pool, 2)

    res1 = await run_cantrip(
        'globalThis.__leak = 1; context.character.scenario = String(globalThis.__leak);',
        _make_context(),
    )
    assert res1.error is None
    assert res1.scenario == "1"

    res2 = await run_cantrip(
        'context.character.scenario = String(typeof globalThis.__leak);',
        _make_context(),
    )
    assert res2.error is None
    assert res2.scenario == "undefined"


@pytest.mark.asyncio
async def test_warm_hit_counter_and_replenishment():
    """Checking out a warm worker increments total_warm_hits and replenishes to min_idle."""
    deno_pool.configure(min_idle=1, max_idle=4, idle_ttl_seconds=60)
    await _wait_for_idle(deno_pool, 1)

    before = deno_pool.status()
    assert before["idle_count"] == 1
    assert before["total_warm_hits"] == 0

    res = await run_cantrip(
        'context.character.personality = "warm-hit";',
        _make_context(),
    )
    assert res.error is None
    assert res.personality == "warm-hit"

    after = deno_pool.status()
    assert after["total_warm_hits"] == 1
    assert after["total_cold_spawns"] == 0

    # Pool replenishes back to min_idle in the background.
    await _wait_for_idle(deno_pool, 1)


@pytest.mark.asyncio
async def test_ensure_warm_burst_scales_and_decays():
    """ensure_warm(3) scales up to min(3, max_idle) and decays back to min_idle after idle_ttl."""
    deno_pool.configure(min_idle=1, max_idle=3, idle_ttl_seconds=60)
    await _wait_for_idle(deno_pool, 1)

    # Override TTL directly for a fast decay test.
    deno_pool.idle_ttl_seconds = 0.25

    # Request 5 workers -> clamped to max_idle (3).
    deno_pool.ensure_warm(5)
    await _wait_for_idle(deno_pool, 3)

    # After idle_ttl elapses, burst workers above min_idle decay back to 1.
    await _wait_for_idle(deno_pool, 1, timeout=3.0)


@pytest.mark.asyncio
async def test_dead_idle_worker_discarded_transparently():
    """A crashed idle worker is discarded on checkout without failing the cantrip."""
    deno_pool.configure(min_idle=1, max_idle=4, idle_ttl_seconds=60)
    await _wait_for_idle(deno_pool, 1)

    # Kill the idle worker directly and wait for it to exit.
    dead_proc, _, _ = deno_pool._idle[0]
    dead_proc.kill()
    await dead_proc.wait()
    assert dead_proc.returncode is not None

    # run_cantrip should discard the dead worker and succeed via a fresh spawn.
    res = await run_cantrip(
        'context.character.scenario = "recovered";',
        _make_context(),
    )
    assert res.error is None
    assert res.scenario == "recovered"
    assert deno_pool.status()["total_cold_spawns"] >= 1


@pytest.mark.asyncio
async def test_watchdog_timeout_kills_worker():
    """An infinite loop is killed by the watchdog and raises CantripTimeoutError."""
    deno_pool.ensure_warm(1)
    await _wait_for_idle(deno_pool, 1)

    with pytest.raises(CantripTimeoutError):
        await run_cantrip("while (true) {}", _make_context(), timeout_ms=400)


@pytest.mark.asyncio
async def test_passenger_interactive_mode_via_warm_worker():
    """context.passenger.call(...) works end-to-end through a pre-warmed worker."""
    deno_pool.ensure_warm(1)
    await _wait_for_idle(deno_pool, 1)

    handler = _FakePassengerHandler(reply_text="ORACLE-SPEAKS")
    code = (
        'const ans = await context.passenger.call({'
        '  tag: "tool_use",'
        '  instructions: "Reply with a prophecy",'
        '  context: "hero enters tavern"'
        '});'
        'context.character.scenario = "Prophecy: " + ans;'
    )
    res = await run_cantrip(code, _make_context(), passenger_handler=handler)
    assert res.error is None
    assert res.scenario == "Prophecy: ORACLE-SPEAKS"
    assert len(handler.calls) == 1
    assert handler.calls[0]["tag"] == "tool_use"
    assert deno_pool.status()["total_warm_hits"] == 1


@pytest.mark.asyncio
async def test_pool_disabled_uses_cold_spawns():
    """With min_idle=0, max_idle=0, cantrips still run via cold deno_worker.mjs."""
    deno_pool.configure(min_idle=0, max_idle=0, idle_ttl_seconds=60)
    deno_pool.ensure_warm(3)
    assert deno_pool.status()["idle_count"] == 0

    res = await run_cantrip(
        'context.character.personality = "cold-ok";',
        _make_context(),
    )
    assert res.error is None
    assert res.personality == "cold-ok"

    st = deno_pool.status()
    assert st["idle_count"] == 0
    assert st["total_cold_spawns"] == 1
    assert st["total_warm_hits"] == 0


@pytest.mark.asyncio
async def test_shutdown_terminates_all_idle_workers():
    """pool.shutdown() kills and reaps every idle worker."""
    pool = DenoWorkerPool(min_idle=2, max_idle=4, idle_ttl_seconds=60)
    pool.ensure_warm(2)
    await _wait_for_idle(pool, 2)

    procs = [entry[0] for entry in pool._idle]
    assert len(procs) == 2
    assert all(p.returncode is None for p in procs)

    await pool.shutdown()

    assert pool.status()["idle_count"] == 0
    assert all(p.returncode is not None for p in procs)


def test_configure_clamping_without_running_loop():
    """configure() clamps bounds and is safe to call outside an event loop."""
    pool = DenoWorkerPool()
    pool.configure(min_idle=-5, max_idle=999, idle_ttl_seconds=1)
    assert pool.min_idle == 0
    assert pool.max_idle == 32
    assert pool.idle_ttl_seconds == 10

    pool.configure(min_idle=20, max_idle=2, idle_ttl_seconds=9999)
    assert pool.min_idle == 16
    assert pool.max_idle == 16
    assert pool.idle_ttl_seconds == 600
