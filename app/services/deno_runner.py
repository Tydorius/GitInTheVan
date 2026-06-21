from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

_TEMPLATE_PATH = Path(__file__).parent / "deno_template.mjs"


def _find_deno() -> str | None:
    env_path = os.environ.get("GITV_DENO_PATH", "")
    if env_path and Path(env_path).exists():
        return env_path

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


def _build_runner_script(context: dict, chat_data: dict, user_code: str) -> str:
    template = _TEMPLATE_PATH.read_text(encoding="utf-8")
    context_b64 = base64.b64encode(json.dumps(context).encode("utf-8")).decode("ascii")
    chatdata_b64 = base64.b64encode(json.dumps(chat_data).encode("utf-8")).decode("ascii")
    usercode_b64 = base64.b64encode(user_code.encode("utf-8")).decode("ascii")

    return (
        template
        .replace("__GITV_CONTEXT__", context_b64)
        .replace("__GITV_CHATDATA__", chatdata_b64)
        .replace("__GITV_USERCODE__", usercode_b64)
    )


async def run_cantrip(
    code: str,
    context: dict,
    chat_data: dict | None = None,
    timeout_ms: int = 5000,
) -> CantripResult:
    if not DENO_PATH:
        raise CantripExecutionError(
            "Deno runtime not found. Set GITV_DENO_PATH or install Deno."
        )

    chat_data = chat_data or {}
    runner_script = _build_runner_script(context, chat_data, code)

    fd, temp_path = tempfile.mkstemp(suffix=".mjs", prefix="gitv_cantrip_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(runner_script)

        try:
            stdout_bytes, return_code = await _execute_deno(temp_path, timeout_ms)
        except TimeoutError:
            raise CantripTimeoutError(
                f"Script timed out after {timeout_ms}ms"
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
            debug_logs=result_data.get("debug_logs", []),
            error=result_data.get("error"),
        )
    finally:
        try:
            os.unlink(temp_path)
        except OSError:
            pass


def _run_deno_sync(script_path: str, timeout_ms: int) -> tuple[bytes, int, bytes]:
    try:
        result = subprocess.run(
            [DENO_PATH, "run", "--quiet", script_path],
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
