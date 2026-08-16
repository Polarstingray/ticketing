"""Template rendering, stub scanning, and the scaffold ticket flow."""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from stingray_cli import cmd_scaffold
from stingray_cli import scaffold as sc
from stingray_cli.config import ConfigError

PY_WITH_STUBS = '''\
"""A module."""


def alpha(x):
    """Do alpha."""
    # STINGRAY-STUB: implement alpha over x.
    # ACCEPTANCE: returns 0 for an empty x.
    raise NotImplementedError("STINGRAY-STUB")


def beta(y):
    """Do beta."""
    # STINGRAY-STUB: implement beta, wrapping onto
    # a second comment line for good measure.
    raise NotImplementedError("STINGRAY-STUB")


def already_done(z):
    return z + 1
'''

JS_WITH_STUB = """\
export function gamma(a) {
  // STINGRAY-STUB: implement gamma.
  // ACCEPTANCE: throws on a null argument.
  throw new Error("STINGRAY-STUB");
}
"""


def test_templates_are_discoverable():
    names = [t.name for t in sc.available_templates()]
    assert "python-cli" in names
    assert all(t.description for t in sc.available_templates())


def test_unknown_template_lists_the_known_ones():
    with pytest.raises(sc.ScaffoldError) as exc:
        sc.load_template("nope")
    assert "python-cli" in str(exc.value)


def test_render_substitutes_content_and_paths(tmp_path):
    template = sc.load_template("python-cli")
    dest = tmp_path / "out"
    dest.mkdir()
    sc.render(template, dest, {"project_name": "widget", "package": "widget",
                               "description": "A widget."})

    assert (dest / "widget" / "main.py").is_file(), "path segments must substitute"
    assert not list(dest.rglob("*.tmpl")), ".tmpl suffixes must be stripped"
    assert "widget" in (dest / "pyproject.toml").read_text()
    assert "{{project_name}}" not in (dest / "README.md").read_text()


def test_scan_finds_stubs_with_summaries(tmp_path):
    (tmp_path / "m.py").write_text(PY_WITH_STUBS, encoding="utf-8")
    stubs = sc.scan_stubs(tmp_path)
    assert len(stubs) == 2
    assert stubs[0].summary == "implement alpha over x."
    assert stubs[0].acceptance == "returns 0 for an empty x."


def test_wrapped_summary_is_joined(tmp_path):
    """A note that wraps onto a second comment line must not be truncated."""
    (tmp_path / "m.py").write_text(PY_WITH_STUBS, encoding="utf-8")
    beta = sc.scan_stubs(tmp_path)[1]
    assert beta.summary == "implement beta, wrapping onto a second comment line for good measure."


def test_enclosing_block_covers_the_function(tmp_path):
    (tmp_path / "m.py").write_text(PY_WITH_STUBS, encoding="utf-8")
    alpha = sc.scan_stubs(tmp_path)[0]
    lines = PY_WITH_STUBS.splitlines()
    block = lines[alpha.block_start - 1:alpha.block_end]
    assert block[0].startswith("def alpha")
    assert any("NotImplementedError" in line for line in block)
    assert not any("def beta" in line for line in block), "must not run into the next def"


def test_scan_handles_other_comment_syntaxes(tmp_path):
    (tmp_path / "m.js").write_text(JS_WITH_STUB, encoding="utf-8")
    stubs = sc.scan_stubs(tmp_path)
    assert len(stubs) == 1
    assert stubs[0].summary == "implement gamma."
    assert stubs[0].acceptance == "throws on a null argument."


def test_prose_files_are_not_scanned(tmp_path):
    """A project's CLAUDE.md documents the convention and so contains the marker;
    filing a ticket against the documentation would be nonsense."""
    (tmp_path / "CLAUDE.md").write_text(
        "Stubs look like:\n\n    # STINGRAY-STUB: do the thing.\n", encoding="utf-8")
    (tmp_path / "m.py").write_text(PY_WITH_STUBS, encoding="utf-8")
    assert {s.path for s in sc.scan_stubs(tmp_path)} == {"m.py"}


def test_git_dir_is_skipped(tmp_path):
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "hook.py").write_text(PY_WITH_STUBS, encoding="utf-8")
    assert sc.scan_stubs(tmp_path) == []


def test_validate_rejects_a_tree_with_no_stubs(tmp_path):
    (tmp_path / "m.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    problems = sc.validate_tree(tmp_path)
    assert any("no STINGRAY-STUB markers" in p for p in problems)


def test_validate_rejects_unparseable_python(tmp_path):
    (tmp_path / "m.py").write_text(PY_WITH_STUBS, encoding="utf-8")
    (tmp_path / "broken.py").write_text("def (:\n", encoding="utf-8")
    assert any("broken.py" in p for p in sc.validate_tree(tmp_path))


def test_validate_rejects_bad_json(tmp_path):
    (tmp_path / "m.py").write_text(PY_WITH_STUBS, encoding="utf-8")
    (tmp_path / "package.json").write_text("{nope", encoding="utf-8")
    assert any("package.json" in p for p in sc.validate_tree(tmp_path))


def test_validate_passes_a_good_tree(tmp_path):
    (tmp_path / "m.py").write_text(PY_WITH_STUBS, encoding="utf-8")
    (tmp_path / "package.json").write_text('{"ok": true}', encoding="utf-8")
    assert sc.validate_tree(tmp_path) == []


def test_git_init_and_commit(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_AUTHOR_NAME", "T")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "T")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@example.com")
    (tmp_path / "m.py").write_text(PY_WITH_STUBS, encoding="utf-8")

    sc.git_init_and_commit(tmp_path, "scaffold: test")
    log = subprocess.run(["git", "-C", str(tmp_path), "log", "--oneline"],
                         capture_output=True, text=True)
    assert "scaffold: test" in log.stdout


def test_refuses_an_existing_repo(tmp_path):
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    with pytest.raises(sc.ScaffoldError, match="already a git repository"):
        sc.git_init_and_commit(tmp_path, "x")


def test_rendered_template_has_stubs_and_validates(tmp_path):
    """The shipped template must actually produce a scaffold worth ticketing."""
    template = sc.load_template("python-cli")
    dest = tmp_path / "out"
    dest.mkdir()
    sc.render(template, dest, {"project_name": "widget", "package": "widget",
                               "description": "A widget."})
    assert sc.validate_tree(dest) == []
    assert len(sc.scan_stubs(dest)) >= 3


# --- --intent / --describe naming -------------------------------------------

def _args(**kw):
    base = dict(intent=None, describe_alias=None, agent=None, agent_timeout=None,
                profile=None, url=None, api_key=None, yes=True)
    base.update(kw)
    return SimpleNamespace(**base)


def test_intent_is_used_as_is():
    assert cmd_scaffold._resolve_intent(_args(intent="a log parser")) == "a log parser"


def test_describe_alias_still_works_but_warns(capsys):
    """Existing invocations must not break on the rename."""
    args = _args(describe_alias="a log parser")
    assert cmd_scaffold._resolve_intent(args) == "a log parser"
    assert args.intent == "a log parser", "must normalize onto args.intent"
    err = capsys.readouterr().err
    assert "deprecated" in err and "--intent" in err


def test_passing_both_is_an_error():
    with pytest.raises(ConfigError, match="only --intent"):
        cmd_scaffold._resolve_intent(_args(intent="a", describe_alias="b"))


def test_no_intent_is_none_and_silent(capsys):
    assert cmd_scaffold._resolve_intent(_args()) is None
    assert capsys.readouterr().err == ""


# --- adaptation timeout ------------------------------------------------------

def test_adapt_timeout_default_exceeds_describe_default():
    """Adapting a tree is a strictly bigger job than describing a diff."""
    from stingray_cli import describe
    assert cmd_scaffold.DEFAULT_ADAPT_TIMEOUT > describe.DEFAULT_TIMEOUT
    assert cmd_scaffold.DEFAULT_ADAPT_TIMEOUT >= 1200


def test_adapt_uses_the_default_timeout(tmp_path, monkeypatch):
    """Regression: this was hardcoded to 600s and ignored config entirely."""
    seen = {}
    monkeypatch.setattr(cmd_scaffold, "_profile_or_none", lambda a: None)
    monkeypatch.setattr("stingray_cli.agent.run",
                        lambda *a, **kw: seen.update(kw) or "")

    template = sc.load_template("python-cli")
    work = tmp_path / "w"
    work.mkdir()
    sc.render(template, work, {"project_name": "p", "package": "p", "description": "d"})
    cmd_scaffold._adapt(work, template, _args(intent="x"), {"project_name": "p"})

    assert seen["timeout"] == cmd_scaffold.DEFAULT_ADAPT_TIMEOUT
    assert seen["edit"] is True, "the adaptation pass must be allowed to edit files"


def test_adapt_timeout_precedence_flag_over_profile(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(cmd_scaffold, "_profile_or_none",
                        lambda a: SimpleNamespace(describe={"timeout": 111,
                                                            "agent": "opencode"}))
    monkeypatch.setattr("stingray_cli.agent.run",
                        lambda *a, **kw: seen.update(kw) or "")

    template = sc.load_template("python-cli")
    work = tmp_path / "w"
    work.mkdir()
    sc.render(template, work, {"project_name": "p", "package": "p", "description": "d"})

    # Profile alone.
    cmd_scaffold._adapt(work, template, _args(intent="x"), {"project_name": "p"})
    assert seen["timeout"] == 111
    assert seen["agent"] == "opencode"

    # Flag beats profile.
    cmd_scaffold._adapt(work, template, _args(intent="x", agent_timeout=222,
                                              agent="claude"), {"project_name": "p"})
    assert seen["timeout"] == 222
    assert seen["agent"] == "claude"


# --- fastapi-spa template ----------------------------------------------------

def test_fastapi_spa_is_available():
    names = [t.name for t in sc.available_templates()]
    assert "fastapi-spa" in names


def test_fastapi_spa_renders_validates_and_stubs_both_sides(tmp_path):
    """A shipped template must render cleanly, validate, and leave stubs on both
    sides of the wire — a backend-only backlog would miss half the work."""
    template = sc.load_template("fastapi-spa")
    dest = tmp_path / "out"
    dest.mkdir()
    sc.render(template, dest, {"project_name": "notekeeper", "package": "notekeeper",
                               "description": "Keeps notes."})

    assert sc.validate_tree(dest) == []
    assert not list(dest.rglob("*.tmpl"))
    # Check for leftover *placeholders* specifically, not any `{{`: JSX writes
    # `style={{...}}` for an inline style object, and substitution must leave
    # that alone (it matches on the full `{{name}}` token, not the braces).
    leftover = [
        p for p in dest.rglob("*") if p.is_file()
        and any(tok in p.read_text(encoding="utf-8", errors="ignore")
                for tok in ("{{project_name}}", "{{package}}", "{{description}}"))
    ]
    assert not leftover, f"unsubstituted placeholders in {leftover}"

    stubs = sc.scan_stubs(dest)
    suffixes = {Path(s.path).suffix for s in stubs}
    assert ".py" in suffixes
    assert suffixes & {".js", ".jsx"}, "the frontend must carry stubs too"
    assert len(stubs) >= 6


def test_fastapi_spa_python_parses(tmp_path):
    import ast
    template = sc.load_template("fastapi-spa")
    dest = tmp_path / "out"
    dest.mkdir()
    sc.render(template, dest, {"project_name": "np", "package": "np", "description": "d"})
    for path in dest.rglob("*.py"):
        ast.parse(path.read_text(encoding="utf-8"))


# --- JS brace scanning -------------------------------------------------------
# A false positive here is expensive: validate_tree failing discards the agent's
# adaptation and silently falls back to the plain template. So the bias is
# firmly toward returning 0 when anything is ambiguous.

@pytest.mark.parametrize("source", [
    "function f() { return 1; }",
    'const s = "{{{"; function f() {}',
    "const s = '}}}'; function f() {}",
    "// } } }\nfunction f() {}",
    "/* { { { */ function f() {}",
    "const s = `a { b }`; function f() {}",
    "const s = `a ${ {x:1}.x } b`; function f() {}",
    "const s = `${ `${ 1 }` }`; function f() {}",       # nested template
    "const s = `a${1}b{`; function f() {}",
    "const r = /[{]/; function f() {}",                  # regex containing a brace
    "const r = /a{2,3}/; function f() {}",               # regex quantifier
    "const r = /[/{]/; function f() {}",                 # slash inside a class
    "const x = a / b; function f() {}",                  # division, not regex
    "const e = <div style={{color:'red'}}>hi</div>;",    # JSX double brace
    'const s = "he said \\"{\\""; function f() {}',
    "const s = `a \\` { `; function f() {}",
    'const s = "abc\nfunction f() {',                    # unterminated string
    "/* { { {",                                          # unterminated comment
    "const s = `abc { ",                                 # unterminated template
    "",
])
def test_balanced_or_ambiguous_reports_zero(source):
    assert sc.brace_imbalance(source) == 0


@pytest.mark.parametrize("source,expected", [
    ("function f() { if (x) {", 2),
    ("function f() {", 1),
    ("const o = { a: { b: 1 }", 1),
])
def test_truncation_is_detected(source, expected):
    assert sc.brace_imbalance(source) == expected


def test_validate_flags_a_truncated_js_file(tmp_path):
    (tmp_path / "m.py").write_text(PY_WITH_STUBS, encoding="utf-8")
    (tmp_path / "app.js").write_text("export function f() {\n  if (x) {\n",
                                     encoding="utf-8")
    problems = sc.validate_tree(tmp_path)
    assert any("app.js" in p and "truncated" in p for p in problems)


def test_validate_ignores_node_modules(tmp_path):
    (tmp_path / "m.py").write_text(PY_WITH_STUBS, encoding="utf-8")
    vendored = tmp_path / "node_modules" / "dep"
    vendored.mkdir(parents=True)
    (vendored / "broken.js").write_text("function f() {", encoding="utf-8")
    assert sc.validate_tree(tmp_path) == []
