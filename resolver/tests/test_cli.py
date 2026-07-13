"""Unit tests for the `resolver` CLI helpers and offline subcommands.

Network paths (bot create/list) are exercised with a stubbed `requests`; the
.env-file plumbing (roster, identity parsing, env writing) is tested against a
temp resolver dir so it never touches the real .env files.
"""
from types import SimpleNamespace

import pytest

import cli


@pytest.fixture
def fake_resolver_dir(tmp_path, monkeypatch):
    """Point the CLI at a temp resolver dir with a minimal .env.example."""
    (tmp_path / ".env.example").write_text(
        "STINGRAY_URL=http://localhost:8000\n"
        "STINGRAY_API_KEY=sk_replace_me\n"
        "RESOLVER_BOT_USER_ID=2\n"
        "PROJECTS_ROOT=/home/me/projects\n"
        "RESOLVER_AGENT=claude\n"
    )
    monkeypatch.setattr(cli, "HERE", tmp_path)
    monkeypatch.setattr(cli, "ENV_EXAMPLE", tmp_path / ".env.example")
    return tmp_path


def test_read_env_file_ignores_comments_and_blanks(tmp_path):
    p = tmp_path / ".env.x"
    p.write_text("# a comment\n\nFOO=bar\nQUOTED=\"baz\"\nNOEQ\n")
    env = cli._read_env_file(p)
    assert env == {"FOO": "bar", "QUOTED": "baz"}


def test_identity_name():
    assert cli._identity_name(cli.HERE / ".env") == "default"
    assert cli._identity_name(cli.HERE / ".env.open") == "open"


def test_write_identity_substitutes_values(fake_resolver_dir):
    dest = cli._write_identity(
        "open", url="https://tix.example.com/api", api_key="sk_live_abc",
        user_id=3, desc="cheap fixes", projects_root="/srv/code", force=False,
    )
    env = cli._read_env_file(dest)
    assert env["STINGRAY_URL"] == "https://tix.example.com/api"
    assert env["STINGRAY_API_KEY"] == "sk_live_abc"
    assert env["RESOLVER_BOT_USER_ID"] == "3"
    assert env["PROJECTS_ROOT"] == "/srv/code"
    assert env[cli.DESC_KEY] == "cheap fixes"
    # Untouched template keys survive.
    assert env["RESOLVER_AGENT"] == "claude"


def test_write_identity_refuses_overwrite(fake_resolver_dir):
    (fake_resolver_dir / ".env.open").write_text("STINGRAY_URL=x\n")
    with pytest.raises(SystemExit):
        cli._write_identity("open", url="u", api_key="k", user_id=1,
                            desc="", projects_root="", force=False)
    # With --force it overwrites.
    dest = cli._write_identity("open", url="u2", api_key="k", user_id=1,
                               desc="", projects_root="", force=True)
    assert cli._read_env_file(dest)["STINGRAY_URL"] == "u2"


def test_roster_builds_worker_string(fake_resolver_dir, capsys):
    (fake_resolver_dir / ".env.claude").write_text(
        "RESOLVER_BOT_USER_ID=2\nRESOLVER_BOT_DESC=heavy refactors\n")
    (fake_resolver_dir / ".env.open").write_text(
        "RESOLVER_BOT_USER_ID=3\nRESOLVER_BOT_DESC=cheap fixes\n")
    cli.cmd_roster(SimpleNamespace())
    out = capsys.readouterr().out
    assert "RESOLVER_WORKERS=" in out
    assert "2:claude:heavy refactors" in out
    assert "3:open:cheap fixes" in out


def test_roster_skips_identities_without_id(fake_resolver_dir, capsys):
    (fake_resolver_dir / ".env").write_text("STINGRAY_URL=x\n")  # no bot id
    cli.cmd_roster(SimpleNamespace())
    assert "no identities" in capsys.readouterr().out


def test_bot_create_writes_env(fake_resolver_dir, monkeypatch, capsys):
    def fake_post(url, json, headers, timeout):
        assert url.endswith("/users/resolver-bot")
        assert json["username"] == "open-bot"
        return SimpleNamespace(
            status_code=201,
            json=lambda: {"user_id": 7, "username": "open-bot", "api_key": "sk_new"},
        )
    monkeypatch.setattr(cli.requests, "post", fake_post)
    args = SimpleNamespace(
        username="open-bot", name="open", display_name=None, email=None,
        desc="cheap fixes", projects_root=None, url="http://localhost:8000",
        admin_key="sk_admin", no_env_file=False, force=False,
    )
    assert cli.cmd_bot_create(args) == 0
    env = cli._read_env_file(fake_resolver_dir / ".env.open")
    assert env["STINGRAY_API_KEY"] == "sk_new"
    assert env["RESOLVER_BOT_USER_ID"] == "7"


def test_bot_create_errors_without_admin_key(fake_resolver_dir):
    args = SimpleNamespace(
        username="x", name=None, display_name=None, email=None, desc=None,
        projects_root=None, url="http://localhost:8000", admin_key=None,
        no_env_file=False, force=False,
    )
    with pytest.raises(SystemExit):
        cli.cmd_bot_create(args)
