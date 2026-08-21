"""The guided-project path: the handout, the exercise side-channel, and the payloads.

Covers the pieces both front doors share (``stingray_client.stubs``) plus the
CLI-only handout generation (``stingray_cli.guided``). The resolver's use of the
same shared code is tested in ``resolver/tests/test_scaffold_followup.py``.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from stingray_cli import cmd_scaffold, guided
from stingray_cli import scaffold as sc
from stingray_client import stubs as stubs_mod

PY_WITH_STUBS = '''\
def alpha(x):
    """Do alpha."""
    # STINGRAY-STUB: implement alpha over x.
    # ACCEPTANCE: returns 0 for an empty x.
    raise NotImplementedError("STINGRAY-STUB")


def beta(y):
    """Do beta."""
    # STINGRAY-STUB: implement beta.
    raise NotImplementedError("STINGRAY-STUB")
'''


@pytest.fixture
def stubbed(tmp_path: Path) -> Path:
    (tmp_path / "mod.py").write_text(PY_WITH_STUBS, encoding="utf-8")
    return tmp_path


# --- the shared scanner still behaves as the CLI always expected -------------

def test_scaffold_reexports_the_shared_scanner():
    """Existing callers import these off stingray_cli.scaffold; keep that working."""
    assert sc.scan_stubs is stubs_mod.scan_stubs
    assert sc.Stub is stubs_mod.Stub
    assert sc.STUB_MARKER == "STINGRAY-STUB"


def test_scan_can_be_restricted_to_touched_files(tmp_path: Path):
    """The resolver stubs into a repo that may already use the convention."""
    (tmp_path / "new.py").write_text(PY_WITH_STUBS, encoding="utf-8")
    (tmp_path / "old.py").write_text(PY_WITH_STUBS, encoding="utf-8")

    everything = stubs_mod.scan_stubs(tmp_path)
    assert {s.path for s in everything} == {"new.py", "old.py"}

    only_new = stubs_mod.scan_stubs(tmp_path, only={"new.py"})
    assert {s.path for s in only_new} == {"new.py"}


def test_vendored_directories_are_skipped(tmp_path: Path):
    """A stray node_modules in a real repo must not file 400 tickets."""
    vendored = tmp_path / "node_modules" / "pkg"
    vendored.mkdir(parents=True)
    (vendored / "index.py").write_text(PY_WITH_STUBS, encoding="utf-8")
    (tmp_path / "mine.py").write_text(PY_WITH_STUBS, encoding="utf-8")

    assert {s.path for s in stubs_mod.scan_stubs(tmp_path)} == {"mine.py"}


# --- stub ticket payloads ----------------------------------------------------

def test_stub_payload_links_the_epic_and_never_self_drives(stubbed):
    stub = stubs_mod.scan_stubs(stubbed)[0]
    payload = stubs_mod.build_stub_payload(
        stubbed, "proj", stub, epic_id=42, repo="proj")

    assert "epic:42" in payload["tags"]
    assert not any(t.startswith("parent:") for t in payload["tags"]), \
        "parent: would make the child self-driving — a learner's backlog must not be"
    assert "repo:proj" in payload["tags"]
    assert payload["type"] == "code_review"
    assert payload["code_blocks"][0]["language"] == "python"
    assert "**Acceptance:** returns 0 for an empty x." in payload["description"]


def test_stub_payload_prefers_agent_written_prose(stubbed):
    stub = stubs_mod.scan_stubs(stubbed)[0]
    payload = stubs_mod.build_stub_payload(
        stubbed, "proj", stub, epic_id=42, repo="proj",
        body="Work out what an empty input should mean before you write anything.")

    assert "Work out what an empty input should mean" in payload["description"]
    assert "**Acceptance:**" in payload["description"], "acceptance still comes along"


def test_epic_tag_is_free_not_reserved():
    """`epic:` must stay outside the reserved set, or non-admin keys can't set it."""
    from stingray_client.tickets import is_reserved_tag
    assert not is_reserved_tag(stubs_mod.epic_tag(7))


# --- the deterministic handout ----------------------------------------------

def _handout(stubs, milestones=4):
    return guided.deterministic_assignment(
        project_name="hw3", template_description="A thing.",
        template_notes="", intent="a library loan tracker",
        stubs=stubs, milestones=milestones)


def test_deterministic_handout_has_every_section(stubbed):
    text = _handout(stubs_mod.scan_stubs(stubbed))
    for heading in ("# hw3", "## What you are building", "## Milestones",
                    "## Rubric", "## Getting started"):
        assert heading in text
    assert "a library loan tracker" in text


def test_every_stub_lands_in_a_milestone(stubbed):
    stubs = stubs_mod.scan_stubs(stubbed)
    text = _handout(stubs)
    for stub in stubs:
        assert f"`{stub.path}:{stub.line}`" in text


def test_milestones_are_capped_by_the_stub_count(stubbed):
    """Asking for 10 milestones over 2 stubs must not emit 8 empty ones."""
    text = _handout(stubs_mod.scan_stubs(stubbed), milestones=10)
    assert text.count("### Milestone ") == 2


def test_handout_survives_having_no_stubs():
    assert "## Rubric" in _handout([])


def test_epic_summary_drops_getting_started(stubbed):
    text = _handout(stubs_mod.scan_stubs(stubbed))
    summary = guided.epic_summary(text)
    assert "## Milestones" in summary
    assert "## Getting started" not in summary, \
        "run commands are only useful next to a checkout"


def test_epic_summary_truncates_loudly(stubbed):
    summary = guided.epic_summary("# h\n" + "\n".join(f"line {i}" for i in range(2000)),
                                  limit=200)
    assert len(summary) < 400
    assert "truncated" in summary


# --- .gitignore --------------------------------------------------------------

def test_gitignore_entry_is_added_once(tmp_path: Path):
    guided.ensure_gitignored(tmp_path, "ASSIGNMENT.md")
    guided.ensure_gitignored(tmp_path, "ASSIGNMENT.md")
    lines = (tmp_path / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert lines.count("ASSIGNMENT.md") == 1


def test_gitignore_appends_to_an_existing_file_without_a_trailing_newline(tmp_path: Path):
    (tmp_path / ".gitignore").write_text("dist/", encoding="utf-8")
    guided.ensure_gitignored(tmp_path, "ASSIGNMENT.md")
    lines = (tmp_path / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "dist/" in lines and "ASSIGNMENT.md" in lines


# --- the exercise side-channel ----------------------------------------------

def test_exercises_are_read_and_the_file_is_consumed(tmp_path: Path):
    (tmp_path / guided.EXERCISES_FILE).write_text(json.dumps({"exercises": [
        {"stub": "mod.py:3", "title": "Handle the empty case", "body": "Think first."},
    ]}), encoding="utf-8")

    exercises = guided.read_exercises(tmp_path)
    assert exercises["mod.py:3"]["title"] == "Handle the empty case"
    assert not (tmp_path / guided.EXERCISES_FILE).exists(), \
        "the side-channel must never survive into the project"


def test_malformed_exercises_degrade_quietly(tmp_path: Path, capsys):
    (tmp_path / guided.EXERCISES_FILE).write_text("{not json", encoding="utf-8")
    assert guided.read_exercises(tmp_path) == {}
    assert "warning" in capsys.readouterr().err
    assert not (tmp_path / guided.EXERCISES_FILE).exists()


def test_missing_exercises_file_is_not_an_error(tmp_path: Path):
    assert guided.read_exercises(tmp_path) == {}


def test_exercise_lookup_matches_on_path_and_line(stubbed):
    stub = stubs_mod.scan_stubs(stubbed)[0]
    exercises = {f"{stub.path}:{stub.line}": {"title": "T", "body": "B"}}
    assert guided.exercise_for(exercises, stub) == ("T", "B")
    assert guided.exercise_for({}, stub) == (None, None)


# --- the prompt --------------------------------------------------------------

def test_assignment_prompt_forbids_solutions_and_names_the_real_stubs(stubbed):
    stubs = stubs_mod.scan_stubs(stubbed)
    prompt = guided.assignment_prompt(
        project_name="hw3", template_description="d", template_notes="n",
        intent="a loan tracker", stubs=stubs, level="intro", milestones=3)

    assert "Do NOT give away implementations" in prompt
    assert "no pseudocode" in prompt.lower()
    assert guided.ASSIGNMENT_FILE in prompt and guided.EXERCISES_FILE in prompt
    assert "first- or second-year student" in prompt, "level must reach the prompt"
    for stub in stubs:
        assert f"{stub.path}:{stub.line}" in prompt


@pytest.mark.parametrize("level", guided.COURSE_LEVELS)
def test_every_course_level_has_guidance(level, stubbed):
    prompt = guided.assignment_prompt(
        project_name="p", template_description="d", template_notes="",
        intent=None, stubs=stubs_mod.scan_stubs(stubbed), level=level, milestones=2)
    assert guided._LEVEL_GUIDANCE[level] in prompt


# --- the --guided wiring -----------------------------------------------------

def _guided_args(**kw):
    base = dict(intent=None, describe_alias=None, agent=None, agent_timeout=None,
                profile=None, url=None, api_key=None, yes=True, no_ai=True,
                guided=True, course_level="intermediate", milestones=4,
                no_assignment=False, name=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_guided_writes_the_handout_and_ignores_it(stubbed):
    template = SimpleNamespace(name="t", description="d", notes="")
    assignment, exercises = cmd_scaffold._guide(
        stubbed, template, _guided_args(), "a loan tracker",
        stubs_mod.scan_stubs(stubbed))

    assert "## Milestones" in assignment
    assert exercises == {}
    assert (stubbed / guided.ASSIGNMENT_FILE).is_file()
    assert "ASSIGNMENT.md" in (stubbed / ".gitignore").read_text(encoding="utf-8")


def test_no_assignment_still_mirrors_onto_the_epic(stubbed):
    """--no-assignment is about the file, not about losing the coursework."""
    template = SimpleNamespace(name="t", description="d", notes="")
    assignment, _ = cmd_scaffold._guide(
        stubbed, template, _guided_args(no_assignment=True), None,
        stubs_mod.scan_stubs(stubbed))

    assert "## Milestones" in assignment
    assert not (stubbed / guided.ASSIGNMENT_FILE).exists()
