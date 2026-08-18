"""Supply-chain guards for the dependency pinning policy (every dependency exactly pinned).

The rule already existed and was violated anyway: frontend/package.json shipped
caret ranges, and every deploy/update script ran `npm install`, which re-resolves
those ranges against the live registry and rewrites the lockfile. These tests
make the rule enforced rather than trusted.
"""

import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Exact semver, optionally with a prerelease/build suffix. No ^, ~, >=, *, x or ranges.
_EXACT_NPM_VERSION = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")


class TestPythonPinning:
    def test_all_requirements_use_exact_pins(self):
        data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        project = data["project"]

        groups = {"dependencies": project.get("dependencies", [])}
        for extra, reqs in project.get("optional-dependencies", {}).items():
            groups[f"optional-dependencies.{extra}"] = reqs

        unpinned = [
            f"{group}: {req}"
            for group, reqs in groups.items()
            for req in reqs
            if "==" not in req
        ]
        assert not unpinned, f"Requirements without an exact pin: {unpinned}"


class TestFrontendPinning:
    @staticmethod
    def _package_json() -> dict:
        return json.loads((ROOT / "frontend/package.json").read_text(encoding="utf-8"))

    def test_all_deps_use_exact_pins(self):
        pkg = self._package_json()
        unpinned = [
            f"{section}: {name}@{spec}"
            for section in ("dependencies", "devDependencies", "overrides")
            for name, spec in pkg.get(section, {}).items()
            if not _EXACT_NPM_VERSION.fullmatch(spec)
        ]
        assert not unpinned, f"Frontend deps without an exact pin: {unpinned}"

    def test_lockfile_root_matches_package_json(self):
        """`npm ci` hard-fails when these disagree, which would abort an update."""
        pkg = self._package_json()
        lock = json.loads((ROOT / "frontend/package-lock.json").read_text(encoding="utf-8"))
        root = lock["packages"][""]

        for section in ("dependencies", "devDependencies"):
            assert pkg.get(section, {}) == root.get(section, {}), (
                f"package.json {section} does not match package-lock.json; "
                "run `npm install --package-lock-only` in frontend/"
            )


class TestInstallCommandsRespectPins:
    """Pinning the manifest is pointless if the install command ignores it."""

    @staticmethod
    def _strip_comments(text: str) -> str:
        """Drop `#` (sh) and `REM`/`::` (batch) lines.

        These tests match on command text, so a comment that merely *mentions*
        `npm install` (explaining why it is not used) must not fail them.
        """
        kept = [
            line
            for line in text.splitlines()
            if not re.match(r"\s*(#|REM\b|::)", line, re.IGNORECASE)
        ]
        return "\n".join(kept)

    @classmethod
    def _script_text(cls) -> dict[str, str]:
        return {
            path.name: cls._strip_comments(path.read_text(encoding="utf-8", errors="ignore"))
            for path in (ROOT / "scripts").glob("*")
            if path.suffix in (".sh", ".bat")
        }

    def test_no_script_uses_npm_install(self):
        offenders = [
            name
            for name, text in self._script_text().items()
            if re.search(r"npm(-cli\.js)?[\"'!]*\s+install\b|NPM_C(MD|LI)[\"'!]*\s+install\b", text)
        ]
        assert not offenders, (
            f"Scripts using `npm install` instead of `npm ci`: {offenders}. "
            "`npm install` re-resolves ranges against the registry and rewrites the lockfile."
        )

    def test_no_script_upgrades_pip_unpinned(self):
        offenders = [
            name
            for name, text in self._script_text().items()
            if "--upgrade pip" in text
        ]
        assert not offenders, f"Scripts running an unpinned pip upgrade: {offenders}"

    def test_dockerfile_uses_npm_ci(self):
        text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        assert "npm ci" in text
        assert not re.search(r"npm\s+install\b", text)

    def test_deno_is_version_pinned_never_latest(self):
        """A `releases/latest` Deno fetch would silently change the sandbox runtime."""
        targets = [ROOT / "Dockerfile", *(ROOT / "scripts").glob("deploy-*")]
        for path in targets:
            text = path.read_text(encoding="utf-8", errors="ignore")
            if "deno" not in text.lower():
                continue
            assert "releases/latest" not in text, f"{path.name} fetches Deno from releases/latest"
            assert re.search(r"DENO_VERSION\s*=\s*\"?v\d+\.\d+\.\d+", text), (
                f"{path.name} does not pin DENO_VERSION to an exact version"
            )


class TestHashPinnedLockfiles:
    """Direct pins in pyproject do not constrain the transitive tree.

    `pip install -e .` re-resolved everything below the direct dependencies
    against PyPI on every deploy and every update, on every user's machine --
    which is the exposure a compromised transitive package relies on. The
    lockfiles close that; these tests keep them honest.
    """

    LOCKS = ("requirements/dev.txt", "requirements/docker.txt")

    # `name==version` at the start of a line, ignoring markers and continuations.
    _PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;]+)", re.MULTILINE)

    def _lock_text(self, rel: str) -> str:
        return (ROOT / rel).read_text(encoding="utf-8")

    def _pins(self, rel: str) -> dict[str, str]:
        return {m.group(1).lower().replace("_", "-"): m.group(2)
                for m in self._PIN.finditer(self._lock_text(rel))}

    def test_lockfiles_exist_and_are_populated(self):
        for rel in self.LOCKS:
            path = ROOT / rel
            assert path.exists(), f"{rel} is missing; regenerate with uv pip compile"
            assert len(self._pins(rel)) > 40, f"{rel} looks truncated"

    def test_every_locked_package_carries_a_hash(self):
        """--require-hashes fails closed, but only if the hashes are actually there."""
        for rel in self.LOCKS:
            text = self._lock_text(rel)
            blocks = re.split(r"\n(?=[A-Za-z0-9])", text)
            missing = [
                b.split("\n")[0].strip()
                for b in blocks
                if self._PIN.match(b) and "--hash=sha256:" not in b
            ]
            assert not missing, f"{rel}: entries without a sha256 hash: {missing}"

    def test_locks_agree_with_pyproject_direct_pins(self):
        """A stale lock would silently reinstate a version we deliberately patched."""
        data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        direct = {}
        for spec in data["project"].get("dependencies", []):
            m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]+\])?==(.+)$", spec.strip())
            if m:
                direct[m.group(1).lower().replace("_", "-")] = m.group(2)

        for rel in self.LOCKS:
            pins = self._pins(rel)
            for name, version in direct.items():
                assert name in pins, f"{rel} is missing {name}; regenerate the lock"
                assert pins[name] == version, (
                    f"{rel} pins {name}=={pins[name]} but pyproject pins {version}. "
                    "Regenerate the lockfiles after changing pyproject."
                )

    def test_scripts_install_with_require_hashes(self):
        for name, text in TestInstallCommandsRespectPins._script_text().items():
            if "requirements" not in text:
                continue
            assert "--require-hashes" in text, (
                f"{name} installs from a requirements file without --require-hashes"
            )

    def test_no_script_installs_the_dependency_tree_unverified(self):
        """`pip install -e .[dev]` would re-resolve the whole tree, unhashed."""
        offenders = [
            name
            for name, text in TestInstallCommandsRespectPins._script_text().items()
            if re.search(r"install\s+-e\s+\S*\[", text)
        ]
        assert not offenders, (
            f"Scripts installing the app with extras (re-resolves transitives): {offenders}. "
            "Install deps from the lockfile, then `-e . --no-deps`."
        )

    def test_editable_install_uses_no_deps(self):
        for name, text in TestInstallCommandsRespectPins._script_text().items():
            for line in text.splitlines():
                if re.search(r"install\s+-e\s", line):
                    assert "--no-deps" in line, (
                        f"{name}: editable install without --no-deps re-resolves "
                        f"the verified tree -- {line.strip()}"
                    )

    def test_bootstrap_pip_matches_the_locked_pip(self):
        """pip cannot replace its own running executable on Windows.

        pip-audit pulls pip into the dev lock, so if the bootstrap pin and the
        locked pin disagree, the lockfile install tries to upgrade pip and every
        deploy/update script dies with 'To modify pip, please run...'.
        """
        locked = self._pins("requirements/dev.txt").get("pip")
        assert locked, "pip is expected in the dev lock (via pip-audit)"

        for name, text in TestInstallCommandsRespectPins._script_text().items():
            for found in re.findall(r'pip install "pip==([^"]+)"', text):
                assert found == locked, (
                    f"{name} bootstraps pip=={found} but the lock pins pip=={locked}; "
                    "pip would try to modify itself and the install would fail on Windows."
                )

    def test_scripts_invoke_pip_as_a_module(self):
        """`python -m pip` survives a pip self-upgrade; `pip.exe` does not."""
        for name, text in TestInstallCommandsRespectPins._script_text().items():
            for line in text.splitlines():
                if "--require-hashes" not in line and not re.search(r"install\s+-e\s", line):
                    continue
                if re.search(r'[/\\"](pip)"?\s+install', line):
                    raise AssertionError(
                        f"{name}: calls the pip executable directly; use "
                        f"`python -m pip` -- {line.strip()}"
                    )

    def test_dockerfile_installs_from_the_lock(self):
        text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        assert "--require-hashes" in text and "requirements/docker.txt" in text
        assert "COPY requirements/" in text, "the lock must be COPYed before install"
        assert not re.search(r'install[^\n]*-e\s+"?\.\[', text), (
            "Dockerfile installs extras editably, re-resolving the tree unhashed"
        )
