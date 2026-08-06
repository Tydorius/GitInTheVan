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
