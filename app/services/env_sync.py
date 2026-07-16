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


def set_env_value(env_path: Path, key: str, value: str) -> None:
    """Set a single KEY=value in .env, updating an existing line or appending.

    Used by the deploy/update scripts to record runtime-resolved paths (e.g.
    GITV_DENO_PATH) so the application reads them via Settings on next start.
    """
    pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
    if not env_path.exists():
        env_path.write_text(f"{key}={value}\n", encoding="utf-8")
        return
    content = env_path.read_text(encoding="utf-8")
    if pattern.search(content):
        env_path.write_text(pattern.sub(f"{key}={value}", content), encoding="utf-8")
    else:
        sep = "" if content.endswith("\n") else "\n"
        env_path.write_text(content + f"{sep}{key}={value}\n", encoding="utf-8")


if __name__ == "__main__":
    root = Path(__file__).resolve().parent.parent.parent
    env_path = root / ".env"
    template_path = root / ".env.example"

    # --set KEY=VALUE: write a single runtime-resolved value into .env.
    # Used by deploy/update scripts to record paths like GITV_DENO_PATH.
    if len(sys.argv) == 3 and sys.argv[1] == "--set":
        key, _, value = sys.argv[2].partition("=")
        if not key:
            print("Invalid --set argument; use KEY=VALUE")
            sys.exit(1)
        if not env_path.exists() and template_path.exists():
            env_path.write_text(template_path.read_text(encoding="utf-8"), encoding="utf-8")
        set_env_value(env_path, key.strip(), value.strip())
        print(f"Set {key.strip()} in .env")
        sys.exit(0)

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
