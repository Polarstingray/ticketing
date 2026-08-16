"""Git range resolution and diff→code_block mapping."""
from __future__ import annotations

import pytest

from stingray_cli import gitctx
from tests.conftest import run_git

BODY = "\n".join(f"line {i}" for i in range(1, 41)) + "\n"


def test_repo_root(git_repo, commit):
    commit("init", {"a.txt": "x\n"})
    assert gitctx.repo_root(git_repo) == git_repo
    sub = git_repo / "nested"
    sub.mkdir()
    assert gitctx.repo_root(sub) == git_repo


def test_not_a_repo(tmp_path):
    with pytest.raises(gitctx.GitError):
        gitctx.repo_root(tmp_path)


def test_default_range_is_last_commit(git_repo, commit):
    commit("first", {"a.py": BODY})
    commit("second", {"a.py": BODY.replace("line 5", "CHANGED 5")})
    change = gitctx.resolve_range(git_repo, None)
    assert change.range == "HEAD~1..HEAD"
    assert len(change.commits) == 1


def test_root_commit_falls_back_to_empty_tree(git_repo, commit):
    """A repo with one commit has no HEAD~1; the first commit must still review."""
    commit("only", {"a.py": BODY})
    change = gitctx.resolve_range(git_repo, None)
    assert change.range == f"{gitctx.EMPTY_TREE}..HEAD"
    result = gitctx.collect_blocks(change)
    assert result.blocks, "the first commit should produce blocks"


def test_worktree_changes_are_included_by_default(git_repo, commit):
    commit("first", {"a.py": BODY})
    commit("second", {"a.py": BODY.replace("line 5", "CHANGED 5")})
    (git_repo / "b.py").write_text(BODY.replace("line 9", "DIRTY 9"), encoding="utf-8")
    run_git(git_repo, "add", "-A")

    change = gitctx.resolve_range(git_repo, None)
    assert change.worktree is True
    files = {b["filename"] for b in gitctx.collect_blocks(change).blocks}
    assert files == {"a.py", "b.py"}


def test_no_worktree_flag_drops_dirty_changes(git_repo, commit):
    commit("first", {"a.py": BODY})
    commit("second", {"a.py": BODY.replace("line 5", "CHANGED 5")})
    (git_repo / "b.py").write_text(BODY, encoding="utf-8")
    run_git(git_repo, "add", "-A")

    change = gitctx.resolve_range(git_repo, None, include_worktree=False)
    files = {b["filename"] for b in gitctx.collect_blocks(change).blocks}
    assert files == {"a.py"}


def test_explicit_range_does_not_fold_in_worktree(git_repo, commit):
    """`stingray review <sha>` must be reproducible, so it stays committed-only."""
    commit("first", {"a.py": BODY})
    commit("second", {"a.py": BODY.replace("line 5", "CHANGED 5")})
    (git_repo / "dirty.py").write_text(BODY, encoding="utf-8")
    run_git(git_repo, "add", "-A")

    change = gitctx.resolve_range(git_repo, "HEAD~1..HEAD")
    assert change.worktree is False
    files = {b["filename"] for b in gitctx.collect_blocks(change).blocks}
    assert files == {"a.py"}


def test_bare_sha_becomes_that_commit(git_repo, commit):
    commit("first", {"a.py": BODY})
    sha = commit("second", {"a.py": BODY.replace("line 5", "CHANGED 5")})
    change = gitctx.resolve_range(git_repo, sha)
    assert change.range == f"{sha}~1..{sha}"


def test_staged_only(git_repo, commit):
    commit("first", {"a.py": BODY})
    (git_repo / "a.py").write_text(BODY.replace("line 5", "STAGED 5"), encoding="utf-8")
    run_git(git_repo, "add", "a.py")
    (git_repo / "b.py").write_text(BODY, encoding="utf-8")  # unstaged, untracked

    change = gitctx.resolve_range(git_repo, None, staged=True)
    files = {b["filename"] for b in gitctx.collect_blocks(change).blocks}
    assert files == {"a.py"}


def test_committed_content_comes_from_the_commit_not_disk(git_repo, commit):
    """The correctness rule: a historical range must not read the drifted worktree.

    Reading disk here would attach the commit's line numbers to content that no
    longer matches — wrong in a way a reviewer would never notice.
    """
    commit("first", {"a.py": BODY})
    commit("second", {"a.py": BODY.replace("line 5", "COMMITTED 5")})
    # Drift the worktree *after* the commit we're going to review.
    (git_repo / "a.py").write_text(BODY.replace("line 5", "WORKTREE DRIFT"), encoding="utf-8")

    change = gitctx.resolve_range(git_repo, "HEAD~1..HEAD")
    blocks = gitctx.collect_blocks(change).blocks
    content = "\n".join(b["content"] for b in blocks)
    assert "COMMITTED 5" in content
    assert "WORKTREE DRIFT" not in content


def test_pure_deletion_hunk_is_skipped(git_repo, commit):
    commit("first", {"a.py": BODY, "gone.py": BODY})
    (git_repo / "gone.py").unlink()
    run_git(git_repo, "add", "-A")
    run_git(git_repo, "commit", "-q", "-m", "delete")

    change = gitctx.resolve_range(git_repo, "HEAD~1..HEAD")
    files = {b["filename"] for b in gitctx.collect_blocks(change).blocks}
    assert "gone.py" not in files


def test_nearby_hunks_merge(git_repo, commit):
    commit("first", {"a.py": BODY})
    edited = BODY.replace("line 10", "EDIT 10").replace("line 14", "EDIT 14")
    commit("second", {"a.py": edited})

    change = gitctx.resolve_range(git_repo, "HEAD~1..HEAD", include_worktree=False)
    blocks = gitctx.collect_blocks(change, context=1).blocks
    assert len(blocks) == 1, f"expected one merged block, got {blocks}"


def test_distant_hunks_stay_separate(git_repo, commit):
    # Newline-anchored so "line 5" doesn't also match inside "line 50".
    long_body = "\n".join(f"line {i}" for i in range(1, 121)) + "\n"
    commit("first", {"a.py": long_body})
    edited = long_body.replace("line 5\n", "EDIT 5\n").replace("line 100\n", "EDIT 100\n")
    commit("second", {"a.py": edited})

    change = gitctx.resolve_range(git_repo, "HEAD~1..HEAD", include_worktree=False)
    assert len(gitctx.collect_blocks(change, context=1).blocks) == 2


def test_generated_paths_are_excluded(git_repo, commit):
    commit("first", {"a.py": BODY})
    commit("second", {"a.py": BODY.replace("line 5", "X"), "package-lock.json": "{}\n"})

    change = gitctx.resolve_range(git_repo, "HEAD~1..HEAD", include_worktree=False)
    result = gitctx.collect_blocks(change)
    files = {b["filename"] for b in result.blocks}
    assert "package-lock.json" not in files
    assert "package-lock.json" in result.skipped


def test_include_filter(git_repo, commit):
    commit("first", {"a.py": BODY, "b.js": BODY})
    commit("second", {"a.py": BODY.replace("line 5", "X"),
                      "b.js": BODY.replace("line 5", "Y")})

    change = gitctx.resolve_range(git_repo, "HEAD~1..HEAD", include_worktree=False)
    files = {b["filename"] for b in gitctx.collect_blocks(change, includes=("*.py",)).blocks}
    assert files == {"a.py"}


def test_block_line_cap_clamps(git_repo, commit):
    big = "\n".join(f"line {i}" for i in range(1, 501)) + "\n"
    commit("first", {"a.py": "x\n"})
    commit("second", {"a.py": big})

    change = gitctx.resolve_range(git_repo, "HEAD~1..HEAD", include_worktree=False)
    blocks = gitctx.collect_blocks(change, max_block_lines=50).blocks
    for block in blocks:
        assert block["line_end"] - block["line_start"] + 1 <= 50


def test_total_cap_truncates_and_reports(git_repo, commit):
    files = {f"f{i}.py": BODY for i in range(10)}
    commit("first", {"seed.py": "x\n"})
    commit("second", files)

    change = gitctx.resolve_range(git_repo, "HEAD~1..HEAD", include_worktree=False)
    result = gitctx.collect_blocks(change, max_blocks=3)
    assert len(result.blocks) == 3
    assert result.truncated is True
    assert result.skipped


def test_language_detection():
    assert gitctx.language_for("a/b.py") == "python"
    assert gitctx.language_for("a/b.tsx") == "typescript"
    assert gitctx.language_for("a/b.unknown") == "text"


def test_auto_title_uses_the_newest_subject(git_repo, commit):
    commit("first", {"a.py": BODY})
    commit("make the thing work", {"a.py": BODY.replace("line 5", "X")})
    change = gitctx.resolve_range(git_repo, "HEAD~1..HEAD", include_worktree=False)
    assert gitctx.auto_title(change) == "Review: make the thing work"


def test_auto_description_notes_skipped_files(git_repo, commit):
    commit("first", {"a.py": BODY})
    commit("second", {"a.py": BODY.replace("line 5", "X"), "yarn.lock": "junk\n"})
    change = gitctx.resolve_range(git_repo, "HEAD~1..HEAD", include_worktree=False)
    result = gitctx.collect_blocks(change)
    description = gitctx.auto_description(change, result)
    assert "yarn.lock" in description
    assert "Diffstat" in description
