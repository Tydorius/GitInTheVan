"""Tests for the cross-platform test harness (testing/harness.py).

No network and no SSH: these cover config parsing, the teardown guard, log
redaction/scanning, and the line-ending integrity of the shipped scripts.

The teardown guard matters most. `down` deletes directories on machines that
hold real work, so every rejection path is asserted here rather than trusted.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HARNESS_PATH = ROOT / "testing" / "harness.py"


def _load_harness():
    """testing/ is not a package, so load by path."""
    spec = importlib.util.spec_from_file_location("gitv_harness", HARNESS_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["gitv_harness"] = module
    spec.loader.exec_module(module)
    return module


harness = _load_harness()


class TestConfigParsing:
    def test_parses_keys_comments_and_blank_lines(self, tmp_path):
        cfg_file = tmp_path / "h.env"
        cfg_file.write_text(
            "# a comment\n"
            "\n"
            "TARGET_LINUX=root@10.0.2.50\n"
            "LINUX_FOLDER=~/github   # trailing comment\n"
            "ADMIN_PASSWORD=testpassword1\n",
            encoding="utf-8",
        )
        cfg = harness.load_config(cfg_file)

        assert cfg["TARGET_LINUX"] == "root@10.0.2.50"
        assert cfg["LINUX_FOLDER"] == "~/github", "inline comment should be stripped"
        assert cfg["ADMIN_PASSWORD"] == "testpassword1"

    def test_windows_path_with_backslashes_survives(self, tmp_path):
        cfg_file = tmp_path / "h.env"
        cfg_file.write_text("WINDOWS_FOLDER=E:\\github\\_gitv-testing\n", encoding="utf-8")

        assert harness.load_config(cfg_file)["WINDOWS_FOLDER"] == "E:\\github\\_gitv-testing"

    def test_missing_file_names_the_example(self, tmp_path):
        with pytest.raises(harness.HarnessError, match="harness.env.example"):
            harness.load_config(tmp_path / "absent.env")

    def test_blank_target_is_reported_as_disabled(self, tmp_path):
        cfg_file = tmp_path / "h.env"
        cfg_file.write_text("TARGET_LINUX=\nLINUX_FOLDER=~/github\n", encoding="utf-8")
        cfg = harness.load_config(cfg_file)

        with pytest.raises(harness.HarnessError, match="disabled"):
            harness.build_target(cfg, "linux")

    def test_unknown_target_rejected(self):
        with pytest.raises(harness.HarnessError, match="unknown target"):
            harness.build_target({}, "solaris")

    def test_localhost_target_skips_ssh(self):
        cfg = {"TARGET_WINDOWS": "localhost", "WINDOWS_FOLDER": "E:\\github"}
        target = harness.build_target(cfg, "windows")

        assert target.is_local is True
        assert target.kind == "windows"

    def test_shipped_example_config_is_parseable_and_complete(self):
        cfg = harness.load_config(ROOT / "testing" / "harness.env.example")

        for key in ("REPO_URL", "BRANCH", "GITV_PORT", "ADMIN_USER",
                    "ADMIN_PASSWORD", "ARCHIVE_DIR"):
            assert key in cfg, f"{key} missing from harness.env.example"

    def test_example_admin_password_satisfies_the_apps_own_rules(self):
        """A password the app rejects would fail every run at admin creation."""
        from app.services.auth import validate_password_strength

        cfg = harness.load_config(ROOT / "testing" / "harness.env.example")

        assert validate_password_strength(cfg["ADMIN_PASSWORD"]) is None


class TestTeardownGuard:
    """`down` deletes directories. Every refusal path is asserted."""

    def _state(self, run_dir: str, run_id: str = "20260818-101500-1234"):
        return harness.RunState(
            run_id=run_id, target="windows", kind="windows", dest="localhost",
            run_dir=run_dir, branch="main", port=8100, mock_port=8199,
        )

    def _target(self, folder: str = "E:\\github\\_gitv-testing"):
        return harness.TargetSpec(
            name="windows", dest="localhost", folder=folder, kind="windows",
        )

    def test_accepts_a_well_formed_run_directory(self):
        state = self._state("E:\\github\\_gitv-testing\\_gitv-testruns\\20260818-101500-1234")
        harness.assert_safe_to_delete(state, self._target())

    @pytest.mark.parametrize("bad", ["/", "~", ".", ""])
    def test_rejects_root_like_paths(self, bad):
        with pytest.raises(harness.HarnessError, match="root-like"):
            harness.assert_safe_to_delete(self._state(bad), self._target())

    @pytest.mark.parametrize("bad", ["C:\\", "E:/", "D:"])
    def test_rejects_drive_roots(self, bad):
        with pytest.raises(harness.HarnessError, match="drive root"):
            harness.assert_safe_to_delete(self._state(bad), self._target())

    def test_rejects_a_path_outside_the_run_root(self):
        """The nightmare case: a real project directory."""
        state = self._state("E:\\github\\GitInTheVan-Public")
        with pytest.raises(harness.HarnessError, match="_gitv-testruns"):
            harness.assert_safe_to_delete(state, self._target("E:\\github"))

    def test_rejects_the_configured_parent_folder(self):
        folder = "E:\\github\\_gitv-testruns"
        state = self._state(folder)
        with pytest.raises(harness.HarnessError, match="parent folder"):
            harness.assert_safe_to_delete(state, self._target(folder))

    def test_rejects_when_run_id_is_not_in_the_path(self):
        """Guards against a stale state file aiming at another run's directory."""
        state = self._state("E:\\github\\_gitv-testing\\_gitv-testruns\\some-other-run")
        with pytest.raises(harness.HarnessError, match="run id"):
            harness.assert_safe_to_delete(state, self._target())


class TestLogHandling:
    def test_credentials_are_redacted_from_the_archive(self, tmp_path):
        log = tmp_path / "app.log"
        log.write_text(
            'endpoint created with api_key="sk-abcdef1234567890"\n'
            "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9abcdef\n"
            "user key gitv_Ab12Cd34Ef56Gh78\n"
            "this line is fine\n",
            encoding="utf-8",
        )

        count = harness.redact_tree(tmp_path)
        text = log.read_text(encoding="utf-8")

        assert count >= 3
        assert "abcdef1234567890" not in text
        assert "eyJhbGciOiJIUzI1NiJ9abcdef" not in text
        assert "Ab12Cd34Ef56Gh78" not in text
        assert "this line is fine" in text

    def test_scan_reports_problem_lines_only(self, tmp_path):
        (tmp_path / "a.log").write_text(
            "INFO all good\n"
            "ERROR something broke\n"
            "DEBUG noisy\n"
            "Traceback (most recent call last):\n",
            encoding="utf-8",
        )

        hits = harness.scan_tree(tmp_path)

        assert len(hits) == 2
        assert {h[1] for h in hits} == {2, 4}

    def test_clean_logs_produce_no_hits(self, tmp_path):
        (tmp_path / "a.log").write_text("INFO started\nINFO ready\n", encoding="utf-8")

        assert harness.scan_tree(tmp_path) == []


class TestRunState:
    def test_round_trips(self, tmp_path, monkeypatch):
        monkeypatch.setattr(harness, "RUNS_DIR", tmp_path)
        state = harness.RunState(
            run_id="r1", target="linux", kind="posix", dest="root@host",
            run_dir="/root/github/_gitv-testruns/r1", branch="main",
            port=8100, mock_port=8199, commit="abc123",
        )
        state.save()

        loaded = harness.RunState.load("r1")

        assert loaded.commit == "abc123"
        assert loaded.run_dir == state.run_dir

    def test_missing_state_is_an_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(harness, "RUNS_DIR", tmp_path)
        with pytest.raises(harness.HarnessError, match="no run state"):
            harness.RunState.load("nope")

    def test_latest_picks_the_matching_target(self, tmp_path, monkeypatch):
        monkeypatch.setattr(harness, "RUNS_DIR", tmp_path)
        for run_id, target in (("r1", "linux"), ("r2", "macos")):
            harness.RunState(
                run_id=run_id, target=target, kind="posix", dest="x",
                run_dir=f"/x/_gitv-testruns/{run_id}", branch="main",
                port=8100, mock_port=8199,
            ).save()

        assert harness.RunState.latest("macos").run_id == "r2"


class TestShippedScriptIntegrity:
    """A stray CR inside a .bat silently corrupted a path once already.

    `file` still reported CRLF and the bare-LF count was zero, so neither of
    the obvious checks caught it. This asserts the byte-level property.
    """

    def _script_files(self) -> list[Path]:
        return [
            *(ROOT / "scripts").glob("*.sh"),
            *(ROOT / "scripts").glob("*.bat"),
            *(ROOT / "testing").glob("*.bat"),
            *(ROOT / "testing" / "remote").glob("*.sh"),
        ]

    def test_no_stray_carriage_returns(self):
        offenders = []
        for path in self._script_files():
            text = path.read_bytes().decode("utf-8")
            for i, ch in enumerate(text):
                if ch == "\r" and (i + 1 >= len(text) or text[i + 1] != "\n"):
                    offenders.append(f"{path.name} at offset {i}")
        assert not offenders, f"stray CR (corrupted escape?) in: {offenders}"

    def test_batch_files_are_crlf_and_shell_scripts_are_lf(self):
        for path in self._script_files():
            data = path.read_bytes()
            if path.suffix == ".bat":
                assert b"\r\n" in data, (
                    f"{path.name} must use CRLF -- cmd.exe misparses LF-only .bat "
                    f"files under non-interactive invocation"
                )
            else:
                assert b"\r\n" not in data, f"{path.name} must use LF endings"

    def test_remote_scripts_are_syntactically_valid(self):
        """bash -n on the provisioning scripts, mirroring the deploy-script checks."""
        import shutil
        import subprocess

        bash = shutil.which("bash")
        if not bash:
            pytest.skip("bash unavailable")

        for path in (ROOT / "testing" / "remote").glob("*.sh"):
            proc = subprocess.run([bash, "-n", str(path)], capture_output=True, text=True)
            assert proc.returncode == 0, f"{path.name}: {proc.stderr}"

    def test_harness_and_remote_python_compile(self):
        import py_compile

        targets = [HARNESS_PATH, *(ROOT / "testing" / "remote").glob("*.py")]
        assert len(targets) >= 3, "expected harness plus the remote helpers"
        for path in targets:
            py_compile.compile(str(path), doraise=True)


class TestJumpHostResolution:
    """A single global jump host is wrong on a mixed network.

    Routing a directly reachable machine through a bastion is pointless at
    best and broken at worst, so the jump is resolved per target.
    """

    def test_target_specific_key_wins_over_the_global_default(self):
        cfg = {"SSH_JUMP": "root@bastion", "LINUX_SSH_JUMP": "root@other"}

        assert harness.resolve_jump(cfg, "linux") == "root@other"

    def test_global_default_applies_when_no_specific_key_exists(self):
        cfg = {"SSH_JUMP": "root@bastion"}

        assert harness.resolve_jump(cfg, "docker") == "root@bastion"

    def test_present_but_empty_key_opts_a_target_out(self):
        """The macOS case: reachable directly while others need the bastion."""
        cfg = {"SSH_JUMP": "root@bastion", "MACOS_SSH_JUMP": ""}

        assert harness.resolve_jump(cfg, "macos") == ""

    def test_no_configuration_means_no_jump(self):
        assert harness.resolve_jump({}, "linux") == ""

    def test_cli_override_beats_configuration(self):
        cfg = {"SSH_JUMP": "root@bastion", "LINUX_SSH_JUMP": "root@other"}

        assert harness.resolve_jump(cfg, "linux", "root@cli") == "root@cli"

    @pytest.mark.parametrize("value", ["none", "NONE", " none ", ""])
    def test_cli_none_disables_the_jump(self, value):
        cfg = {"SSH_JUMP": "root@bastion"}

        assert harness.resolve_jump(cfg, "linux", value) == ""

    def test_build_target_attaches_the_resolved_jump(self):
        cfg = {
            "TARGET_LINUX": "linuxuser@dock-21", "LINUX_FOLDER": "~/github",
            "SSH_JUMP": "root@bastion",
        }

        assert harness.build_target(cfg, "linux").jump == "root@bastion"

    def test_transport_passes_the_jump_to_ssh_and_scp(self):
        target = harness.TargetSpec(
            name="linux", dest="linuxuser@dock-21", folder="~/github",
            kind="posix", jump="root@bastion",
        )

        transport = harness.Transport(target, "-o BatchMode=yes")

        assert transport.jump == "root@bastion"
        assert "-J" in transport._ssh_base()
        assert "root@bastion" in transport._ssh_base()

    def test_no_jump_means_no_proxyjump_flag(self):
        target = harness.TargetSpec(
            name="macos", dest="tydorius@host", folder="~/github", kind="posix",
        )

        assert "-J" not in harness.Transport(target, "").ssh_opts
        assert "-J" not in harness.Transport(target, "")._ssh_base()


class TestShellScriptErrorHandling:
    r"""Guards against the `set -e` + `$?` trap.

    `deploy-linux.sh` and `deploy-macos.sh` both ran a bare command and then
    inspected `$?` on the following line. Under `set -e` that line is
    unreachable: a bare command exiting non-zero terminates the shell at once.
    For the port check, non-zero meant "the port is free" -- the normal case on
    a clean machine -- so the script exited silently right after announcing
    that the server was starting, and `app.main` never ran. Neither script had
    ever been executed, so nothing caught it until the harness did.
    """

    def _set_e_scripts(self) -> list[Path]:
        return [
            p for p in (ROOT / "scripts").glob("*.sh")
            if p.read_text(encoding="utf-8").lstrip().startswith("#!/bin/bash\nset -e")
            or "\nset -e\n" in p.read_text(encoding="utf-8")
        ]

    def test_there_are_set_e_scripts_to_check(self):
        assert self._set_e_scripts(), "fixture check: expected set -e shell scripts"

    def test_no_exit_status_tested_after_a_bare_command(self):
        offenders = []
        for path in self._set_e_scripts():
            for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if "[ $? -eq" in line or "[ $? -ne" in line:
                    offenders.append(f"{path.name}:{i}")
        assert not offenders, (
            "Under `set -e` the script dies before $? can be read. Use "
            "`if cmd; then` instead, or capture the status with `cmd || true` "
            "followed immediately by STATUS=$?. Offenders: " + ", ".join(offenders)
        )

    def test_deploy_scripts_start_the_server_on_the_configured_port(self):
        """The port check and the banner both hardcoded 8000.

        The app binds GITV_PORT from .env, so on any non-default install the
        script probed the wrong port and printed a URL that did not work.
        """
        for name in ("deploy-linux.sh", "deploy-macos.sh"):
            text = (ROOT / "scripts" / name).read_text(encoding="utf-8")

            assert 'GITV_PORT=$(grep -E "^GITV_PORT="' in text, (
                f"{name} does not read GITV_PORT from .env"
            )
            assert "connect_ex(('127.0.0.1',8000))" not in text, (
                f"{name} still probes a hardcoded port"
            )
            assert "localhost:8000" not in text, (
                f"{name} still advertises a hardcoded port in its banner"
            )
            assert text.rstrip().endswith("-m app.main"), (
                f"{name} must end by starting the server"
            )


class TestDocumentedCommandIntegrity:
    """A backslash-t escape expanded while editing eats the character after it.

    The harness example pointed at a testing directory that rendered as
    <TAB>esting in both CHANGELOG.md and README.md. The tab is invisible in a
    rendered diff and the path is simply wrong for anyone who copies the
    command. Same failure mode as the stray CR above, in prose not in a script.

    The file list is explicit rather than a glob: a glob also picks up
    untracked local Markdown, which makes the result depend on the developer
    machine and can name a local file in the assertion message.
    """

    DOCS = (
        "README.md",
        "CHANGELOG.md",
        "docs/user-guide.md",
        "docs/examples/map/README.md",
        "testing/README.md",
        "frontend/README.md",
    )

    def test_documented_files_all_exist(self):
        missing = [rel for rel in self.DOCS if not (ROOT / rel).exists()]
        assert not missing, f"listed doc file is gone; update DOCS: {missing}"

    def test_no_literal_tabs_in_documentation(self):
        offenders = []
        for rel in self.DOCS:
            text = (ROOT / rel).read_text(encoding="utf-8")
            for i, line in enumerate(text.splitlines(), 1):
                if chr(9) in line:
                    offenders.append(f"{rel}:{i}")
        assert not offenders, f"literal tab (expanded escape?) in: {offenders}"
