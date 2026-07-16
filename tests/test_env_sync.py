from app.services.env_sync import parse_env_keys, set_env_value, sync_env


def test_parse_env_keys_skips_comments_and_blanks(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "# a comment\n\nGITV_HOST=0.0.0.0\nGITV_PORT=8000\n",
        encoding="utf-8",
    )
    keys = parse_env_keys(env)
    assert keys == {"GITV_HOST": "0.0.0.0", "GITV_PORT": "8000"}


def test_parse_env_keys_missing_file(tmp_path):
    assert parse_env_keys(tmp_path / "nonexistent.env") == {}


def test_sync_env_adds_only_missing_keys(tmp_path):
    env = tmp_path / ".env"
    template = tmp_path / ".env.example"
    env.write_text("GITV_HOST=localhost\n", encoding="utf-8")
    template.write_text("GITV_HOST=localhost\nGITV_PORT=8000\n", encoding="utf-8")

    added = sync_env(env, template)
    assert added == ["GITV_PORT"]
    keys = parse_env_keys(env)
    assert keys["GITV_HOST"] == "localhost"
    assert keys["GITV_PORT"] == "8000"


def test_sync_env_nothing_to_add(tmp_path):
    env = tmp_path / ".env"
    template = tmp_path / ".env.example"
    env.write_text("GITV_HOST=localhost\nGITV_PORT=8000\n", encoding="utf-8")
    template.write_text("GITV_HOST=localhost\nGITV_PORT=8000\n", encoding="utf-8")
    assert sync_env(env, template) == []


def test_set_env_value_adds_new_key(tmp_path):
    env = tmp_path / ".env"
    env.write_text("GITV_HOST=0.0.0.0\nGITV_PORT=8000\n", encoding="utf-8")

    set_env_value(env, "GITV_DENO_PATH", "/opt/deno")

    keys = parse_env_keys(env)
    assert keys["GITV_DENO_PATH"] == "/opt/deno"
    assert keys["GITV_HOST"] == "0.0.0.0"
    assert len(keys) == 3


def test_set_env_value_updates_existing_key(tmp_path):
    env = tmp_path / ".env"
    env.write_text("GITV_DENO_PATH=/old/deno\nGITV_PORT=8000\n", encoding="utf-8")

    set_env_value(env, "GITV_DENO_PATH", "/new/deno")

    keys = parse_env_keys(env)
    assert keys["GITV_DENO_PATH"] == "/new/deno"
    # No duplicate line introduced.
    assert len(keys) == 2


def test_set_env_value_creates_file_if_missing(tmp_path):
    env = tmp_path / ".env"
    assert not env.exists()

    set_env_value(env, "GITV_DENO_PATH", "/opt/deno")

    keys = parse_env_keys(env)
    assert keys == {"GITV_DENO_PATH": "/opt/deno"}


def test_set_env_value_preserves_comments(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# Deno binary path\nGITV_DENO_PATH=\nGITV_PORT=8000\n", encoding="utf-8")

    set_env_value(env, "GITV_DENO_PATH", "/opt/deno")

    content = env.read_text(encoding="utf-8")
    assert "# Deno binary path" in content
    assert "GITV_DENO_PATH=/opt/deno" in content
    assert "GITV_PORT=8000" in content
