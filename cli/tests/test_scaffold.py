"""Template rendering, stub scanning, and the scaffold ticket flow."""
from __future__ import annotations

import subprocess

import pytest

from stingray_cli import scaffold as sc

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
