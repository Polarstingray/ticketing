"""The agent-runner abstraction: registry, factory, and dispatch."""
import pytest

import agents
import resolve_tickets as rt


def test_claude_runner_is_registered():
    runner = agents.get_runner("claude")
    assert runner.name == "claude"
    assert runner.label == "Claude"


def test_unknown_agent_exits_with_guidance():
    with pytest.raises(SystemExit) as exc:
        agents.get_runner("nope")
    msg = str(exc.value)
    assert "nope" in msg
    assert "claude" in msg  # lists the registered agents


def test_codex_runner_is_a_template_not_registered():
    # The template exists but isn't wired up, so selecting it fails fast.
    assert "codex" not in agents._REGISTRY
    with pytest.raises(NotImplementedError):
        agents.CodexRunner().run(None, "p", None, "plan", None)


def test_run_agent_dispatches_to_claude(monkeypatch, fake_cfg, tmp_path):
    calls = {}

    def fake_run_claude(cfg, prompt, cwd, mode, log_path):
        calls["args"] = (prompt, mode)
        return True, "ok"

    monkeypatch.setattr(rt, "run_claude", fake_run_claude)
    ok, text = rt.run_agent(fake_cfg, "do it", tmp_path, "plan", tmp_path / "l.log")
    assert ok and text == "ok"
    assert calls["args"] == ("do it", "plan")


def test_register_runner_requires_name():
    class Nameless(agents.AgentRunner):
        def run(self, cfg, prompt, cwd, mode, log_path):
            return True, ""

    with pytest.raises(ValueError):
        agents.register_runner(Nameless())
