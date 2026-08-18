"""Cross-platform test harness for GitInTheVan.

Provisions a throwaway install on a target (macOS, Linux, Docker host, or this
Windows machine), exercises it, archives the logs, and deletes itself.

The point is that verifying a branch is one repeatable command per target
rather than a manual afternoon -- and that nothing is ever deleted by hand on
a machine holding real work.

    python testing/harness.py -env testing/harness.env -target linux -branch main up test hold
    python testing/harness.py -env testing/harness.env -target linux logs down

Subcommands chain left to right. Run state is persisted to testing/runs/, so
`down` works from a later session than `up`.

Stdlib only, on purpose: it must run before any virtualenv exists.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTING_DIR = REPO_ROOT / "testing"
RUNS_DIR = TESTING_DIR / "runs"
REMOTE_DIR = TESTING_DIR / "remote"

# Every run directory lives under this name, and teardown refuses to delete a
# path that does not contain it. The marker file is the second gate.
RUN_ROOT_NAME = "_gitv-testruns"
MARKER_NAME = ".gitv-testrun"

LINE = "=" * 72

# The instance under test serves a self-signed certificate by design, so
# verification is disabled for these loopback/LAN checks.
_UNVERIFIED = ssl.create_default_context()
_UNVERIFIED.check_hostname = False
_UNVERIFIED.verify_mode = ssl.CERT_NONE


# --------------------------------------------------------------- plumbing ---

def say(msg: str) -> None:
    print(f"[harness] {msg}", flush=True)


def section(title: str) -> None:
    print(f"\n{LINE}\n  {title}\n{LINE}", flush=True)


class HarnessError(RuntimeError):
    """Anything that should stop the run with a non-zero exit code."""


# ----------------------------------------------------------------- config ---

@dataclass
class TargetSpec:
    name: str
    dest: str            # ssh destination, or "localhost"
    folder: str          # parent folder on the target
    kind: str            # posix | windows | docker
    compose_file: str = ""
    jump: str = ""       # ProxyJump host, resolved per target

    @property
    def is_local(self) -> bool:
        return self.dest.strip().lower() == "localhost"


def load_config(path: Path) -> dict[str, str]:
    """Parse a KEY=value file. '#' starts a comment; values are not quoted."""
    if not path.exists():
        raise HarnessError(
            f"config not found: {path}\n"
            f"Copy testing/harness.env.example to {path} and fill it in."
        )
    cfg: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        # Strip trailing comments, but not '#' inside a Windows path.
        value = re.sub(r"\s+#.*$", "", value).strip()
        cfg[key.strip()] = value
    return cfg


def resolve_jump(cfg: dict[str, str], name: str, override: str | None = None) -> str:
    """Pick the ProxyJump host for one target.

    A single global jump is wrong for a mixed network: routing a directly
    reachable machine through a bastion is at best pointless and at worst
    broken. Precedence, most specific first:

        -jump on the command line   ('none' disables it for this run)
        <TARGET>_SSH_JUMP           (present-but-empty means "no jump")
        SSH_JUMP                    (fallback for every target)
    """
    if override is not None:
        return "" if override.strip().lower() in ("none", "") else override.strip()

    specific = f"{name.upper()}_SSH_JUMP"
    if specific in cfg:
        # Present but empty is a deliberate opt-out, not a missing value.
        return cfg[specific].strip()
    return cfg.get("SSH_JUMP", "").strip()


def build_target(cfg: dict[str, str], name: str, jump_override: str | None = None) -> TargetSpec:
    table = {
        "macos":   ("TARGET_MACOS", "MACOS_FOLDER", "posix"),
        "linux":   ("TARGET_LINUX", "LINUX_FOLDER", "posix"),
        "docker":  ("TARGET_DOCKER", "DOCKER_FOLDER", "docker"),
        "windows": ("TARGET_WINDOWS", "WINDOWS_FOLDER", "windows"),
    }
    if name not in table:
        raise HarnessError(f"unknown target '{name}'; expected one of {', '.join(table)}")

    dest_key, folder_key, kind = table[name]
    dest = cfg.get(dest_key, "").strip()
    folder = cfg.get(folder_key, "").strip()
    if not dest:
        raise HarnessError(f"{dest_key} is not set in the config; target '{name}' is disabled")
    if not folder:
        raise HarnessError(f"{folder_key} is not set in the config")

    return TargetSpec(
        name=name, dest=dest, folder=folder, kind=kind,
        compose_file=cfg.get("DOCKER_COMPOSE_FILE", "docker-compose.sqlite.yml"),
        jump=resolve_jump(cfg, name, jump_override),
    )


# -------------------------------------------------------------- transport ---

class Transport:
    """Runs commands and moves files, locally or over SSH.

    'localhost' is handled without SSH at all, so the Windows target needs no
    sshd and no key material.
    """

    def __init__(self, target: TargetSpec, ssh_opts: str):
        self.target = target
        self.ssh_opts = shlex.split(ssh_opts) if ssh_opts else []
        self.jump = target.jump.strip()

    def _ssh_base(self) -> list[str]:
        cmd = ["ssh", *self.ssh_opts]
        if self.jump:
            cmd += ["-J", self.jump]
        return cmd

    def run(self, command: str, check: bool = True, timeout: int = 1800,
            capture: bool = True) -> subprocess.CompletedProcess:
        if self.target.is_local:
            if self.target.kind == "windows":
                argv = ["powershell", "-NoProfile", "-NonInteractive", "-Command", command]
            else:
                argv = ["bash", "-lc", command]
        else:
            argv = [*self._ssh_base(), self.target.dest, command]

        # Output goes to temp files, never pipes. capture_output=True reads
        # until EOF, and EOF only arrives once every inherited copy of the
        # handle is closed -- so a provisioning step that deliberately leaves a
        # server running in the background hangs the harness forever, even
        # though the command it ran exited cleanly. Observed on the Windows
        # target: the instance was healthy and provision-windows.ps1 had
        # exited, but the detached server still held the pipe.
        # ignore_cleanup_errors: the same inherited handles that made pipes
        # hang also keep these files open, and Windows refuses to unlink a
        # file a live process still holds. The content has already been read
        # by then, so a leftover temp file is cosmetic; failing the run over
        # it is not.
        with tempfile.TemporaryDirectory(prefix="gitv-harness-",
                                         ignore_cleanup_errors=True) as tmp:
            out_path = Path(tmp) / "stdout.txt"
            err_path = Path(tmp) / "stderr.txt"
            with open(out_path, "wb") as out, open(err_path, "wb") as err:
                code = subprocess.call(argv, stdout=out, stderr=err, timeout=timeout)
            stdout = out_path.read_text(encoding="utf-8", errors="replace")
            stderr = err_path.read_text(encoding="utf-8", errors="replace")

        proc = subprocess.CompletedProcess(argv, code, stdout, stderr)
        if check and proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            raise HarnessError(
                f"command failed on {self.target.name} (exit {proc.returncode}):\n"
                f"  {command}\n{detail[-2000:]}"
            )
        return proc

    def push(self, local: Path, remote: str) -> None:
        if self.target.is_local:
            dest = Path(self._localise(remote))
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(local, dest)
            return
        cmd = ["scp", *self.ssh_opts]
        if self.jump:
            cmd += ["-J", self.jump]
        cmd += [str(local), f"{self.target.dest}:{remote}"]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if proc.returncode != 0:
            raise HarnessError(f"scp push failed: {proc.stderr.strip()}")

    def pull(self, remote: str, local: Path) -> bool:
        local.parent.mkdir(parents=True, exist_ok=True)
        if self.target.is_local:
            src = Path(self._localise(remote))
            if not src.exists():
                return False
            if src.is_dir():
                shutil.copytree(src, local, dirs_exist_ok=True)
            else:
                shutil.copy2(src, local)
            return True
        cmd = ["scp", *self.ssh_opts, "-r"]
        if self.jump:
            cmd += ["-J", self.jump]
        cmd += [f"{self.target.dest}:{remote}", str(local)]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        return proc.returncode == 0

    def _localise(self, remote: str) -> str:
        return remote.replace("/", os.sep) if os.name == "nt" else remote


# ------------------------------------------------------------- run state ----

@dataclass
class RunState:
    run_id: str
    target: str
    kind: str
    dest: str
    run_dir: str
    branch: str
    port: int
    mock_port: int
    commit: str = ""
    scheme: str = "http"
    compose_project: str = ""
    started_at: str = ""
    replicated: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def path(self) -> Path:
        return RUNS_DIR / f"{self.run_id}.json"

    def save(self) -> None:
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")

    @staticmethod
    def load(run_id: str) -> RunState:
        p = RUNS_DIR / f"{run_id}.json"
        if not p.exists():
            raise HarnessError(f"no run state for '{run_id}' at {p}")
        return RunState(**json.loads(p.read_text(encoding="utf-8")))

    @staticmethod
    def latest(target: str) -> RunState:
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        runs = sorted(RUNS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        for p in runs:
            data = json.loads(p.read_text(encoding="utf-8"))
            if data.get("target") == target:
                return RunState(**data)
        raise HarnessError(
            f"no previous run found for target '{target}'. Run `up` first, "
            f"or pass -run <run-id>."
        )


# ----------------------------------------------------------------- http -----

def http_json(url: str, method: str = "GET", body: dict | None = None,
              token: str = "", timeout: int = 20) -> tuple[int, dict | str]:
    """Minimal JSON client. urllib, so the harness needs no dependencies."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        ctx = _UNVERIFIED if url.startswith("https") else None
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                return resp.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return e.code, raw
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return 0, str(e)


def wait_healthy(base: str, timeout: int = 600) -> bool:
    """Two consecutive OKs.

    One is not enough: the updater's maintenance page binds the same port and
    answers every path, so a listening socket proves nothing. Mirrors the
    reasoning in app/services/updater.py.
    """
    deadline = time.time() + timeout
    consecutive = 0
    while time.time() < deadline:
        status, payload = http_json(f"{base}/health", timeout=5)
        if status == 200 and isinstance(payload, dict) and payload.get("status") == "ok":
            consecutive += 1
            if consecutive >= 2:
                return True
        else:
            consecutive = 0
        time.sleep(3)
    return False


# ------------------------------------------------------------------- up -----

def remote_join(target: TargetSpec, *parts: str) -> str:
    sep = "\\" if target.kind == "windows" and target.is_local else "/"
    base = target.folder.rstrip("/\\")
    return sep.join([base, *parts])


def cmd_up(cfg: dict, target: TargetSpec, tr: Transport, args) -> RunState:
    section(f"UP  target={target.name}  branch={args.branch}")

    run_id = datetime.now(UTC).strftime("%Y%m%d-%H%M%S") + f"-{os.getpid() % 9973:04d}"
    run_dir = remote_join(target, RUN_ROOT_NAME, run_id)
    port = int(cfg.get("GITV_PORT", "8100"))
    mock_port = 0 if args.replicate else port + 99

    state = RunState(
        run_id=run_id, target=target.name, kind=target.kind, dest=target.dest,
        run_dir=run_dir, branch=args.branch, port=port, mock_port=mock_port,
        started_at=datetime.now(UTC).isoformat(), replicated=args.replicate,
    )
    state.save()
    say(f"run id {run_id}")
    say(f"run dir {run_dir}")

    # Create the run dir and drop the marker before anything else, so that even
    # a failed provision leaves a directory teardown is willing to remove.
    if target.kind == "windows" and target.is_local:
        tr.run(
            f"New-Item -ItemType Directory -Force -Path '{run_dir}' | Out-Null; "
            f"Set-Content -Path '{run_dir}\\{MARKER_NAME}' -Value '{run_id}'"
        )
    else:
        tr.run(f"mkdir -p {shlex.quote(run_dir)} && printf '%s' {shlex.quote(run_id)} "
               f"> {shlex.quote(run_dir + '/' + MARKER_NAME)}")

    # Ship the provisioning scripts rather than embedding them in SSH strings.
    if target.kind == "docker":
        script = REMOTE_DIR / "provision-docker.sh"
    elif target.kind == "windows":
        script = REMOTE_DIR / "provision-windows.ps1"
    else:
        script = REMOTE_DIR / "provision-posix.sh"

    tr.push(script, f"{run_dir}/{script.name}")
    if target.kind != "docker":
        tr.push(REMOTE_DIR / "wait_health.py", f"{run_dir}/wait_health.py")
    if mock_port:
        tr.push(REMOTE_DIR / "mock_upstream.py", f"{run_dir}/mock_upstream.py")

    repo_url = cfg.get("REPO_URL", "").strip()
    if not repo_url:
        raise HarnessError("REPO_URL is not set in the config")

    section("PROVISION (clone + real deploy script + health poll)")
    if target.kind == "docker":
        cmd = (f"bash {shlex.quote(run_dir + '/' + script.name)} "
               f"{shlex.quote(run_dir)} {shlex.quote(repo_url)} {shlex.quote(args.branch)} "
               f"{port} {shlex.quote(target.compose_file)}")
    elif target.kind == "windows":
        cmd = (f"powershell -NoProfile -ExecutionPolicy Bypass -File "
               f"'{run_dir}\\{script.name}' -RunDir '{run_dir}' -RepoUrl '{repo_url}' "
               f"-Branch '{args.branch}' -Port {port} -MockPort {mock_port}")
    else:
        cmd = (f"bash {shlex.quote(run_dir + '/' + script.name)} "
               f"{shlex.quote(run_dir)} {shlex.quote(repo_url)} {shlex.quote(args.branch)} "
               f"{port} {mock_port}")

    proc = tr.run(cmd, check=False, timeout=2400)
    print(proc.stdout or "", flush=True)
    if proc.returncode != 0:
        print(proc.stderr or "", file=sys.stderr, flush=True)
        state.notes.append("provision failed")
        state.save()
        raise HarnessError(
            f"provisioning failed on {target.name}. The run directory was left in "
            f"place for inspection: {run_dir}\nTear it down with: "
            f"-target {target.name} down"
        )

    for line in (proc.stdout or "").splitlines():
        if line.startswith("PROVISION_OK"):
            for token in line.split()[1:]:
                key, _, value = token.partition("=")
                if key == "commit":
                    state.commit = value
                elif key == "scheme":
                    state.scheme = value
                elif key == "project":
                    state.compose_project = value
    state.save()

    base = instance_url(state, cfg)
    say(f"instance reachable at {base}")
    if not wait_healthy(base, timeout=180):
        raise HarnessError(
            f"provisioning reported success but {base}/health is not reachable "
            f"from this machine. Check firewall rules on the target."
        )

    bootstrap_admin(state, cfg, base)
    seed_endpoint(state, cfg, base, args)
    section("UP COMPLETE")
    return state


def instance_url(state: RunState, cfg: dict) -> str:
    """URL this machine uses to reach the instance."""
    host = state.dest.split("@")[-1] if "@" in state.dest else state.dest
    if host.lower() == "localhost":
        host = "127.0.0.1"
    return f"{state.scheme}://{host}:{state.port}"


def bootstrap_admin(state: RunState, cfg: dict, base: str) -> None:
    user = cfg.get("ADMIN_USER", "admin")
    password = cfg.get("ADMIN_PASSWORD", "")

    # Fail with the real reason rather than a bare 400 from the server.
    if len(password) < 8 or not any(c.isalpha() for c in password) \
            or not any(c.isdigit() for c in password):
        raise HarnessError(
            "ADMIN_PASSWORD does not satisfy the app's password rules "
            "(8+ characters, at least one letter and one digit). "
            "'testpassword' is rejected; 'testpassword1' works."
        )

    status, payload = http_json(f"{base}/api/auth/setup", "POST",
                                {"username": user, "password": password})
    if status == 201:
        say(f"admin '{user}' created")
    elif status == 409:
        say(f"admin already exists; reusing '{user}'")
    else:
        raise HarnessError(f"admin creation failed ({status}): {payload}")


def admin_token(cfg: dict, base: str) -> str:
    status, payload = http_json(f"{base}/api/auth/login", "POST", {
        "username": cfg.get("ADMIN_USER", "admin"),
        "password": cfg.get("ADMIN_PASSWORD", ""),
    })
    if status != 200 or not isinstance(payload, dict):
        raise HarnessError(f"admin login failed ({status}): {payload}")
    return payload.get("access_token", "")


def seed_endpoint(state: RunState, cfg: dict, base: str, args) -> None:
    token = admin_token(cfg, base)

    if args.replicate:
        count = replicate_endpoints(base, token)
        say(f"replicated {count} endpoint(s) from the local database")
        return

    body = {
        "name": "Mock Upstream",
        "base_url": f"http://127.0.0.1:{state.mock_port}",
        "api_key": "mock-key-not-a-real-credential",
        "api_base_path": "",
        "default_model": "mock-model-v1",
        "enabled": True,
        "role_tag": "default",
        "priority": 1,
    }
    status, payload = http_json(f"{base}/api/endpoints", "POST", body, token=token)
    if status not in (200, 201):
        raise HarnessError(f"could not create the mock endpoint ({status}): {payload}")
    say(f"mock endpoint created against 127.0.0.1:{state.mock_port}")


def replicate_endpoints(base: str, token: str) -> int:
    """Copy endpoint rows out of the local database onto the test instance.

    Opt-in only. Endpoint.api_key is plaintext at rest, so this puts live
    billable credentials on a throwaway machine; the log archiver scrubs them
    on the way back out, but they still exist in that instance's database
    until teardown.
    """
    import sqlite3

    db = REPO_ROOT / "data" / "gitinthevan.db"
    if not db.exists():
        raise HarnessError(f"-replicate needs a local database at {db}")

    say("WARNING: -replicate copies real API keys to the target host")
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT name, base_url, api_key, api_base_path, provider, "
            "default_model, bypass_method, enabled, role_tag, priority, custom_tag "
            "FROM endpoints"
        ).fetchall()
    finally:
        conn.close()

    created = 0
    for row in rows:
        body = {k: (row[k] if row[k] is not None else "") for k in row.keys()}
        body["enabled"] = bool(body.get("enabled", 1))
        body["priority"] = int(body.get("priority") or 1)
        status, _ = http_json(f"{base}/api/endpoints", "POST", body, token=token)
        if status in (200, 201):
            created += 1
    if not created:
        raise HarnessError("-replicate found no endpoints to copy")
    return created


# ----------------------------------------------------------------- test -----

def cmd_test(cfg: dict, target: TargetSpec, tr: Transport, state: RunState) -> bool:
    section(f"TEST  target={target.name}  run={state.run_id}")
    base = instance_url(state, cfg)
    ok = True

    ok &= run_flow_test(cfg, target, tr, state, base)
    ok &= local_checks(cfg, state, base)

    section("TEST RESULT: " + ("PASS" if ok else "FAIL"))
    return ok


def run_flow_test(cfg, target: TargetSpec, tr: Transport, state: RunState, base: str) -> bool:
    """flow_test.py drives the full proxy pipeline against the instance.

    For native installs it runs on the target using that install's own venv,
    which is the honest end-user check. For Docker it runs from this machine
    instead: scripts/ is not copied into the image, so there is no in-container
    copy to run.
    """
    section("flow_test.py")
    user = cfg.get("ADMIN_USER", "admin")
    password = cfg.get("ADMIN_PASSWORD", "")

    if target.kind == "docker":
        py = sys.executable
        script = REPO_ROOT / "scripts" / "flow_test.py"
        argv = [py, str(script), "--server", base,
                "--admin-user", user, "--admin-pass", password]
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=1800)
        print(proc.stdout[-8000:], flush=True)
        if proc.returncode != 0:
            print(proc.stderr[-4000:], file=sys.stderr, flush=True)
        return proc.returncode == 0

    src = f"{state.run_dir}/GitInTheVan"
    if target.kind == "windows":
        cmd = (f"& '{src}\\.venv\\Scripts\\python.exe' '{src}\\scripts\\flow_test.py' "
               f"--server '{state.scheme}://127.0.0.1:{state.port}' "
               f"--admin-user '{user}' --admin-pass '{password}'")
    else:
        cmd = (f"cd {shlex.quote(src)} && ./.venv/bin/python scripts/flow_test.py "
               f"--server {state.scheme}://127.0.0.1:{state.port} "
               f"--admin-user {shlex.quote(user)} --admin-pass {shlex.quote(password)}")

    proc = tr.run(cmd, check=False, timeout=1800)
    print((proc.stdout or "")[-8000:], flush=True)
    if proc.returncode != 0:
        print((proc.stderr or "")[-4000:], file=sys.stderr, flush=True)
    return proc.returncode == 0


def local_checks(cfg: dict, state: RunState, base: str) -> bool:
    """Cross-network checks driven from this machine.

    Covers the things an on-target test cannot: that the instance is actually
    reachable over the LAN, and that auth is enforced for a remote caller.
    """
    section("local checks (across the network)")
    results: list[tuple[str, bool, str]] = []

    def check(name: str, passed: bool, detail: str = "") -> None:
        results.append((name, passed, detail))
        print(f"  {'PASS' if passed else 'FAIL'}: {name}{' - ' + detail if detail else ''}")

    status, payload = http_json(f"{base}/health")
    check("health reachable over the network", status == 200, f"status={status}")

    status, _ = http_json(f"{base}/api/site-banner")
    check("public site-banner endpoint", status == 200, f"status={status}")

    status, _ = http_json(f"{base}/api/admin/settings")
    check("admin route rejects unauthenticated caller", status in (401, 403), f"status={status}")

    try:
        token = admin_token(cfg, base)
        check("admin login over the network", bool(token))
    except HarnessError as e:
        check("admin login over the network", False, str(e))
        token = ""

    if token:
        status, payload = http_json(f"{base}/api/admin/settings", token=token)
        check("authenticated admin settings", status == 200, f"status={status}")

        status, payload = http_json(f"{base}/api/admin/ssl/ip-check", token=token)
        check("certificate/LAN-address check responds", status == 200, f"status={status}")
        if status == 200 and isinstance(payload, dict) and payload.get("mismatch"):
            print(f"    note: instance reports a cert/IP mismatch "
                  f"(cert={payload.get('cert_ips')}, local={payload.get('local_ips')})")

        status, payload = http_json(f"{base}/api/diagnostics/audit", token=token)
        check("diagnostics audit", status == 200, f"status={status}")

        status, payload = http_json(f"{base}/api/admin/update/check", token=token)
        check("update check", status == 200, f"status={status}")

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n  {passed}/{len(results)} local checks passed")
    return passed == len(results)


# ----------------------------------------------------------------- hold -----

def cmd_hold(cfg: dict, state: RunState) -> None:
    base = instance_url(state, cfg)
    section("HOLD - instance is up and waiting")
    print(f"  URL:      {base}")
    print(f"  Admin:    {cfg.get('ADMIN_USER', 'admin')} / {cfg.get('ADMIN_PASSWORD', '')}")
    print(f"  Target:   {state.target} ({state.dest})")
    print(f"  Run dir:  {state.run_dir}")
    print(f"  Run id:   {state.run_id}")
    print(f"  Commit:   {state.commit or 'unknown'}")
    print()
    print("  Test from any machine on the network now. When finished, either")
    print("  press Enter here, or in a new session run:")
    print(f"    python testing/harness.py -env <config> -target {state.target} logs down")
    print()
    try:
        input("  Press Enter to continue... ")
    except EOFError:
        # Non-interactive invocation: holding forever would hang CI.
        say("stdin is closed; not holding")


# ----------------------------------------------------------------- logs -----

# Anything matching these gets flagged in the summary.
_PROBLEM = re.compile(r"\b(ERROR|CRITICAL|Traceback|Exception|FAILED)\b")
# Redacted before anything is written into the archive.
_SECRET = re.compile(
    r"(?i)(api[_-]?key\"?\s*[:=]\s*\"?|Bearer\s+|sk-|gitv_)([A-Za-z0-9_\-\.]{8,})"
)


def cmd_logs(cfg: dict, target: TargetSpec, tr: Transport, state: RunState) -> bool:
    section(f"LOGS  run={state.run_id}")
    archive_root = (REPO_ROOT / cfg.get("ARCHIVE_DIR", "./testing/artifacts").lstrip("./")).resolve()
    dest = archive_root / state.run_id / state.target
    dest.mkdir(parents=True, exist_ok=True)

    src = f"{state.run_dir}/GitInTheVan"
    wanted = [
        (f"{state.run_dir}/harness-logs", "harness-logs"),
        (f"{src}/data/logs", "app-logs"),
        (f"{src}/data/updater.log", "updater.log"),
        (f"{src}/scripts/installer.log", "installer.log"),
    ]
    if target.kind == "docker":
        project = state.compose_project or ""
        if project:
            proc = tr.run(f"docker compose -p {shlex.quote(project)} logs --tail 500 "
                          f"2>/dev/null || docker-compose -p {shlex.quote(project)} logs --tail 500",
                          check=False, timeout=300)
            (dest / "container.log").write_text(proc.stdout or "", encoding="utf-8")

    pulled = 0
    for remote, label in wanted:
        if tr.pull(remote, dest / label):
            pulled += 1
        else:
            say(f"not present (skipped): {remote}")

    scrubbed = redact_tree(dest)
    say(f"archived {pulled} log location(s) to {dest}")
    if scrubbed:
        say(f"redacted {scrubbed} credential-shaped value(s) from the archive")

    problems = scan_tree(dest)
    if problems:
        section("PROBLEMS FOUND IN LOGS")
        for path, line_no, text in problems[:60]:
            print(f"  {path}:{line_no}: {text[:180]}")
        if len(problems) > 60:
            print(f"  ... and {len(problems) - 60} more")
        say(f"{len(problems)} problem line(s); archive retained at {dest}")
        return False

    say("no ERROR/CRITICAL/Traceback lines found")
    return True


def redact_tree(root: Path) -> int:
    count = 0
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix in (".db", ".gz", ".zip"):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        redacted, n = _SECRET.subn(lambda m: m.group(1) + "<redacted>", text)
        if n:
            path.write_text(redacted, encoding="utf-8")
            count += n
    return count


def scan_tree(root: Path) -> list[tuple[str, int, str]]:
    hits: list[tuple[str, int, str]] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if _PROBLEM.search(line):
                hits.append((str(path.relative_to(root)), i, line.strip()))
    return hits


# ----------------------------------------------------------------- down -----

def assert_safe_to_delete(state: RunState, target: TargetSpec) -> None:
    """Refuse anything that is not unmistakably a run directory of ours.

    The Windows target's parent folder can sit beside real repositories, so
    this is the difference between a test harness and an accident.
    """
    run_dir = state.run_dir.strip()
    folder = target.folder.rstrip("/\\").strip()

    if not run_dir or run_dir in ("/", "~", "."):
        raise HarnessError(f"refusing to delete a root-like path: {run_dir!r}")
    if re.fullmatch(r"[A-Za-z]:[\\/]?", run_dir):
        raise HarnessError(f"refusing to delete a drive root: {run_dir!r}")
    if RUN_ROOT_NAME not in run_dir:
        raise HarnessError(
            f"refusing to delete {run_dir!r}: it does not contain {RUN_ROOT_NAME!r}, "
            f"so it was not created by this harness"
        )
    if run_dir.rstrip("/\\") == folder:
        raise HarnessError(f"refusing to delete the configured parent folder: {folder!r}")
    if state.run_id not in run_dir:
        raise HarnessError(
            f"refusing to delete {run_dir!r}: the run id {state.run_id!r} is not part of the path"
        )


def cmd_down(cfg: dict, target: TargetSpec, tr: Transport, state: RunState) -> None:
    section(f"DOWN  run={state.run_id}")
    assert_safe_to_delete(state, target)

    src = f"{state.run_dir}/GitInTheVan"

    if target.kind == "docker":
        project = state.compose_project
        if project:
            say(f"stopping compose project {project}")
            tr.run(f"cd {shlex.quote(src)} && "
                   f"(docker compose -f {shlex.quote(target.compose_file)} -p {shlex.quote(project)} down -v "
                   f"|| docker-compose -f {shlex.quote(target.compose_file)} -p {shlex.quote(project)} down -v)",
                   check=False, timeout=600)
    elif target.kind == "windows":
        say("stopping server and mock upstream")
        tr.run(
            f"foreach ($f in @('.server.pid','.deploy.pid','.mock.pid')) {{ "
            f"  $p = Join-Path '{state.run_dir}' $f; "
            f"  if (Test-Path $p) {{ "
            f"    $id = (Get-Content $p | Select-Object -First 1).Trim(); "
            f"    if ($id) {{ try {{ Stop-Process -Id $id -Force -ErrorAction Stop }} catch {{}} }} }} }}",
            check=False, timeout=120,
        )
    else:
        say("stopping server and mock upstream")
        tr.run(
            f"for f in .server.pid .deploy.pid .mock.pid; do "
            f"  p={shlex.quote(state.run_dir)}/$f; "
            f"  [ -f \"$p\" ] && kill $(cat \"$p\") 2>/dev/null; done; true",
            check=False, timeout=120,
        )

    # The marker is verified on the target, immediately before removal, so a
    # stale local run-state file cannot direct a delete at the wrong directory.
    say(f"verifying marker and removing {state.run_dir}")
    if target.kind == "windows" and target.is_local:
        cmd = (
            f"$d = '{state.run_dir}'; $m = Join-Path $d '{MARKER_NAME}'; "
            f"if (-not (Test-Path $m)) {{ Write-Error 'marker missing'; exit 3 }}; "
            f"if ((Get-Content $m -Raw).Trim() -ne '{state.run_id}') {{ Write-Error 'marker mismatch'; exit 4 }}; "
            f"Remove-Item -Recurse -Force $d; Write-Output 'REMOVED'"
        )
    else:
        d = shlex.quote(state.run_dir)
        m = shlex.quote(state.run_dir + "/" + MARKER_NAME)
        cmd = (
            f"[ -f {m} ] || {{ echo 'marker missing' >&2; exit 3; }}; "
            f"[ \"$(cat {m})\" = {shlex.quote(state.run_id)} ] || {{ echo 'marker mismatch' >&2; exit 4; }}; "
            f"rm -rf {d} && echo REMOVED"
        )

    proc = tr.run(cmd, check=False, timeout=300)
    if proc.returncode != 0 or "REMOVED" not in (proc.stdout or ""):
        raise HarnessError(
            f"teardown refused or failed (exit {proc.returncode}): "
            f"{(proc.stderr or proc.stdout or '').strip()}\n"
            f"The directory was left in place: {state.run_dir}"
        )

    say("run directory removed")
    state.notes.append("torn down")
    state.save()


# ------------------------------------------------------------------ cli -----

def main() -> int:
    parser = argparse.ArgumentParser(
        prog="harness.py",
        description="Provision, test, and tear down a throwaway GitInTheVan install.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Subcommands run in the order given:\n"
            "  up     clone at -branch, run the real deploy script, create admin, seed endpoint\n"
            "  test   flow_test.py on the target, then cross-network checks from here\n"
            "  hold   print credentials and wait, so you can test by hand\n"
            "  logs   pull and scan logs into the archive\n"
            "  down   stop the instance and delete the run directory\n"
            "  all    up test logs down\n"
        ),
    )
    parser.add_argument("commands", nargs="+",
                        choices=["up", "test", "hold", "logs", "down", "all"])
    parser.add_argument("-env", dest="env", default="testing/harness.env",
                        help="config file (default: testing/harness.env)")
    parser.add_argument("-target", dest="target", required=True,
                        choices=["macos", "linux", "docker", "windows"])
    parser.add_argument("-branch", dest="branch", default="",
                        help="branch to clone (default: BRANCH from config)")
    parser.add_argument("-run", dest="run_id", default="",
                        help="operate on a specific run id instead of the latest")
    parser.add_argument("-jump", dest="jump", default=None,
                        help="ProxyJump host for this run, overriding the config. "
                             "Use '-jump none' to connect directly.")
    parser.add_argument("-replicate", action="store_true",
                        help="copy endpoints from the local database instead of using "
                             "the mock upstream (WARNING: copies real API keys)")
    args = parser.parse_args()

    try:
        cfg = load_config(Path(args.env))
        args.branch = args.branch or cfg.get("BRANCH", "main")
        target = build_target(cfg, args.target, args.jump)
        tr = Transport(target, cfg.get("SSH_OPTS", ""))
        if target.jump and not target.is_local:
            say(f"connecting to {target.dest} via jump host {target.jump}")

        commands = ["up", "test", "logs", "down"] if "all" in args.commands else args.commands
        state: RunState | None = None
        ok = True

        for command in commands:
            if command == "up":
                state = cmd_up(cfg, target, tr, args)
                continue

            if state is None:
                state = RunState.load(args.run_id) if args.run_id else RunState.latest(args.target)

            if command == "test":
                ok &= cmd_test(cfg, target, tr, state)
            elif command == "hold":
                cmd_hold(cfg, state)
            elif command == "logs":
                ok &= cmd_logs(cfg, target, tr, state)
            elif command == "down":
                cmd_down(cfg, target, tr, state)

        section("HARNESS " + ("PASS" if ok else "FAIL"))
        return 0 if ok else 1

    except HarnessError as e:
        print(f"\n[harness] ERROR: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n[harness] interrupted; run state is preserved for `down`", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
