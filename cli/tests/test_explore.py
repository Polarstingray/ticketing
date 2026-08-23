"""``stingray explore``: file enumeration, prompt shape, parsing, and the command."""
from __future__ import annotations

import json

import pytest

from stingray_cli import cmd_explore, explore
from stingray_cli.agent import AgentError
from stingray_cli.main import build_parser


def _args(*argv):
    return build_parser().parse_args(["explore", *argv])


# --- file enumeration --------------------------------------------------------

def test_lists_tracked_files_and_drops_generated_ones(git_repo, commit):
    commit("init", {
        "app/main.py": "print(1)\n",
        "app/ui.tsx": "export const x = 1;\n",
        "package-lock.json": "{}\n",
        "assets/logo.png": "notreallyapng\n",
    })
    files = explore.list_repo_files(git_repo)
    assert files == ["app/main.py", "app/ui.tsx"]


# --- prompt ------------------------------------------------------------------

def test_prompt_states_the_contract_and_lists_files(git_repo, commit):
    commit("init", {"app/main.py": "print(1)\n"})
    prompt = explore.build_discovery_prompt(git_repo, ["app/main.py"], None, False)
    assert "app/main.py" in prompt
    assert "fenced ```json block" in prompt
    assert "3 to 10 features" in prompt
    assert "TEACH MODE" not in prompt


def test_teach_mode_changes_the_instructions(git_repo):
    prompt = explore.build_discovery_prompt(git_repo, ["a.py"], None, True)
    assert "TEACH MODE" in prompt
    assert "student" in prompt


def test_feature_filter_scopes_the_prompt(git_repo):
    prompt = explore.build_discovery_prompt(git_repo, ["a.py"], "auth", False)
    assert "ONLY the feature called 'auth'" in prompt
    assert "3 to 10 features" not in prompt


def test_prompt_caps_the_file_list(git_repo):
    files = [f"src/mod{i}.py" for i in range(explore.MAX_FILES * 3)]
    prompt = explore.build_discovery_prompt(git_repo, files, None, False)
    assert f"and {len(files) - explore.MAX_FILES} more" in prompt
    # Only the capped slice is listed, but the true total is still stated so the
    # model knows it is seeing a sample.
    assert prompt.count("  src/mod") == explore.MAX_FILES
    assert f"{explore.MAX_FILES} shown of {len(files)}" in prompt


# --- parsing -----------------------------------------------------------------

GOOD = ('[{"name": "auth", "title": "Session auth", "description": "D", '
        '"priority": "high", "files": ["backend/auth.py"]}]')


def test_parses_bare_json_list():
    features = explore.parse_feature_tickets(GOOD)
    assert len(features) == 1
    assert features[0]["name"] == "auth"
    assert features[0]["priority"] == "high"


def test_parses_fenced_json_and_prefers_the_last_fence():
    text = (f'```json\n[{{"name": "draft", "title": "T", "files": ["a.py"]}}]\n```\n'
            f'on reflection:\n```json\n{GOOD}\n```')
    assert explore.parse_feature_tickets(text)[0]["name"] == "auth"


def test_falls_back_to_outermost_brackets():
    text = f"Sure! {GOOD} — hope that helps."
    assert explore.parse_feature_tickets(text)[0]["name"] == "auth"


def test_no_json_returns_empty():
    assert explore.parse_feature_tickets("I could not tell what this repo does") == []


def test_entries_without_name_title_or_files_are_dropped():
    text = ('[{"title": "T", "files": ["a.py"]},'
            ' {"name": "n", "files": ["a.py"]},'
            ' {"name": "n2", "title": "T", "files": []},'
            ' {"name": "ok", "title": "T", "files": ["a.py"]}]')
    assert [f["name"] for f in explore.parse_feature_tickets(text)] == ["ok"]


def test_duplicate_names_are_deduped():
    text = ('[{"name": "a", "title": "one", "files": ["a.py"]},'
            ' {"name": "a", "title": "two", "files": ["b.py"]}]')
    features = explore.parse_feature_tickets(text)
    assert [f["title"] for f in features] == ["one"]


def test_unknown_priority_is_dropped():
    text = '[{"name": "a", "title": "T", "priority": "urgent", "files": ["a.py"]}]'
    assert explore.parse_feature_tickets(text)[0]["priority"] == ""


# --- code blocks -------------------------------------------------------------

def test_blocks_come_from_the_commit_not_the_worktree(git_repo, commit):
    sha = commit("init", {"a.py": "committed\n"})
    (git_repo / "a.py").write_text("dirty\n", encoding="utf-8")

    blocks = explore.build_code_blocks_for_feature(git_repo, ["a.py"], sha).blocks
    assert blocks[0]["content"] == "committed"
    assert blocks[0]["language"] == "python"
    assert (blocks[0]["line_start"], blocks[0]["line_end"]) == (1, 1)


def test_blocks_read_the_worktree_when_there_is_no_rev(git_repo, commit):
    commit("init", {"a.py": "committed\n"})
    (git_repo / "a.py").write_text("dirty\n", encoding="utf-8")
    blocks = explore.build_code_blocks_for_feature(git_repo, ["a.py"], None).blocks
    assert blocks[0]["content"] == "dirty"


def test_hallucinated_paths_are_skipped_and_reported(git_repo, commit):
    sha = commit("init", {"a.py": "x\n"})
    result = explore.build_code_blocks_for_feature(git_repo, ["nope.py", "a.py"], sha)
    assert [b["filename"] for b in result.blocks] == ["a.py"]
    assert result.skipped == ["nope.py"]


def test_only_the_first_few_files_are_quoted(git_repo, commit):
    files = {f"f{i}.py": "x\n" for i in range(6)}
    sha = commit("init", files)
    result = explore.build_code_blocks_for_feature(git_repo, sorted(files), sha)
    assert len(result.blocks) == explore.MAX_FILES_PER_FEATURE
    # Files past the cap are deliberately unquoted, not failures to read.
    assert result.skipped == []


def test_long_files_are_truncated_to_the_cap(git_repo, commit):
    sha = commit("init", {"big.py": "x\n" * 900})
    blocks = explore.build_code_blocks_for_feature(git_repo, ["big.py"], sha,
                                                   max_block_lines=400).blocks
    assert blocks[0]["line_end"] == 400
    assert len(blocks[0]["content"].splitlines()) == 400


@pytest.mark.parametrize("path", [
    "../outside.py", "a/../../outside.py", "/etc/passwd", "~/.ssh/id_rsa",
    "C:\\Windows\\win.ini", "", "   ",
])
def test_paths_escaping_the_repo_are_refused(path):
    assert explore.is_safe_repo_path(path) is False


@pytest.mark.parametrize("path", ["a.py", "cli/stingray_cli/explore.py", "a/b/c.tsx"])
def test_ordinary_repo_paths_are_allowed(path):
    assert explore.is_safe_repo_path(path) is True


def test_traversal_is_not_read_off_disk(git_repo, commit, monkeypatch):
    """A hallucinated ../ path must never reach the file reader at all."""
    sha = commit("init", {"a.py": "x\n"})
    monkeypatch.setattr(explore.gitctx, "_file_lines",
                        lambda *a, **kw: (_ for _ in ()).throw(
                            AssertionError("unsafe path was read")))
    result = explore.build_code_blocks_for_feature(git_repo, ["../../etc/passwd"], sha)
    assert result.blocks == []
    assert result.skipped == ["../../etc/passwd"]


# --- the command ------------------------------------------------------------

def _agent_returning(monkeypatch, text):
    def fake(prompt, cwd, **kw):
        fake.prompt = prompt
        fake.kwargs = kw
        return text
    monkeypatch.setattr(cmd_explore, "run_agent", fake)
    return fake


def test_dry_run_prints_payloads_and_posts_nothing(git_repo, commit, monkeypatch,
                                                   capsys, isolated_config):
    commit("init", {"backend/auth.py": "def login():\n    pass\n"})
    _agent_returning(monkeypatch, '[{"name": "auth", "title": "Session auth", '
                                  '"description": "D", "files": ["backend/auth.py"]}]')

    args = _args("-C", str(git_repo), "--dry-run")
    assert cmd_explore.cmd_explore(args) == 0

    payloads = json.loads(capsys.readouterr().out)
    assert len(payloads) == 1
    assert payloads[0]["title"] == "Review: Session auth"
    assert payloads[0]["type"] == "code_review"
    assert payloads[0]["code_blocks"][0]["filename"] == "backend/auth.py"


def test_payload_pins_repo_rev_and_branch(git_repo, commit, monkeypatch, capsys,
                                          isolated_config):
    sha = commit("init", {"a.py": "x\n"})
    _agent_returning(monkeypatch,
                     '[{"name": "a", "title": "A", "files": ["a.py"]}]')

    assert cmd_explore.cmd_explore(_args("-C", str(git_repo), "--dry-run")) == 0
    tags = json.loads(capsys.readouterr().out)[0]["tags"]
    assert f"repo:{git_repo.name}" in tags
    assert f"rev:{sha}" in tags
    assert "branch:main" in tags


def test_teach_flag_reaches_the_prompt(git_repo, commit, monkeypatch, isolated_config):
    commit("init", {"a.py": "x\n"})
    spy = _agent_returning(monkeypatch,
                           '[{"name": "a", "title": "A", "files": ["a.py"]}]')
    cmd_explore.cmd_explore(_args("-C", str(git_repo), "--teach", "--dry-run"))
    assert "TEACH MODE" in spy.prompt


def test_max_features_truncates(git_repo, commit, monkeypatch, capsys, isolated_config):
    commit("init", {"a.py": "x\n", "b.py": "y\n", "c.py": "z\n"})
    _agent_returning(monkeypatch, json.dumps([
        {"name": n, "title": n.upper(), "files": [f"{n}.py"]} for n in "abc"
    ]))
    assert cmd_explore.cmd_explore(
        _args("-C", str(git_repo), "--max-features", "2", "--dry-run")) == 0
    out = capsys.readouterr()
    assert len(json.loads(out.out)) == 2
    assert "raise --max-features" in out.err


def test_agent_failure_is_a_clean_error(git_repo, commit, monkeypatch, capsys,
                                        isolated_config):
    commit("init", {"a.py": "x\n"})

    def boom(*a, **kw):
        raise AgentError("no local agent found on PATH")
    monkeypatch.setattr(cmd_explore, "run_agent", boom)

    assert cmd_explore.cmd_explore(_args("-C", str(git_repo), "--dry-run")) == 1
    assert "feature discovery failed" in capsys.readouterr().err


def test_unparseable_output_is_an_error_not_an_invented_ticket(
        git_repo, commit, monkeypatch, capsys, isolated_config):
    commit("init", {"a.py": "x\n"})
    _agent_returning(monkeypatch, "I could not tell what this repo does")
    assert cmd_explore.cmd_explore(_args("-C", str(git_repo), "--dry-run")) == 1
    assert "did not identify any features" in capsys.readouterr().err


def test_features_with_only_unreadable_files_are_skipped(
        git_repo, commit, monkeypatch, capsys, isolated_config):
    commit("init", {"a.py": "x\n"})
    _agent_returning(monkeypatch, json.dumps([
        {"name": "real", "title": "Real", "files": ["a.py"]},
        {"name": "ghost", "title": "Ghost", "files": ["invented.py"]},
    ]))
    assert cmd_explore.cmd_explore(_args("-C", str(git_repo), "--dry-run")) == 0
    out = capsys.readouterr()
    assert [p["title"] for p in json.loads(out.out)] == ["Review: Real"]
    assert "skipping feature 'ghost'" in out.err


def test_partially_unreadable_feature_is_filed_but_warns(
        git_repo, commit, monkeypatch, capsys, isolated_config):
    commit("init", {"a.py": "x\n"})
    _agent_returning(monkeypatch, json.dumps([
        {"name": "half", "title": "Half", "files": ["a.py", "invented.py"]},
    ]))
    assert cmd_explore.cmd_explore(_args("-C", str(git_repo), "--dry-run")) == 0
    out = capsys.readouterr()
    payloads = json.loads(out.out)
    assert [b["filename"] for b in payloads[0]["code_blocks"]] == ["a.py"]
    assert "invented.py" in out.err
    assert "not quoted" in out.err


def test_dirty_tree_warns_that_blocks_come_from_the_commit(
        git_repo, commit, monkeypatch, capsys, isolated_config):
    sha = commit("init", {"a.py": "committed\n"})
    (git_repo / "a.py").write_text("dirty\n", encoding="utf-8")
    _agent_returning(monkeypatch, '[{"name": "a", "title": "A", "files": ["a.py"]}]')

    assert cmd_explore.cmd_explore(_args("-C", str(git_repo), "--dry-run")) == 0
    out = capsys.readouterr()
    assert "uncommitted changes" in out.err
    assert sha[:12] in out.err
    assert json.loads(out.out)[0]["code_blocks"][0]["content"] == "committed"


def test_clean_tree_does_not_warn(git_repo, commit, monkeypatch, capsys,
                                  isolated_config):
    commit("init", {"a.py": "x\n"})
    _agent_returning(monkeypatch, '[{"name": "a", "title": "A", "files": ["a.py"]}]')
    assert cmd_explore.cmd_explore(_args("-C", str(git_repo), "--dry-run")) == 0
    assert "uncommitted changes" not in capsys.readouterr().err


def test_assign_bot_dry_run_survives_a_missing_bot_id(git_repo, commit, monkeypatch,
                                                      capsys, isolated_config):
    """--dry-run POSTs nothing, so an unresolvable assignee must not crash it."""
    commit("init", {"a.py": "x\n"})
    _agent_returning(monkeypatch, '[{"name": "a", "title": "A", "files": ["a.py"]}]')
    profile = type("P", (), {"url": "http://x", "web_url": "http://x",
                             "api_key": "k", "bot_user_id": None, "describe": {}})()
    monkeypatch.setattr(cmd_explore, "profile_from", lambda a: profile)

    assert cmd_explore.cmd_explore(
        _args("-C", str(git_repo), "--assign-bot", "--dry-run")) == 0
    out = capsys.readouterr()
    assert "assigned_to" not in json.loads(out.out)[0]
    assert "--assign-bot needs a bot user id" in out.err


def test_assign_bot_without_a_profile_dry_runs(git_repo, commit, monkeypatch, capsys,
                                               isolated_config):
    """No profile stored at all: discovery is local, so --dry-run still works."""
    from stingray_cli.config import ConfigError
    commit("init", {"a.py": "x\n"})
    _agent_returning(monkeypatch, '[{"name": "a", "title": "A", "files": ["a.py"]}]')

    def no_profile(args):
        raise ConfigError("no profile configured")
    monkeypatch.setattr(cmd_explore, "profile_from", no_profile)

    assert cmd_explore.cmd_explore(
        _args("-C", str(git_repo), "--assign-bot", "--dry-run")) == 0
    assert len(json.loads(capsys.readouterr().out)) == 1


def test_assign_bot_without_a_bot_id_fails_before_the_agent_runs(
        git_repo, commit, monkeypatch, capsys, isolated_config):
    """A real run reports the config mistake instead of spending minutes first."""
    commit("init", {"a.py": "x\n"})
    profile = type("P", (), {"url": "http://x", "web_url": "http://x",
                             "api_key": "k", "bot_user_id": None, "describe": {}})()
    monkeypatch.setattr(cmd_explore, "profile_from", lambda a: profile)

    def boom(*a, **kw):
        raise AssertionError("the agent must not run")
    monkeypatch.setattr(cmd_explore, "run_agent", boom)

    assert cmd_explore.cmd_explore(
        _args("-C", str(git_repo), "--assign-bot", "--yes")) == 1
    assert "--assign-bot needs a bot user id" in capsys.readouterr().err


def test_reserved_tag_is_refused_before_the_agent_runs(git_repo, commit, monkeypatch,
                                                       isolated_config):
    from stingray_cli.config import ConfigError
    commit("init", {"a.py": "x\n"})

    def boom(*a, **kw):
        raise AssertionError("the agent must not run")
    monkeypatch.setattr(cmd_explore, "run_agent", boom)

    with pytest.raises(ConfigError):
        cmd_explore.cmd_explore(_args("-C", str(git_repo), "--tag", "dangerous",
                                      "--dry-run"))


def test_files_each_ticket(git_repo, commit, monkeypatch, fake_client, isolated_config):
    commit("init", {"a.py": "x\n", "b.py": "y\n"})
    _agent_returning(monkeypatch, json.dumps([
        {"name": "a", "title": "A", "files": ["a.py"]},
        {"name": "b", "title": "B", "files": ["b.py"]},
    ]))
    profile = type("P", (), {"url": "http://x", "web_url": "http://x",
                             "api_key": "k", "bot_user_id": 2, "describe": {}})()
    monkeypatch.setattr(cmd_explore, "client_from", lambda a: (fake_client, profile))
    monkeypatch.setattr(cmd_explore, "profile_from", lambda a: profile)

    assert cmd_explore.cmd_explore(_args("-C", str(git_repo), "--yes")) == 0
    assert [t["title"] for t in fake_client.created] == ["Review: A", "Review: B"]


def test_assign_bot_uses_the_profile(git_repo, commit, monkeypatch, fake_client,
                                     isolated_config):
    commit("init", {"a.py": "x\n"})
    _agent_returning(monkeypatch, '[{"name": "a", "title": "A", "files": ["a.py"]}]')
    profile = type("P", (), {"url": "http://x", "web_url": "http://x",
                             "api_key": "k", "bot_user_id": 7, "describe": {}})()
    monkeypatch.setattr(cmd_explore, "client_from", lambda a: (fake_client, profile))
    monkeypatch.setattr(cmd_explore, "profile_from", lambda a: profile)

    assert cmd_explore.cmd_explore(_args("-C", str(git_repo), "--assign-bot", "--yes")) == 0
    assert fake_client.created[0]["assigned_to"] == 7


def test_explore_is_registered_on_the_parser():
    args = build_parser().parse_args(["explore", "--teach"])
    assert args.func is cmd_explore.cmd_explore
    assert args.teach is True
