from __future__ import annotations

import asyncio
import atexit
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from collections import deque
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_WORKER_PATH = Path(__file__).parent / "deno_worker.mjs"


def _find_deno() -> str | None:
    # Explicit config value (populated from GITV_DENO_PATH in .env by Settings).
    from app.config import settings
    if settings.deno_path and Path(settings.deno_path).exists():
        return settings.deno_path

    # Local pinned install laid down by the deploy scripts (.deno/deno[.exe]).
    local = Path(__file__).resolve().parent.parent.parent / ".deno" / "deno.exe"
    if local.exists():
        return str(local)

    local_unix = Path(__file__).resolve().parent.parent.parent / ".deno" / "deno"
    if local_unix.exists():
        return str(local_unix)

    which = shutil.which("deno")
    if which:
        return which

    return None


DENO_PATH = _find_deno()


@dataclass
class CantripResult:
    personality: str = ""
    scenario: str = ""
    example_dialogs: str = ""
    response_content: str | None = None
    tool_result: str = ""
    chat_data: dict = field(default_factory=dict)
    user_data: dict = field(default_factory=dict)
    cantrip_data: dict = field(default_factory=dict)
    # 0.42.0: keys the script deleted via the store facades. Absent from
    # the dicts above AND listed here = delete the row host-side; present
    # in the dicts = deleted then re-set in the same run (the set wins).
    chat_data_deletes: list[str] = field(default_factory=list)
    user_data_deletes: list[str] = field(default_factory=list)
    cantrip_data_deletes: list[str] = field(default_factory=list)
    memories: dict = field(default_factory=dict)
    memory_deletes: list[str] = field(default_factory=list)
    # Map tags the cantrip asked to activate via context.maps.activate(tag).
    # A nomination, not a command: maps are a selection resource under repo
    # rule 19, so the nominated tag competes in tag_matches_resource
    # selection rather than joining the additive union.
    maps_nominated: list[str] = field(default_factory=list)
    # Dynamic lorebook ops collected by the context.lorebook facade (Phase
    # 20). Raw op dicts; the host validates and applies them with the
    # lorebook_writes guards -- they are requests, not performed writes.
    lorebook_ops: list[dict] = field(default_factory=list)
    # Package 3 (item 8): static lorebook names the script asked to activate
    # for THIS request. A union source under rule 19 -- adds, never
    # subtracts; applied post-cantrip by matching the book's entries and
    # extending the same-turn injection delta.
    book_activations: list[str] = field(default_factory=list)
    # Package 3 (item 4): prompt injections the script requested. dicts of
    # {text, position}; applied post-cantrip through inject_entries.
    injections: list[dict] = field(default_factory=list)
    # Pipeline ordering (0.41.0): prompt strips the script requested via
    # context.strip(pattern, scope). dicts of {pattern, scope}; applied
    # host-side at the cantrip's step (manual order) or right after the
    # pre-driver pass (default order), rewriting message content before
    # later slots match against it. Collection only -- the sandbox never
    # compiles or executes the pattern.
    prompt_strips: list[dict] = field(default_factory=list)
    # Message-level history control (0.46.0): ops collected via
    # context.history.keep_recent(n) / context.history.drop(indices).
    history_ops: list[dict[str, Any]] = field(default_factory=list)
    debug_logs: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def has_error(self) -> bool:
        return self.error is not None

    @property
    def has_modifications(self) -> bool:
        return bool(self.personality or self.scenario or self.example_dialogs or self.response_content or self.tool_result)


class CantripTimeoutError(Exception):
    pass


class CantripExecutionError(Exception):
    pass


# Upper bound on the index list of one `context.history.drop(indices)` op.
MAX_HISTORY_DROP_INDICES = 200


def _parse_history_ops(raw: Any) -> list[dict[str, Any]]:
    """Validate and normalize history_ops emitted by the Deno runner."""
    if not isinstance(raw, list):
        return []
    ops: list[dict[str, Any]] = []
    for item in raw[:20]:
        if not isinstance(item, dict):
            continue
        op = item.get("op")
        if op == "keep_recent":
            count = item.get("count")
            if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
                ops.append({"op": "keep_recent", "count": count})
        elif op == "drop":
            raw_indices = item.get("indices")
            if isinstance(raw_indices, list):
                # Bounded like every other collected output (20 ops/request,
                # strips 256 chars): a drop cannot carry an unbounded index
                # list through runner stdout.
                indices = [
                    idx for idx in raw_indices[:MAX_HISTORY_DROP_INDICES]
                    if isinstance(idx, int) and not isinstance(idx, bool)
                ]
                if indices:
                    ops.append({"op": "drop", "indices": indices})
    return ops


# Cantrips get no permissions. Deno denies everything by default when no --allow-*
# flags are passed, so this list is behaviorally a no-op — it exists so the sandbox's
# posture is explicit and auditable in code rather than implied by the absence of flags.
# See Planning/security-control-document.md for rationale.
_DENO_DENY_FLAGS = [
    "--deny-net",
    "--deny-read",
    "--deny-write",
    "--deny-run",
    "--deny-env",
    "--deny-ffi",
    "--deny-sys",
    "--deny-import",
]


class DenoWorkerPool:
    """Dynamic pool of pre-warmed, single-use Deno workers.

    Each idle worker has already booted the V8 isolate with ``deno_worker.mjs``
    and is blocked on stdin waiting for one newline-delimited JSON job frame.
    Once checked out and fed a job, the worker runs the script, writes the
    result JSON to stdout, and exits -- never reused for a second job.
    """

    def __init__(
        self,
        min_idle: int = 1,
        max_idle: int = 4,
        idle_ttl_seconds: int = 60,
    ) -> None:
        self.min_idle: int = min_idle
        self.max_idle: int = max_idle
        self.idle_ttl_seconds: int | float = idle_ttl_seconds
        self._idle: deque[tuple[asyncio.subprocess.Process, float, asyncio.AbstractEventLoop]] = deque()
        self._spawn_tasks: set[asyncio.Task[None]] = set()
        self._trim_task: asyncio.Task[None] | None = None
        self.last_burst_at: float = 0.0
        self.total_warm_hits: int = 0
        self.total_cold_spawns: int = 0
        self._shutting_down: bool = False

    @staticmethod
    def _get_running_loop() -> asyncio.AbstractEventLoop | None:
        try:
            return asyncio.get_running_loop()
        except RuntimeError:
            return None

    @staticmethod
    def _kill_proc_sync(proc: asyncio.subprocess.Process) -> None:
        if proc.stdin is not None:
            with suppress(Exception):
                proc.stdin.close()
        if proc.returncode is None:
            with suppress(ProcessLookupError, OSError):
                proc.kill()

    def _discard_proc(
        self,
        proc: asyncio.subprocess.Process,
        proc_loop: asyncio.AbstractEventLoop,
        current_loop: asyncio.AbstractEventLoop | None,
    ) -> None:
        self._kill_proc_sync(proc)
        if current_loop is not None and proc_loop is current_loop and not current_loop.is_closed():
            task = current_loop.create_task(self._reap_proc(proc))
            task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

    @staticmethod
    def _close_transport(proc: asyncio.subprocess.Process) -> None:
        transport = getattr(proc, "_transport", None)
        if transport is not None:
            with suppress(Exception):
                transport.close()

    @staticmethod
    async def _reap_proc(proc: asyncio.subprocess.Process) -> None:
        with suppress(Exception):
            await proc.wait()
        DenoWorkerPool._close_transport(proc)

    def _prune_stale(self, current_loop: asyncio.AbstractEventLoop | None) -> None:
        kept: deque[tuple[asyncio.subprocess.Process, float, asyncio.AbstractEventLoop]] = deque()
        while self._idle:
            entry = self._idle.popleft()
            proc, _, proc_loop = entry
            if (
                proc.returncode is not None
                or proc_loop.is_closed()
                or (current_loop is not None and proc_loop is not current_loop)
            ):
                self._discard_proc(proc, proc_loop, current_loop)
            else:
                kept.append(entry)
        self._idle = kept

        stale_tasks = [
            t for t in self._spawn_tasks
            if t.done() or t.get_loop().is_closed() or (current_loop is not None and t.get_loop() is not current_loop)
        ]
        for t in stale_tasks:
            self._spawn_tasks.discard(t)

        if self._trim_task is not None and (
            self._trim_task.done()
            or self._trim_task.get_loop().is_closed()
            or (current_loop is not None and self._trim_task.get_loop() is not current_loop)
        ):
            self._trim_task = None

    def _trim_expired_burst(self, current_loop: asyncio.AbstractEventLoop | None) -> None:
        now = time.monotonic()
        while len(self._idle) > self.min_idle:
            proc, spawned_at, proc_loop = self._idle[0]
            if now - spawned_at >= self.idle_ttl_seconds:
                self._idle.popleft()
                self._discard_proc(proc, proc_loop, current_loop)
            else:
                break

    async def _spawn_worker(self) -> asyncio.subprocess.Process:
        return await asyncio.create_subprocess_exec(
            DENO_PATH,
            "run",
            "--quiet",
            *_DENO_DENY_FLAGS,
            str(_WORKER_PATH),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    async def _spawn_into_pool(self, loop: asyncio.AbstractEventLoop) -> None:
        try:
            proc = await self._spawn_worker()
        except asyncio.CancelledError:
            return
        except Exception:
            logger.debug("Failed to pre-warm Deno worker", exc_info=True)
            return

        if (
            self._shutting_down
            or proc.returncode is not None
            or len(self._idle) >= self.max_idle
            or self._get_running_loop() is not loop
        ):
            self._kill_proc_sync(proc)
            with suppress(Exception):
                await proc.wait()
            self._close_transport(proc)
            return

        self._idle.append((proc, time.monotonic(), loop))
        if len(self._idle) > self.min_idle:
            self._ensure_trim_timer(loop)

    def _schedule_spawns(self, target: int, loop: asyncio.AbstractEventLoop) -> None:
        target = min(max(0, target), self.max_idle)
        if target <= 0 or not DENO_PATH or self._shutting_down:
            return
        deficit = target - (len(self._idle) + len(self._spawn_tasks))
        for _ in range(max(0, deficit)):
            task = loop.create_task(self._spawn_into_pool(loop))
            self._spawn_tasks.add(task)
            task.add_done_callback(self._spawn_tasks.discard)

    def _ensure_trim_timer(self, loop: asyncio.AbstractEventLoop) -> None:
        if self._trim_task is None or self._trim_task.done():
            self._trim_task = loop.create_task(self._trim_loop(loop))

    async def _trim_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        try:
            while not self._shutting_down:
                self._prune_stale(loop)
                self._trim_expired_burst(loop)
                if len(self._idle) <= self.min_idle:
                    return
                oldest_spawned_at = self._idle[0][1]
                remaining = self.idle_ttl_seconds - (time.monotonic() - oldest_spawned_at)
                sleep_for = max(0.05, min(remaining, 1.0))
                await asyncio.sleep(sleep_for)
        except asyncio.CancelledError:
            return
        finally:
            if self._trim_task is asyncio.current_task():
                self._trim_task = None

    def configure(self, min_idle: int, max_idle: int, idle_ttl_seconds: int = 60) -> None:
        self.min_idle = max(0, min(16, int(min_idle)))
        self.max_idle = max(self.min_idle, min(32, int(max_idle)))
        self.idle_ttl_seconds = max(10, min(600, int(idle_ttl_seconds)))

        loop = self._get_running_loop()
        if loop is None:
            return

        self._prune_stale(loop)
        while len(self._idle) > self.max_idle:
            proc, _, proc_loop = self._idle.pop()
            self._discard_proc(proc, proc_loop, loop)

        if DENO_PATH and len(self._idle) + len(self._spawn_tasks) < self.min_idle:
            self._schedule_spawns(self.min_idle, loop)
        elif len(self._idle) > self.min_idle:
            self._ensure_trim_timer(loop)

    def ensure_warm(self, needed: int) -> None:
        if self.max_idle <= 0 or not DENO_PATH or self._shutting_down:
            return
        loop = self._get_running_loop()
        if loop is None:
            return

        self._prune_stale(loop)
        self._trim_expired_burst(loop)

        burst_target = min(max(self.min_idle, int(needed)), self.max_idle)
        self.last_burst_at = time.monotonic()
        if len(self._idle) + len(self._spawn_tasks) < burst_target:
            self._schedule_spawns(burst_target, loop)
        if burst_target > self.min_idle:
            self._ensure_trim_timer(loop)

    async def checkout(self) -> tuple[asyncio.subprocess.Process, bool]:
        loop = asyncio.get_running_loop()
        self._prune_stale(loop)
        self._trim_expired_burst(loop)

        while self._idle:
            proc, _, proc_loop = self._idle.popleft()
            if proc_loop is not loop or proc_loop.is_closed() or proc.returncode is not None:
                self._discard_proc(proc, proc_loop, loop)
                continue
            self.total_warm_hits += 1
            if self.max_idle > 0 and self.min_idle > 0:
                self._schedule_spawns(self.min_idle, loop)
            return proc, True

        proc = await self._spawn_worker()
        self.total_cold_spawns += 1
        if self.max_idle > 0 and self.min_idle > 0:
            self._schedule_spawns(self.min_idle, loop)
        return proc, False

    async def shutdown(self) -> None:
        self._shutting_down = True
        try:
            current_loop = self._get_running_loop()
            if self._trim_task is not None:
                if not self._trim_task.done() and (
                    current_loop is None or self._trim_task.get_loop() is current_loop
                ):
                    self._trim_task.cancel()
                    with suppress(asyncio.CancelledError, Exception):
                        await self._trim_task
                self._trim_task = None

            tasks = list(self._spawn_tasks)
            self._spawn_tasks.clear()
            for task in tasks:
                if not task.done() and (current_loop is None or task.get_loop() is current_loop):
                    task.cancel()
                    with suppress(asyncio.CancelledError, Exception):
                        await task

            while self._idle:
                proc, _, proc_loop = self._idle.popleft()
                self._kill_proc_sync(proc)
                if current_loop is not None and proc_loop is current_loop and not current_loop.is_closed():
                    with suppress(Exception):
                        await proc.wait()
                    self._close_transport(proc)
            if current_loop is not None and not current_loop.is_closed():
                await asyncio.sleep(0)
        finally:
            self._shutting_down = False

    def status(self) -> dict[str, Any]:
        current_loop = self._get_running_loop()
        self._prune_stale(current_loop)
        return {
            "idle_count": len(self._idle),
            "min_idle": self.min_idle,
            "max_idle": self.max_idle,
            "idle_ttl_seconds": self.idle_ttl_seconds,
            "total_warm_hits": self.total_warm_hits,
            "total_cold_spawns": self.total_cold_spawns,
        }

    def _kill_all_sync(self) -> None:
        while self._idle:
            proc, _, _ = self._idle.popleft()
            self._kill_proc_sync(proc)


deno_pool = DenoWorkerPool()
atexit.register(deno_pool._kill_all_sync)


async def _communicate_worker(
    proc: asyncio.subprocess.Process,
    job_bytes: bytes,
    passenger_handler: Any | None = None,
) -> tuple[bytes, int]:
    """Feed one job frame to a single-use Deno worker and collect its result."""
    assert proc.stdin is not None
    assert proc.stdout is not None
    assert proc.stderr is not None

    if passenger_handler is None:
        try:
            proc.stdin.write(job_bytes)
            await proc.stdin.drain()
            if proc.stdin.can_write_eof():
                with suppress(Exception):
                    proc.stdin.write_eof()
            else:
                with suppress(Exception):
                    proc.stdin.close()

            stdout_bytes, stderr_bytes = await asyncio.gather(
                proc.stdout.read(),
                proc.stderr.read(),
            )
            await proc.wait()

            if proc.returncode != 0 and stderr_bytes:
                stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
                if stderr_text:
                    logger.warning("Deno stderr: %s", stderr_text[:500])

            return stdout_bytes, proc.returncode or 0
        except asyncio.CancelledError:
            with suppress(ProcessLookupError, OSError):
                proc.kill()
            with suppress(Exception):
                await proc.wait()
            raise
        finally:
            with suppress(Exception):
                proc.stdin.close()
            DenoWorkerPool._close_transport(proc)

    # Interactive Passenger mode: keep stdin open for passenger_result replies.
    stdout_task = asyncio.create_task(proc.stdout.read())
    answers: set[asyncio.Task[None]] = set()
    try:
        proc.stdin.write(job_bytes)
        await proc.stdin.drain()

        while True:
            raw = await proc.stderr.readline()
            if not raw:
                break  # stderr closed: the process is exiting
            line = raw.decode("utf-8", errors="replace").rstrip("\n")
            if not line.startswith(_MSG_PREFIX):
                if line.strip():
                    logger.debug("Deno stderr: %s", line[:300])
                continue
            try:
                msg = json.loads(line[len(_MSG_PREFIX):])
            except json.JSONDecodeError:
                logger.debug("Passenger: unparseable protocol line: %s", line[:200])
                continue
            if not isinstance(msg, dict) or msg.get("type") != "passenger_call":
                continue
            task = asyncio.create_task(_answer_passenger_call(proc, passenger_handler, msg))
            answers.add(task)
            task.add_done_callback(answers.discard)

        stdout_bytes = await stdout_task
        await proc.wait()
        return stdout_bytes, proc.returncode or 0
    except asyncio.CancelledError:
        stdout_task.cancel()
        for task in answers:
            task.cancel()
        with suppress(ProcessLookupError, OSError):
            proc.kill()
        with suppress(Exception):
            await proc.wait()
        raise
    finally:
        with suppress(Exception):
            proc.stdin.close()
        DenoWorkerPool._close_transport(proc)


async def run_cantrip(
    code: str,
    context: dict,
    chat_data: dict | None = None,
    user_data: dict | None = None,
    cantrip_data: dict | None = None,
    timeout_ms: int = 5000,
    passenger_handler: Any | None = None,
) -> CantripResult:
    if not DENO_PATH:
        raise CantripExecutionError(
            "Deno runtime not found. Set GITV_DENO_PATH or install Deno."
        )

    chat_data = chat_data or {}
    user_data = user_data or {}
    cantrip_data = cantrip_data or {}

    # Passenger rides only when a handler exists AND its gates are on; a
    # disabled handler produces the same payload as no handler at all
    # (enabled=false, call() -> null, zero protocol traffic).
    passenger_cfg: dict | None = None
    watchdog_ms = timeout_ms
    if passenger_handler is not None and getattr(passenger_handler, "enabled", False):
        passenger_cfg = {
            "enabled": True,
            "maxCalls": max(0, int(passenger_handler.max_calls)),
        }
        # The script's own budget plus every Passenger call's upstream
        # timeout, plus slack: the plain cantrip timeout would kill every
        # Passenger run before the first call completes.
        watchdog_ms = passenger_handler.watchdog_ms(timeout_ms)

    job_payload = {
        "context": context,
        "chat_data": chat_data,
        "user_data": user_data,
        "cantrip_data": cantrip_data,
        "user_code": code,
        "passenger_cfg": passenger_cfg,
    }
    job_bytes = json.dumps(job_payload, separators=(",", ":")).encode("utf-8") + b"\n"

    proc, _was_warm = await deno_pool.checkout()

    try:
        stdout_bytes, return_code = await asyncio.wait_for(
            _communicate_worker(
                proc,
                job_bytes,
                passenger_handler if passenger_cfg is not None else None,
            ),
            timeout=watchdog_ms / 1000,
        )
    except TimeoutError:
        with suppress(ProcessLookupError, OSError):
            proc.kill()
        with suppress(Exception):
            await proc.wait()
        raise CantripTimeoutError(
            f"Script timed out after {watchdog_ms if passenger_cfg else timeout_ms}ms"
        ) from None

    stdout = stdout_bytes.decode("utf-8", errors="replace").strip()

    if return_code != 0 and not stdout:
        raise CantripExecutionError(
            f"Deno process exited with code {return_code}"
        )

    try:
        result_data = json.loads(stdout)
    except json.JSONDecodeError:
        raise CantripExecutionError(
            f"Deno produced invalid output: {stdout[:500]}"
        ) from None

    return CantripResult(
        personality=result_data.get("personality", ""),
        scenario=result_data.get("scenario", ""),
        example_dialogs=result_data.get("example_dialogs", ""),
        response_content=result_data.get("response_content"),
        tool_result=result_data.get("tool_result", ""),
        chat_data=result_data.get("chat_data", {}),
        user_data=result_data.get("user_data", {}),
        cantrip_data=result_data.get("cantrip_data", {}),
        chat_data_deletes=result_data.get("chat_data_deletes", []),
        user_data_deletes=result_data.get("user_data_deletes", []),
        cantrip_data_deletes=result_data.get("cantrip_data_deletes", []),
        memories=result_data.get("memories", {}),
        memory_deletes=result_data.get("memory_deletes", []),
        maps_nominated=result_data.get("maps_nominated", []),
        lorebook_ops=result_data.get("lorebook_ops", []),
        book_activations=result_data.get("book_activations", []),
        injections=result_data.get("injections", []),
        prompt_strips=result_data.get("prompt_strips", []),
        history_ops=_parse_history_ops(result_data.get("history_ops")),
        debug_logs=result_data.get("debug_logs", []),
        error=result_data.get("error"),
    )


# Sentinel prefixing protocol traffic on the child's stderr. Deno diagnostics
# never carry it, so the two cannot be confused. Must match deno_worker.mjs.
_MSG_PREFIX = "__GITV_MSG__ "


async def _answer_passenger_call(
    proc: asyncio.subprocess.Process, passenger_handler: Any, msg: dict
) -> None:
    """Execute one Passenger request from the sandbox and reply on stdin.

    ``PassengerHandler.call`` never raises; a reply is always written unless
    the process died first (broken pipe -- the script abandoned the call or
    the watchdog fired).
    """
    text, outcome = await passenger_handler.call(
        tag=str(msg.get("tag") or ""),
        instructions=str(msg.get("instructions") or ""),
        context=str(msg.get("context") or ""),
        model=str(msg.get("model") or ""),
    )
    reply: dict[str, Any] = {"type": "passenger_result", "id": msg.get("id")}
    if text is not None:
        reply["ok"] = True
        reply["text"] = text
    else:
        reply["ok"] = False
        reply["error"] = outcome
    try:
        proc.stdin.write(json.dumps(reply).encode("utf-8") + b"\n")
        await proc.stdin.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass


def _validate_syntax_sync(code: str) -> str:
    """Parse-check cantrip code with the runner's async wrapper in a bare Deno run.

    Parses exactly what deno_worker.mjs will compile -- ``new Function`` around
    the user code wrapped in an async IIFE, with the same trailing-brace trim
    fallback -- so a syntax error surfaces at write time, not first run. The
    async wrapper only adds capability (top-level ``await``), so code valid
    under the old bare check stays valid.

    Returns "" when the code parses, else the parse error. Without a Deno
    binary there is nothing to check and the result is "" (valid) -- matching
    the historical /api/cantrips/validate behavior. Synchronous subprocess;
    call via run_in_executor off the event loop.
    """
    import json as _json

    if not DENO_PATH:
        return ""

    wrapper = (
        f"const __userCode = {_json.dumps(code)};\n"
        "try {\n"
        "    try {\n"
        "        new Function('context', 'return (async () => {\\n' + __userCode + '\\n})();');\n"
        "    } catch (e1) {\n"
        "        const trimmed = __userCode.replace(/\\n\\s*\\}\\s*$/, '');\n"
        "        new Function('context', 'return (async () => {\\n' + trimmed + '\\n})();');\n"
        "    }\n"
        "} catch (e) {\n"
        "    Deno.stdout.writeSync(new TextEncoder().encode(JSON.stringify({error: e.message})));\n"
        "    Deno.exit(1);\n"
        "}\n"
        "Deno.stdout.writeSync(new TextEncoder().encode(JSON.stringify({error: null})));\n"
    )

    fd, temp_path = tempfile.mkstemp(suffix=".mjs", prefix="gitv_validate_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(wrapper)

        result = subprocess.run(
            [DENO_PATH, "run", "--quiet", *_DENO_DENY_FLAGS, temp_path],
            capture_output=True, timeout=5,
        )
        stdout = result.stdout.decode("utf-8", errors="replace").strip() if result.stdout else ""
        if stdout:
            try:
                data = _json.loads(stdout)
                if data.get("error"):
                    return str(data["error"])
            except _json.JSONDecodeError:
                pass
        return ""
    except Exception as e:
        return str(e)
    finally:
        try:
            os.unlink(temp_path)
        except OSError:
            pass


def validate_cantrip_syntax(code: str) -> str:
    """`_validate_syntax_sync` with the name the callers use. See its docstring."""
    return _validate_syntax_sync(code)
