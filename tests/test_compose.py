"""Guards on the shipped docker-compose files.

Both properties asserted here were broken and found by running the real
container, not by reading the file: the published port was hardcoded, and the
signing key defaulted to the placeholder that is public in this repository.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILES = sorted(ROOT.glob("docker-compose*.yml"))


def test_compose_files_exist():
    assert COMPOSE_FILES, "expected docker-compose*.yml at the repo root"


@pytest.mark.parametrize("path", COMPOSE_FILES, ids=lambda p: p.name)
class TestComposeConfiguration:
    def test_published_port_is_templated(self, path: Path):
        """A hardcoded host port cannot be run on a host already using 8000.

        The deploy scripts gained a port argument in 0.19.0 for exactly this
        reason; compose kept the same limitation until it was found by the
        cross-platform harness, which runs on 8100 to avoid colliding with a
        real instance.
        """
        text = path.read_text(encoding="utf-8")

        assert '"8000:8000"' not in text, (
            f"{path.name} hardcodes the host port; use ${{GITV_PORT:-8000}}:8000"
        )
        assert re.search(r'"\$\{GITV_PORT:-8000\}:8000"', text), (
            f"{path.name} does not template the published port"
        )

    def test_secret_key_does_not_default_to_the_public_placeholder(self, path: Path):
        """Defaulting to it handed every container a signing key from the repo.

        An empty value lets the app generate and persist a real key into
        data/, which every one of these files already mounts as a volume.
        """
        text = path.read_text(encoding="utf-8")

        assert "change-me-in-production" not in text, (
            f"{path.name} defaults GITV_SECRET_KEY to the public placeholder"
        )

    def test_data_volume_is_mounted(self, path: Path):
        """The generated signing key and the database both live in data/.

        Without this mount a container restart silently signs everyone out and
        loses the database.
        """
        text = path.read_text(encoding="utf-8")

        assert "./data:/app/data" in text, f"{path.name} does not persist ./data"
