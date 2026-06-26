"""Ensure .env has all keys from .env.example, filling defaults for missing ones.

Preserves existing values and comments. Only adds missing keys.
"""
import re
import sys
from pathlib import Path


def parse_env_keys(path: Path) -> dict[str, str]:
    """Extract GITV_ keys and their values from an env file."""
    keys = {}
    if not path.exists():
        return keys
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = re.match(r"^([A-Z_]+)=(.*)$", stripped)
        if match:
            keys[match.group(1)] = match.group(2)
    return keys


def sync_env(env_path: Path, template_path: Path) -> list[str]:
    """Add missing keys from template into env. Returns list of added keys."""
    existing = parse_env_keys(env_path)
    template = parse_env_keys(template_path)

    missing = {k: v for k, v in template.items() if k not in existing}
    if not missing:
        return []

    lines = []
    if env_path.exists():
        content = env_path.read_text(encoding="utf-8")
        lines = content.splitlines()
        if lines and lines[-1].strip() and not lines[-1].strip().startswith("#"):
            lines.append("")

    if missing:
        lines.append("")
        lines.append("# Added by env sync")
        for key in sorted(missing):
            lines.append(f"{key}={missing[key]}")

    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return sorted(missing.keys())


if __name__ == "__main__":
    root = Path(__file__).resolve().parent.parent.parent
    env_path = root / ".env"
    template_path = root / ".env.example"

    if not env_path.exists():
        if template_path.exists():
            env_path.write_text(template_path.read_text(encoding="utf-8"), encoding="utf-8")
            print("Created .env from .env.example")
        else:
            print("No .env or .env.example found")
            sys.exit(0)
    else:
        added = sync_env(env_path, template_path)
        if added:
            print(f"Added missing keys to .env: {', '.join(added)}")
        else:
            print(".env is up to date")
