from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import shutil
import subprocess
import tempfile
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_TEMPLATE_PATH = Path(__file__).parent / "deno_template.mjs"


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


def _build_runner_script(
    context: dict,
    chat_data: dict,
    user_data: dict,
    cantrip_data: dict,
    user_code: str,
    passenger_cfg: dict | None = None,
) -> str:
    template = _TEMPLATE_PATH.read_text(encoding="utf-8")
    context_b64 = base64.b64encode(json.dumps(context).encode("utf-8")).decode("ascii")
    chatdata_b64 = base64.b64encode(json.dumps(chat_data).encode("utf-8")).decode("ascii")
    userdata_b64 = base64.b64encode(json.dumps(user_data).encode("utf-8")).decode("ascii")
    cantripdata_b64 = base64.b64encode(json.dumps(cantrip_data).encode("utf-8")).decode("ascii")
    usercode_b64 = base64.b64encode(user_code.encode("utf-8")).decode("ascii")

    return (
        template
        .replace("__GITV_CONTEXT__", context_b64)
        .replace("__GITV_CHATDATA__", chatdata_b64)
        .replace("__GITV_USERDATA__", userdata_b64)
        .replace("__GITV_CANTRIPDATA__", cantripdata_b64)
        .replace("__GITV_USERCODE__", usercode_b64)
        .replace("__GITV_PASSENGER__", json.dumps(passenger_cfg) if passenger_cfg else "null")
    )


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
    # disabled handler produces the same template as no handler at all
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

    runner_script = _build_runner_script(
        context, chat_data, user_data, cantrip_data, code, passenger_cfg
    )

    fd, temp_path = tempfile.mkstemp(suffix=".mjs", prefix="gitv_cantrip_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(runner_script)

        try:
            if passenger_cfg is not None:
                stdout_bytes, return_code = await _execute_deno_interactive(
                    temp_path, watchdog_ms, passenger_handler
                )
            else:
                stdout_bytes, return_code = await _execute_deno(temp_path, timeout_ms)
        except TimeoutError:
            raise CantripTimeoutError(
                f"Script timed out after {watchdog_ms if passenger_cfg else timeout_ms}ms"
            )

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
            )

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
            debug_logs=result_data.get("debug_logs", []),
            error=result_data.get("error"),
        )
    finally:
        try:
            os.unlink(temp_path)
        except OSError:
            pass


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


def _run_deno_sync(script_path: str, timeout_ms: int) -> tuple[bytes, int, bytes]:
    try:
        result = subprocess.run(
            [DENO_PATH, "run", "--quiet", *_DENO_DENY_FLAGS, script_path],
            capture_output=True,
            timeout=timeout_ms / 1000,
        )
        return result.stdout, result.returncode, result.stderr
    except subprocess.TimeoutExpired:
        return b"", 124, b"timeout"
    except Exception as e:
        return b"", 1, str(e).encode()


async def _execute_deno(script_path: str, timeout_ms: int) -> tuple[bytes, int]:
    loop = asyncio.get_event_loop()
    stdout, return_code, stderr = await loop.run_in_executor(
        None, _run_deno_sync, script_path, timeout_ms
    )

    if return_code == 124:
        raise TimeoutError(f"Deno timed out after {timeout_ms}ms")

    if return_code != 0 and stderr:
        stderr_text = stderr.decode("utf-8", errors="replace").strip()
        if stderr_text:
            logger.warning("Deno stderr: %s", stderr_text[:500])

    return stdout, return_code


# Sentinel prefixing protocol traffic on the child's stderr. Deno diagnostics
# never carry it, so the two cannot be confused. Must match deno_template.mjs.
_MSG_PREFIX = "__GITV_MSG__ "


async def _execute_deno_interactive(
    script_path: str, watchdog_ms: int, passenger_handler: Any
) -> tuple[bytes, int]:
    """Run a cantrip with the Passenger protocol attached.

    Same command line and deny flags as the plain path; stdin/stdout/stderr
    are pipes driven by an asyncio read/write loop instead of
    ``subprocess.run``. Requires subprocess support on the event loop, which
    on Windows means the Proactor policy -- uvicorn's default. The watchdog
    covers the script budget plus every Passenger call's upstream timeout;
    on expiry the process is killed and the caller sees ``TimeoutError``.
    """
    try:
        return await asyncio.wait_for(
            _run_deno_interactive(script_path, passenger_handler),
            timeout=watchdog_ms / 1000,
        )
    except TimeoutError:
        raise TimeoutError(f"Deno timed out after {watchdog_ms}ms") from None


async def _run_deno_interactive(script_path: str, passenger_handler: Any) -> tuple[bytes, int]:
    proc = await asyncio.create_subprocess_exec(
        DENO_PATH, "run", "--quiet", *_DENO_DENY_FLAGS, script_path,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_task = asyncio.create_task(proc.stdout.read())
    answers: set[asyncio.Task] = set()
    try:
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
        # Watchdog fired (or the server is shutting down): take the process
        # down and let in-flight answers fail on their broken pipes.
        stdout_task.cancel()
        for task in answers:
            task.cancel()
        with suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        raise
    finally:
        # The template closes its own stdin when the user code completes; this
        # covers any exit path that skipped it, so a blocked reply reader can
        # never hold the process open past its own completion.
        if proc.stdin is not None:
            proc.stdin.close()


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

    Parses exactly what the template will compile -- ``new Function`` around
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
