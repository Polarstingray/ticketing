"""The --describe pass: output parsing, tag safety, and the fallback ladder."""
from __future__ import annotations

import pytest

from stingray_cli import describe, gitctx
from stingray_cli.agent import AgentError
from stingray_cli.describe import DescribeError, parse_response

GOOD = {
    "title": "Tighten the retry bound",
    "description": "The retry loop could spin forever.",
    "priority": "high",
    "tags": ["backend"],
}


def test_parses_bare_json():
    result = parse_response('{"title": "T", "description": "D", "priority": "low"}')
    assert (result.title, result.priority) == ("T", "low")


def test_parses_fenced_json():
    text = f'Here you go:\n\n```json\n{GOOD!r}\n```'.replace("'", '"')
    result = parse_response(text)
    assert result.title == "Tighten the retry bound"


def test_prefers_the_last_fence():
    """Models often think out loud in an early block before committing to one."""
    text = (
        '```json\n{"title": "draft", "description": "d"}\n```\n'
        'on reflection:\n'
        '```json\n{"title": "final", "description": "d"}\n```'
    )
    assert parse_response(text).title == "final"


def test_falls_back_to_outermost_braces():
    text = 'Sure! {"title": "T", "description": "D"} — hope that helps.'
    assert parse_response(text).title == "T"


def test_no_json_raises():
    with pytest.raises(DescribeError):
        parse_response("I could not determine what changed.")


def test_missing_title_raises():
    with pytest.raises(DescribeError):
        parse_response('{"description": "D"}')


def test_unknown_priority_is_dropped(capsys):
    result = parse_response('{"title": "T", "description": "D", "priority": "urgent"}')
    assert result.priority == ""
    assert "urgent" in capsys.readouterr().err


def test_reserved_tags_are_dropped():
    """A hallucinated control tag must never reach the payload."""
    text = (
        '{"title": "T", "description": "D", "tags": '
        '["backend", "dangerous", "repo:evil", "claude:planning", "parent:1", '
        '"review-by:2", "delegate", "fix"]}'
    )
    assert parse_response(text).tags == ["backend"]


def test_tags_are_capped_deduped_and_lowercased():
    text = ('{"title": "T", "description": "D", "tags": '
            '["A", "a", "b", "c", "d", "e", "f", "g"]}')
    tags = parse_response(text).tags
    assert tags == ["a", "b", "c", "d", "e"]


def test_overlong_title_is_truncated():
    result = parse_response('{"title": "%s", "description": "D"}' % ("x" * 500))
    assert len(result.title) == 120


def test_overlong_tag_is_dropped():
    text = '{"title": "T", "description": "D", "tags": ["%s", "ok"]}' % ("x" * 50)
    assert parse_response(text).tags == ["ok"]


# --- the fallback ladder -----------------------------------------------------

def _change(git_repo, commit):
    commit("first", {"a.py": "one\n"})
    commit("second", {"a.py": "two\n"})
    return gitctx.resolve_range(git_repo, "HEAD~1..HEAD", include_worktree=False)


def test_missing_agent_falls_back(git_repo, commit, monkeypatch, capsys):
    def boom(*_a, **_kw):
        raise AgentError("no local agent found on PATH")
    monkeypatch.setattr(describe, "run_agent", boom)

    assert describe.describe_change(_change(git_repo, commit)) is None
    assert "--describe failed" in capsys.readouterr().err


def test_unparseable_output_falls_back(git_repo, commit, monkeypatch, capsys):
    monkeypatch.setattr(describe, "run_agent", lambda *a, **kw: "no json here")
    assert describe.describe_change(_change(git_repo, commit)) is None
    assert "--describe failed" in capsys.readouterr().err


def test_required_reraises(git_repo, commit, monkeypatch):
    monkeypatch.setattr(describe, "run_agent", lambda *a, **kw: "no json here")
    with pytest.raises(DescribeError):
        describe.describe_change(_change(git_repo, commit), required=True)


def test_success_returns_a_suggestion(git_repo, commit, monkeypatch):
    monkeypatch.setattr(
        describe, "run_agent",
        lambda *a, **kw: '{"title": "T", "description": "D", "priority": "high"}',
    )
    result = describe.describe_change(_change(git_repo, commit))
    assert (result.title, result.priority) == ("T", "high")


def test_prompt_contains_the_diff_and_the_contract(git_repo, commit):
    prompt = describe.build_prompt(_change(git_repo, commit))
    assert "Diff:" in prompt
    assert "fenced ```json block" in prompt
    assert "Never tags" in prompt


def test_prompt_bounds_a_huge_diff_and_diffstat(git_repo, commit, monkeypatch):
    """Both the diff and the diffstat are capped, so one enormous change can't
    turn a description pass into an expensive prompt."""
    change = _change(git_repo, commit)
    huge = "x" * (describe.MAX_DIFF_CHARS * 3)
    monkeypatch.setattr(gitctx, "git", lambda *a, **kw: huge)
    monkeypatch.setattr(gitctx, "diffstat", lambda _c: huge)

    prompt = describe.build_prompt(change)
    assert "(TRUNCATED)" in prompt
    assert len(prompt) < describe.MAX_DIFF_CHARS + describe.MAX_STAT_CHARS + 2_000
