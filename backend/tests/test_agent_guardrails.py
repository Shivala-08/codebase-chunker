"""Agent guardrail tests (PRD v3 §2.2 test_agent_guardrails.py).

The PRD's rule: guardrails are "exercised by an actual test, not just
described in the README". These tests spin up throwaway git repos in tmp and
exercise the real code paths — dirty-worktree refusal, git apply --check,
branch isolation via linked worktrees, sandboxed test runs — with the NIM
model call monkeypatched out, so nothing here touches the network or the
user's own checkout.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from codegraph import agent

APP_PY = '''def greet(name):
    return "hello " + name
'''

DOCSTRING_DIFF = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -900,2 +900,4 @@
 def greet(name):
-    return "hello " + name
+    \"\"\"Say hello to someone.\"\"\"
+    return "hello " + name
"""


# --------------------------------------------------------------- helpers --

def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, timeout=30, check=check,
    )


def _commit_all(repo: Path, msg: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", msg)


@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    """A minimal committed git repo (branch 'main'), independent of git defaults."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    (repo / "app.py").write_text(APP_PY, encoding="utf-8")
    (repo / "test_app.py").write_text(
        "from app import greet\n\n\ndef test_greet():\n    assert greet('world') == 'hello world'\n",
        encoding="utf-8",
    )
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _commit_all(repo, "initial")
    if _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() != "main":
        _git(repo, "checkout", "-b", "main")
    return repo


@pytest.fixture()
def no_model_calls(monkeypatch):
    """Any NIM call inside a guardrail test is a guardrail failure."""
    import codegraph.nim as nim

    def boom(*a, **k):
        raise AssertionError("model must not be called before guardrails pass")

    monkeypatch.setattr(nim, "complete_raw", boom)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)


# ------------------------------------------------- pre-flight guardrails --

def test_run_task_refuses_non_git_directory(shop_store, tmp_path):
    bare = tmp_path / "not-a-repo"
    bare.mkdir()
    with pytest.raises(agent.AgentError, match="not a git repository"):
        agent.run_task(str(bare), "anything", shop_store)


def test_run_task_refuses_missing_directory(shop_store):
    with pytest.raises(agent.AgentError, match="not a directory"):
        agent.run_task("/definitely/not/a/real/dir/codegraph", "anything", shop_store)


def test_run_task_refuses_dirty_worktree(shop_store, git_repo, no_model_calls):
    (git_repo / "app.py").write_text(APP_PY + "# uncommitted edit\n", encoding="utf-8")
    with pytest.raises(agent.AgentError, match="uncommitted changes"):
        agent.run_task(str(git_repo), "add a docstring to greet", shop_store)


def test_run_task_refuses_untracked_files(shop_store, git_repo, no_model_calls):
    (git_repo / "stray.py").write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(agent.AgentError, match="uncommitted changes"):
        agent.run_task(str(git_repo), "add a docstring to greet", shop_store)


def test_dirty_check_fires_before_any_model_call(shop_store, git_repo, no_model_calls):
    # no_model_calls monkeypatches complete_raw to explode; the dirty-repo
    # error below proves retrieval/generation never started
    (git_repo / "app.py").write_text(APP_PY + "# dirty\n", encoding="utf-8")
    with pytest.raises(agent.AgentError):
        agent.run_task(str(git_repo), "add a docstring to greet", shop_store)


# ------------------------------------------------------ diff validation --

def test_apply_diff_rejects_inapplicable_diff(git_repo):
    bad = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1,2 +1,3 @@
-context line that exists nowhere in app.py
+replacement line
"""
    before = (git_repo / "app.py").read_text(encoding="utf-8")
    with pytest.raises(agent.AgentError, match="does not apply cleanly"):
        agent._apply_diff(git_repo, bad)
    assert (git_repo / "app.py").read_text(encoding="utf-8") == before  # atomic


def test_apply_diff_applies_and_stages_clean_diff(git_repo):
    good = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1,2 +1,3 @@
 def greet(name):
+    note = "hi"
     return "hello " + name
"""
    agent._apply_diff(git_repo, good)
    assert 'note = "hi"' in (git_repo / "app.py").read_text(encoding="utf-8")
    status = _git(git_repo, "status", "--porcelain").stdout
    assert "M  app.py" in status  # staged, not just modified


def test_parse_changed_files_rejects_path_escapes():
    escape = "diff --git a/../../etc/passwd b/../../etc/passwd\n"
    with pytest.raises(agent.AgentError, match="outside the repo"):
        agent.parse_changed_files(escape)
    absolute = "diff --git a/etc/passwd b//absolute/path\n"
    with pytest.raises(agent.AgentError, match="outside the repo"):
        agent.parse_changed_files(absolute)


def test_parse_changed_files_lists_touched_files():
    diff = (
        "diff --git a/app.py b/app.py\n"
        "diff --git a/util.py b/util.py\n"
    )
    assert agent.parse_changed_files(diff) == ["app.py", "util.py"]


def test_sanitize_diff_extracts_fenced_payload():
    raw = (
        "Sure! Here is the change:\n\n```diff\n"
        + DOCSTRING_DIFF.strip()
        + "\n```\nLet me know if you need anything else.\n"
    )
    out = agent.sanitize_diff(raw)
    assert out.startswith("diff --git a/app.py")
    assert "Here is the change" not in out
    assert "```" not in out


def test_sanitize_diff_rejects_prose_without_diff():
    with pytest.raises(agent.AgentError, match="no recognizable diff"):
        agent.sanitize_diff("I cannot help with that.")


# ---------------------------------------------------- branch isolation --

def test_branch_slug_is_namespaced_and_unique():
    b1 = agent._branch_slug("add a docstring to greet")
    b2 = agent._branch_slug("add a docstring to greet")
    assert b1.startswith("codegraph/")
    assert b2.startswith("codegraph/")
    assert b1 != b2  # uuid suffix: never collide with an existing branch


def test_worktree_branch_never_touches_user_checkout(git_repo):
    branch = agent._branch_slug("isolation check")
    import tempfile

    wt = Path(tempfile.mkdtemp(prefix="codegraph-test-wt-"))
    try:
        agent._git(git_repo, "worktree", "add", "-b", branch, str(wt))
        # user's checkout stays on main, clean
        assert agent._current_branch(git_repo) == "main"
        assert not _git(git_repo, "status", "--porcelain").stdout.strip()
        # the worktree is on the new agent branch
        assert agent._current_branch(wt) == branch
        assert branch not in ("main",) and not branch == agent._current_branch(git_repo)
    finally:
        agent._cleanup_worktree(git_repo, wt)
        _git(git_repo, "branch", "-D", branch, check=False)
    assert agent._branch_exists(git_repo, branch) is False


def test_cleanup_removes_worktree_registration(git_repo):
    import tempfile

    branch = agent._branch_slug("cleanup check")
    wt = Path(tempfile.mkdtemp(prefix="codegraph-test-wt-"))
    agent._git(git_repo, "worktree", "add", "-b", branch, str(wt))
    agent._cleanup_worktree(git_repo, wt)
    listing = _git(git_repo, "worktree", "list").stdout
    assert str(wt) not in listing


# --------------------------------------------- sandboxed test execution --

def test_sandboxed_tests_pass_and_fail_are_reported(git_repo):
    res = agent._run_tests_sandboxed(git_repo, timeout=60)
    assert res["ran"] is True
    assert res["ok"] is True
    assert "pytest" in res.get("cmd", "")

    (git_repo / "test_app.py").write_text(
        "def test_fails():\n    assert False\n", encoding="utf-8"
    )
    res = agent._run_tests_sandboxed(git_repo, timeout=60)
    assert res["ran"] is True and res["ok"] is False


def test_sandboxed_tests_timeout_is_enforced(tmp_path):
    (tmp_path / "test_slow.py").write_text(
        "import time\n\n\ndef test_slow():\n    time.sleep(60)\n", encoding="utf-8"
    )
    res = agent._run_tests_sandboxed(tmp_path, timeout=2)
    assert res["ran"] is True and res["ok"] is False
    assert "timed out" in res.get("error", "")


def test_no_runner_is_reported_not_raised(monkeypatch, tmp_path):
    def raise_fnf(*a, **k):
        raise FileNotFoundError("runner missing")

    monkeypatch.setattr(subprocess, "run", raise_fnf)
    res = agent._run_tests_sandboxed(tmp_path)
    assert res["ran"] is False


def test_has_tests_detection(git_repo):
    assert agent._has_tests(git_repo) is True
    (git_repo / "test_app.py").unlink()
    assert agent._has_tests(git_repo) is False


# -------------------------------------------- full happy path (no NIM) --

def test_full_task_flow_branch_isolated_commit_and_sandboxed_tests(
    shop_store, git_repo, monkeypatch
):
    """End-to-end run_task with the model stubbed: proves the guardrail chain —
    new branch in a throwaway worktree, user checkout untouched, diff applied
    and committed on the branch, tests run sandboxed, worktree cleaned up."""
    import codegraph.nim as nim

    monkeypatch.setattr(nim, "complete_raw", lambda *a, **k: f"```diff\n{DOCSTRING_DIFF.strip()}\n```")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    result = agent.run_task(str(git_repo), "add a docstring to greet", shop_store)

    assert result.validation["apply_ok"] is True
    assert result.files == ["app.py"]
    assert result.branch.startswith("codegraph/")
    assert '"""Say hello to someone."""' in result.diff

    # sandboxed tests ran in the worktree and passed
    assert result.tests["ran"] is True and result.tests["ok"] is True

    # user's checkout: still on main, still clean, still has the ORIGINAL app.py
    assert agent._current_branch(git_repo) == "main"
    assert not _git(git_repo, "status", "--porcelain").stdout.strip()
    assert (git_repo / "app.py").read_text(encoding="utf-8") == APP_PY

    # main got no new commit; the proposal lives on the agent branch only
    main_commits = _git(git_repo, "rev-list", "--count", "main").stdout.strip()
    assert main_commits == "1"
    assert agent._branch_exists(git_repo, result.branch) is True

    # worktree cleaned up even though the branch survives for review
    assert str(result.branch)  # branch kept for local review
    listing = _git(git_repo, "worktree", "list").stdout
    assert "codegraph-wt-" not in listing

    # PR fallback: no remote/auth -> explicit local-commit outcome, never a merge
    assert result.pr["opened"] is False
    assert result.pr.get("local_commit") is True
