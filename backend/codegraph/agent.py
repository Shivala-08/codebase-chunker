"""Agentic modification (v1, Mode 3): graph-grounded task -> proposed diff -> PR.

Pipeline (reuses /chat's retrieval unchanged — TRD §8):
  1. retrieve relevant subgraph via GraphStore.get_subgraph_for_query
  2. inject nodes + relationships + source (same block format as /chat)
  3. NIM generates a unified diff against the *current* file contents
  4. sanitize + validate the diff, then apply+commit it inside a throwaway
     linked worktree on a new branch `codegraph/<slug>` — the user's own
     checkout is never touched (no branch switching, no dirty-tree risk)
  5. if the repo has a test suite, run it in a subprocess (with timeout)
     *in that worktree*, so tests exercise the proposed code — result is
     attached, shown to the user, never silently trusted
  6. open a PR (gh CLI, then GitHub API fallback) or leave the commit local

Guardrails (non-negotiable, TRD §8): never auto-merge; always a new branch;
tests run sandboxed via subprocess, not exec; a proposed diff is data, never
code we execute.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import nim


class AgentError(RuntimeError):
    """User-facing failure of an agent task."""


# ---------------------------------------------------------------------------
# Diff sanitization / validation
# ---------------------------------------------------------------------------

def sanitize_diff(raw: str) -> str:
    """Extract the diff payload, stripping markdown fences and stray prose.

    A model-returned diff is *data*: we cut everything that is not part of a
    git/patch-formatted diff rather than trusting the surrounding text.
    """
    text = raw.strip()
    # strip a single <think> trace if the model emits one
    while "<think>" in text and "</think>" in text:
        s = text.index("<think>")
        e = text.index("</think>") + len("</think>")
        text = (text[:s] + text[e:]).strip()

    # prefer a fenced block if present. Strip newlines only: a plain .strip()
    # would eat the final " " context line and leave the hunk one line short
    # of its @@ header — exactly the "corrupt patch" failure git apply reports.
    fence = re.search(r"```(?:diff|patch)?\s*\n(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).lstrip().rstrip("\n")
        if not text.strip():
            raise AgentError(
                "model proposed no changes (empty diff) — the task may already "
                "be satisfied in the current code, or needs more context to act on"
            )

    lines = text.splitlines()
    start = next(
        (i for i, ln in enumerate(lines)
         if ln.startswith(("diff --git ", "diff\n", "*** Begin patch", "--- ", "*** Update File"))
         or ln.startswith("@@ ")),
        None,
    )
    if start is None:
        raise AgentError("model returned no recognizable diff payload")

    body = lines[start:]
    # models sometimes append a closing fence with no opener; it is never
    # valid patch text, so strip trailing fence markers
    while body and body[-1].strip() in ("```", "```diff", "```patch", "~~~", "~~~diff"):
        body.pop()
    # git apply rejects bare-empty lines inside a hunk body; a context line for
    # an empty source line must be a single space, so restore ones we/models stripped
    fixed: list[str] = []
    in_hunk = False
    for ln in body:
        if ln.startswith("@@"):
            in_hunk = True
        elif in_hunk and ln == "":
            ln = " "
        fixed.append(ln)
    # rstrip newlines only — a plain rstrip() would eat the final " " context line
    return "\n".join(fixed).rstrip("\n") + "\n"


def parse_changed_files(diff_text: str) -> list[str]:
    """Repo-relative paths touched by a git diff. Rejects path escapes."""
    files: list[str] = []
    for m in re.finditer(r"^diff --git a/(\S+) b/(\S+)$", diff_text, re.MULTILINE):
        path = m.group(2)
        if path.startswith("/") or ".." in Path(path).parts:
            raise AgentError(f"diff touches path outside the repo: {path}")
        files.append(path)
    if not files:
        # `git diff` style without headers can still carry ---/+++ pairs
        for m in re.finditer(r"^\+\+\+ (?:b/)?(\S+)", diff_text, re.MULTILINE):
            p = m.group(1)
            if p == "/dev/null":
                continue
            if p.startswith("/") or ".." in Path(p).parts:
                raise AgentError(f"diff touches path outside the repo: {p}")
            files.append(p)
    if not files:
        raise AgentError("diff does not reference any files")
    return files


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class AgentResult:
    task: str
    diff: str
    files: list[str]
    branch: str
    validation: dict = field(default_factory=dict)   # apply-level checks
    tests: dict = field(default_factory=dict)        # sandboxed test run
    pr: dict = field(default_factory=dict)           # PR/local-commit outcome
    retrieval: dict = field(default_factory=dict)    # traceability
    seconds: float = 0.0

    def to_dict(self) -> dict:
        return {
            "task": self.task,
            "diff": self.diff,
            "files": self.files,
            "branch": self.branch,
            "validation": self.validation,
            "tests": self.tests,
            "pr": self.pr,
            "retrieval": self.retrieval,
            "seconds": round(self.seconds, 2),
        }


# ---------------------------------------------------------------------------
# Git plumbing (small, auditable, always branch-isolated)
# ---------------------------------------------------------------------------

def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, timeout=30, check=check,
    )


def _ensure_git_repo(repo: Path) -> None:
    r = _git(repo, "rev-parse", "--is-inside-work-tree", check=False)
    if r.returncode != 0 or r.stdout.strip() != "true":
        raise AgentError(f"not a git repository: {repo} — agent tasks need git for safe branching")


def _assert_clean_worktree(repo: Path) -> None:
    r = _git(repo, "status", "--porcelain")
    if r.stdout.strip():
        lines = r.stdout.strip().splitlines()
        raise AgentError(
            "worktree has uncommitted changes — commit or stash first so the "
            f"agent never mixes your work into its branch ({len(lines)} dirty path(s))"
        )


def _branch_slug(task: str) -> str:
    words = re.findall(r"[a-z0-9]+", task.lower())[:4]
    base = "-".join(words) or "task"
    return f"codegraph/{base}-{uuid.uuid4().hex[:6]}"


def _branch_exists(repo: Path, branch: str) -> bool:
    r = _git(repo, "rev-parse", "--verify", "--quiet", branch, check=False)
    return r.returncode == 0


def _current_branch(repo: Path) -> str:
    r = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    return r.stdout.strip()


def _git_toplevel(repo: Path) -> Path:
    r = _git(repo, "rev-parse", "--show-toplevel")
    return Path(r.stdout.strip()).resolve()


def _run_raw(cmd: list[str]) -> subprocess.CompletedProcess:
    """Run a fully-formed git command (for -c overrides that _git can't express)."""
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=True)


def _commit(repo: Path, task: str) -> str:
    """Commit staged changes; identity comes from repo config if set, else a
    per-invocation `-c` fallback so we never mutate the user's git config."""
    r = _git(repo, "config", "user.email", check=False)
    email = r.stdout.strip()
    cmd = ["git", "-C", str(repo)]
    if not email:
        cmd += ["-c", "user.email=codegraph@local", "-c", "user.name=CodeGraph Agent"]
    cmd += ["commit", "-m", f"[codegraph] {task}\n\nProposed by CodeGraph agent — review before merging."]
    _run_raw(cmd)
    r = _git(repo, "rev-parse", "--short", "HEAD")
    return r.stdout.strip()


def _apply_diff(repo: Path, diff_text: str) -> None:
    """`git apply --check` then apply+stage. Atomic: a rejected diff changes nothing."""
    proc = subprocess.run(
        ["git", "-C", str(repo), "apply", "--check", "--whitespace=nowarn", "-"],
        input=diff_text, capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().splitlines()
        raise AgentError(
            "diff does not apply cleanly against current file contents: "
            + (detail[-1] if detail else "git apply failed")
        )
    subprocess.run(
        ["git", "-C", str(repo), "apply", "--whitespace=nowarn", "-"],
        input=diff_text, capture_output=True, text=True, timeout=30, check=True,
    )
    _git(repo, "add", "-A")


def _resolve_diff_paths(diff_text: str, wt: Path, base: str) -> str:
    """Reconcile diff paths with the worktree layout.

    Graph node paths are relative to the *parse root*, but `git apply` resolves
    against the *git toplevel*. When the parsed root is a subdirectory of the
    repo (the common demo case), rewrite a/ b/ headers to carry the prefix.
    """
    files = parse_changed_files(diff_text)
    if all((wt / f).is_file() for f in files):
        return diff_text
    if base:
        prefixed = [f"{base}/{f}" for f in files]
        if all((wt / f).is_file() for f in prefixed):
            out: list[str] = []
            for ln in diff_text.splitlines():
                if ln.startswith("diff --git "):
                    m = re.match(r"diff --git a/(\S+) b/(\S+)$", ln)
                    out.append(f"diff --git a/{base}/{m.group(1)} b/{base}/{m.group(2)}" if m else ln)
                elif ln.startswith("--- ") and ln != "--- /dev/null":
                    p = ln[4:]
                    p = p[2:] if p.startswith(("a/", "b/")) else p
                    out.append(f"--- a/{base}/{p}")   # keep a/ b/ prefixes: git apply defaults to -p1
                elif ln.startswith("+++ ") and ln != "+++ /dev/null":
                    p = ln[4:]
                    p = p[2:] if p.startswith(("a/", "b/")) else p
                    out.append(f"+++ b/{base}/{p}")
                else:
                    out.append(ln)
            return "\n".join(out) + "\n"
    raise AgentError("diff references files that don't exist in the repo: " + ", ".join(files))


_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _find_block(file_lines: list[str], block: list[str]) -> int | None:
    """0-based position of `block` in `file_lines`.

    Three match levels: exact, trailing-whitespace-insensitive, then fully
    whitespace-insensitive (models frequently re-indent whole hunks; content
    order must still match exactly, so a false match needs identical text).
    """
    if not block:
        return None
    n = len(block)
    for i in range(len(file_lines) - n + 1):
        if file_lines[i:i + n] == block:
            return i
    stripped = [l.rstrip() for l in block]
    for i in range(len(file_lines) - n + 1):
        if [l.rstrip() for l in file_lines[i:i + n]] == stripped:
            return i
    bare = [l.strip() for l in block]
    for i in range(len(file_lines) - n + 1):
        if [l.strip() for l in file_lines[i:i + n]] == bare:
            return i
    return None


def _reanchor_hunks(diff_text: str, base: Path) -> str:
    """Rewrite each hunk's @@ start lines from the model's (unreliable) numbers
    to the position where the hunk's pre-image actually occurs in the file.

    Models emit plausible-but-wrong line numbers — or none at all (bare "@@") —
    sometimes drop the a/ b/ prefixes on ---/+++ headers, and order hunks by
    their imagined file layout instead of the real one. git apply rejects all
    of these (hunks must ascend within a file), so we normalize headers,
    anchor each hunk to where its pre-image actually occurs, and re-sort
    hunks by real position. A hunk whose pre-image can't be found at all is
    genuinely inapplicable and rejected with a clear error.
    """
    lines = diff_text.splitlines()
    out: list[str] = []
    i = 0
    cur_target: str | None = None
    # (real 0-based pos, hunk body, file lines), for the current file
    pending: list[tuple[int, list[str], list[str]]] = []

    def _counts(body: list[str]) -> tuple[int, int]:
        # old = context + deletions; new = context + additions — a deleted
        # line does not appear in the new file, so new != old + additions
        pre = [l[1:] for l in body if l[:1] in (" ", "-") and not l.startswith("\\")]
        post = [l[1:] for l in body if l[:1] in (" ", "+") and not l.startswith("\\")]
        return len(pre), len(post)

    def flush() -> None:
        nonlocal pending
        pending.sort(key=lambda t: t[0])
        # the new-file start of a hunk is its old start plus the net line
        # delta of every preceding hunk in the same file — model numbers are
        # never trusted for positioning
        delta = 0
        for idx, (pos, body, file_lines) in enumerate(pending):
            old_cnt, new_cnt = _counts(body)
            # git's canonical hunks carry up to 3 trailing context lines;
            # hunks that end flush on their last +/- line can fail to apply,
            # so extend from file reality (never into the next hunk)
            end = pos + old_cnt
            nxt = pending[idx + 1][0] if idx + 1 < len(pending) else None
            room = 3 if nxt is None else min(3, max(0, nxt - end))
            added = 0
            for off in range(room):
                if end + off < len(file_lines):
                    body.append(" " + file_lines[end + off])
                    added += 1
            out.append(f"@@ -{pos + 1},{old_cnt + added} +{pos + 1 + delta},{new_cnt + added} @@")
            out.extend(body)
            delta += new_cnt - old_cnt  # trailing context cancels out
        pending = []

    while i < len(lines):
        ln = lines[i]
        if ln.startswith("diff --git "):
            flush()
            m = re.match(r"diff --git a/(\S+) b/(\S+)$", ln)
            cur_target = m.group(2) if m else None
            out.append(ln)
            i += 1
            continue
        if ln.startswith("+++ "):
            flush()
            p = ln[4:].strip()
            if p != "/dev/null":
                cur_target = p[2:] if p.startswith("b/") else p
                if not p.startswith(("a/", "b/")):
                    ln = f"+++ b/{p}"  # repair missing prefix (git apply default is -p1)
            out.append(ln)
            i += 1
            continue
        if ln.startswith("--- "):
            flush()  # a new file's --- header ends the previous file's hunks
            p = ln[4:].strip()
            if p != "/dev/null" and not p.startswith(("a/", "b/")):
                ln = f"--- a/{p}"
            out.append(ln)
            i += 1
            continue
        if ln.startswith("@@") and cur_target:
            j = i + 1
            body: list[str] = []
            # stop at anything that is not a hunk-body line — the next hunk,
            # the next file's headers, a stray fence, or prose all end it
            while j < len(lines) and lines[j][:1] in (" ", "+", "-", "\\") and not lines[j].startswith(("--- ", "+++ ", "diff --git ")):
                raw_ln = lines[j]
                # models indent +/- markers ("   -    return x"); in a valid
                # patch the marker is always the first char, so strip slop
                if re.match(r"^ {2,}[-+]", raw_ln):
                    raw_ln = raw_ln.lstrip()
                body.append(raw_ln)
                j += 1
            pre = [l[1:] for l in body if l[:1] in (" ", "-") and not l.startswith("\\")]
            f = base / cur_target
            if not f.is_file():
                raise AgentError(f"hunk target does not exist: {cur_target}")
            file_lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
            if pre:
                pos = _find_block(file_lines, pre)
                if pos is None:
                    raise AgentError(
                        f"hunk for {cur_target} does not match current file contents "
                        "(context block not found) — model pre-image is stale or invented"
                    )
            else:
                # pure-insertion hunk: anchor at the model's claimed line
                m0 = _HUNK_RE.match(ln)
                if not m0:
                    raise AgentError(f"hunk for {cur_target} has no anchor (empty pre-image, no line numbers)")
                pos = max(0, min(int(m0.group(1)) - 1, len(file_lines)))
            # rebuild the hunk from file reality: context and deletion lines
            # are taken byte-exact from the matched position (models
            # mis-indent whole hunks), additions come from the model,
            # re-indented to the line they replace
            rebuilt: list[str] = []
            del_indents: list[str] = []
            k = pos
            for l in body:
                tag, content = l[0], l[1:]
                if tag == "\\":
                    rebuilt.append(l)
                    continue
                if tag == "+":
                    ind = ""
                    if k < len(file_lines):
                        fl = file_lines[k]
                        ind = fl[:len(fl) - len(fl.lstrip())] if fl.strip() else ""
                    if del_indents:
                        ind = del_indents.pop(0)
                    rebuilt.append("+" + ind + content.lstrip())
                    continue
                if k >= len(file_lines):
                    raise AgentError(f"hunk for {cur_target} runs past the end of the file")
                fl = file_lines[k]
                ind = fl[:len(fl) - len(fl.lstrip())] if fl.strip() else ""
                if tag == " ":
                    rebuilt.append(" " + fl)
                else:  # '-'
                    rebuilt.append("-" + fl)
                    del_indents.append(ind)
                k += 1
            pending.append((pos, rebuilt, file_lines))
            i = j
            continue
        out.append(ln)
        i += 1
    flush()
    return "\n".join(out) + "\n"


def _cleanup_worktree(repo: Path, wt: Path) -> None:
    """Best-effort removal of the throwaway linked worktree."""
    _git(repo, "worktree", "remove", "--force", str(wt), check=False)
    _git(repo, "worktree", "prune", check=False)
    shutil.rmtree(wt, ignore_errors=True)


# ---------------------------------------------------------------------------
# Sandboxed test execution
# ---------------------------------------------------------------------------

_TEST_MARKERS = ("test_", "_test.py", "tests/", "/tests", "conftest.py")
# sys.executable (not hardcoded python3) so tests run under the backend's venv,
# which is where pytest is actually installed.
_TEST_CMDS = [
    [sys.executable, "-m", "pytest", "-x", "-q"],
    [sys.executable, "-m", "pytest", "-q"],
    [sys.executable, "-m", "unittest", "discover", "-s", ".", "-p", "test_*.py"],
]


def _has_tests(repo: Path) -> bool:
    if (repo / "pytest.ini").is_file() or (repo / "pyproject.toml").is_file() and "pytest" in (repo / "pyproject.toml").read_text(errors="replace"):
        return True
    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = [d for d in dirnames if d not in (".git", ".venv", "venv", "__pycache__", "node_modules")]
        if any(any(mk in f for mk in _TEST_MARKERS) for f in filenames):
            return True
    return False


def _run_tests_sandboxed(repo: Path, timeout: int = 120) -> dict:
    """Run the test suite in a subprocess with a hard timeout. Never exec()."""
    started = time.time()
    for cmd in _TEST_CMDS:
        try:
            proc = subprocess.run(
                cmd, cwd=str(repo), capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return {"ran": True, "ok": False, "error": f"timed out after {timeout}s"}
        except FileNotFoundError:
            continue  # runner not installed; try the next one
        if proc.returncode != 0 and "No module named pytest" in (proc.stderr or ""):
            continue
        tail = "\n".join((proc.stdout or "").strip().splitlines()[-25:])
        return {
            "ran": True,
            "ok": proc.returncode == 0,
            "cmd": " ".join(cmd),
            "seconds": round(time.time() - started, 1),
            "output_tail": tail or (proc.stderr or "").strip()[-800:],
        }
    return {"ran": False, "ok": None, "note": "no runnable test runner found (pytest/unittest)"}


# ---------------------------------------------------------------------------
# PR creation — gh CLI first, raw GitHub API fallback, local commit otherwise
# ---------------------------------------------------------------------------

def _pr_via_gh(repo: Path, branch: str, title: str, body: str) -> dict | None:
    if shutil.which("gh") is None:
        return None
    proc = subprocess.run(
        ["gh", "pr", "create", "--head", branch, "--title", title, "--body", body,
         "--base", _default_branch(repo)],
        cwd=str(repo), capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        return None
    url = next((ln.strip() for ln in proc.stdout.splitlines() if ln.startswith("http")), "")
    return {"opened": bool(url), "url": url or proc.stdout.strip(), "via": "gh-cli"}


def _default_branch(repo: Path) -> str:
    r = _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", check=False)
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip().rsplit("/", 1)[-1] or "main"
    return "main"


def _pr_via_api(repo: Path, branch: str, title: str, body: str) -> dict | None:
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        return None
    remote = _git(repo, "remote", "get-url", "origin", check=False)
    if remote.returncode != 0:
        return None
    m = re.search(r"github\.com[:/](.+?)/(.+?)(?:\.git)?/?$", remote.stdout.strip())
    if not m:
        return None
    owner, name = m.group(1), m.group(2)
    payload = json.dumps({"title": title, "head": branch, "base": _default_branch(repo), "body": body}).encode()
    req = urllib.request.Request(
        f"https://api.github.com/repos/{owner}/{name}/pulls",
        data=payload, method="POST",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            return {"opened": True, "url": data.get("html_url", ""), "via": "github-api"}
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        return {"opened": False, "error": f"GitHub API {e.code}: {detail}", "via": "github-api"}


def _open_pr(repo: Path, branch: str, task: str, tests: dict) -> dict:
    title = f"[codegraph] {task[:80]}"
    test_line = "✅ tests passed" if tests.get("ran") and tests.get("ok") else (
        f"❌ tests failed" if tests.get("ran") else "⚠️ tests not run"
    )
    body = (
        f"Proposed change from CodeGraph agent.\n\n**Task**: {task}\n\n"
        f"**Sandboxed tests**: {test_line}\n\n"
        "⚠️ Human review required before merge — this branch is never merged automatically."
    )
    for attempt in (_pr_via_gh, _pr_via_api):
        try:
            res = attempt(repo, branch, title, body)
        except Exception:
            res = None
        if res is not None:
            return res
    return {"opened": False, "local_commit": True,
            "note": "no PR remote/auth — commit left on the branch for local review"}


# ---------------------------------------------------------------------------
# Prompt building — same retrieval + injection format as /chat
# ---------------------------------------------------------------------------

def _build_task_prompt(task: str, node_blocks: str, relationships: str) -> tuple[str, str]:
    system = """You are CodeGraph's modification agent. You receive relevant nodes from a
dependency graph (with source) and a task. Produce a UNIFIED DIFF (git format) that
implements the task.

Rules:
1. Change ONLY what the task requires. Prefer the narrowest correct edit.
2. Base hunks on the exact source shown; do not invent surrounding code.
3. If the task needs code not shown in any node, STOP and answer with
   NEEDS_CONTEXT: <what is missing> instead of guessing.
4. Output ONLY the diff inside one ```diff fence. No prose before or after.
5. Every hunk MUST contain at least one '-' or '+' line — never emit a
   context-only hunk. When the task renames a function/class, the hunk that
   renames its def/class line is mandatory, not just the call sites.
6. Use standard git diff format: '--- a/<path>' / '+++ b/<path>' headers and
   '@@ -<start>,<count> +<start>,<count> @@' hunk headers with line numbers."""
    user = f"""TASK: {task}

RELEVANT NODES (current source):
{node_blocks}

RELATIONSHIPS:
{relationships if relationships else "(none among retrieved nodes)"}

Respond with the unified diff now."""
    return system, user


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------

def run_task(repo_path: str, task: str, store, max_nodes: int = 12) -> AgentResult:
    t0 = time.time()
    repo = Path(repo_path).resolve()
    if not repo.is_dir():
        raise AgentError(f"not a directory: {repo}")
    _ensure_git_repo(repo)
    git_root = _git_toplevel(repo)
    _assert_clean_worktree(repo)
    original_branch = _current_branch(repo)

    # 1. retrieval — the exact pipeline /chat uses (TRD §8: "reused as-is")
    node_ids = store.get_subgraph_for_query(task, max_nodes=max_nodes, expand_hops=2)
    if not node_ids:
        raise AgentError("no graph nodes matched that task — parse the right repo?")

    blocks: list[str] = []
    for nid in node_ids:
        n = store.get_node(nid)
        if n is None:
            continue
        src = _read_source(n["file"], n["line_start"], n["line_end"], str(store.repo))
        src_lines = src.splitlines()
        if len(src_lines) > 40:
            src = "\n".join(src_lines[:40]) + f"\n... (+{len(src_lines) - 40} lines)"
        rel = store.get_neighbors(nid, depth=1)
        blocks.append(
            f"- {nid} ({n['type']}) — neighbors: {', '.join(rel[:6]) or '(none)'}\n"
            f"  {(n.get('docstring') or '')[:200]}\n"
            f"  ```python\n  {src}\n  ```"
        )
    relationships = "\n".join(store.describe_edges(node_ids))
    system, user = _build_task_prompt(task, "\n".join(blocks), relationships)

    # 2. generation (strong model — diff planning benefits from reasoning, TRD §8)
    raw = nim.complete_raw(system=system, user=user, model=nim.MODEL_CHAT)
    if os.environ.get("CODEGRAPH_DEBUG"):
        with open("/tmp/codegraph-agent-raw.txt", "w") as f:
            f.write(raw)  # diagnostic: inspect what the model actually returned

    if "NEEDS_CONTEXT:" in raw and "diff --git" not in raw and "```diff" not in raw:
        raise AgentError("model asked for more context: " + raw.strip()[:400])
    diff_text = sanitize_diff(raw)
    files = parse_changed_files(diff_text)

    validation: dict = {"apply_ok": False, "files": files}

    # 3. branch isolation in a throwaway linked worktree — the user's own
    #    checkout is never touched (no branch switching, no worktree mutation)
    branch = _branch_slug(task)
    if _branch_exists(repo, branch):
        raise AgentError(f"branch {branch} unexpectedly exists; refusing to reuse")
    wt = Path(tempfile.mkdtemp(prefix="codegraph-wt-"))
    try:
        _git(repo, "worktree", "add", "-b", branch, str(wt))
        # graph paths are parse-root-relative; the worktree is toplevel-rooted
        base = os.path.relpath(repo, git_root)
        diff_text = _resolve_diff_paths(diff_text, wt, "" if base == "." else base)
        # never trust model line numbers — anchor hunks to file reality
        diff_text = _reanchor_hunks(diff_text, wt)
        _apply_diff(wt, diff_text)
        validation["apply_ok"] = True
        commit_sha = _commit(wt, task)
    except AgentError:
        _cleanup_worktree(repo, wt)
        _git(repo, "branch", "-D", branch, check=False)  # nothing was committed; don't litter
        raise
    except Exception as e:
        _cleanup_worktree(repo, wt)
        _git(repo, "branch", "-D", branch, check=False)
        raise AgentError(f"failed to apply/commit diff: {e}") from e

    try:
        # 4. sandboxed tests *in the worktree* — they exercise the proposed code,
        #    not the untouched original (subprocess + timeout, shown not trusted)
        tests = _run_tests_sandboxed(wt) if _has_tests(repo) else {
            "ran": False, "ok": None, "note": "no test suite detected in repo",
        }

        # 5. PR — gh CLI → GitHub API → local commit; never a merge
        pr = _open_pr(wt, branch, task, tests)
    finally:
        _cleanup_worktree(repo, wt)

    return AgentResult(
        task=task,
        diff=diff_text,
        files=files,
        branch=branch,
        validation=validation,
        tests=tests,
        pr=pr,
        retrieval={"used_nodes": node_ids, "original_branch": original_branch,
                   "commit": commit_sha},
        seconds=time.time() - t0,
    )


def _read_source(file: str, line_start: int, line_end: int, repo_root: str) -> str:
    p = Path(repo_root) / file
    if p.exists():
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[line_start - 1:line_end])
    return "(source unavailable)"
