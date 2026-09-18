# CodeGraph — v3 PRD + TRD

> Builds on `Shivala-08/codebase-chunker`, where v0 (graph + chat), v1 (agent/mode 3), and v2 (living docs/mode 4) are already implemented — endpoints, retrieval, guardrails, and frontend all exist. This version has two tracks: **close the gaps in what's already built** (nothing left unvalidated), then **extend scope** (multi-language, CI automation).

---

## PART 1 — PRD

### 1. Where we actually are
Confirmed against the repo: parser, graph store, all 9 endpoints, rate limiting, caching, agent guardrails (worktree isolation, `git apply --check`, sandboxed tests, never-auto-merge), and doc generation are all implemented and match the v0–v2 spec closely. This is further along than "next feature to build" — it's "next thing to validate and extend."

### 2. Goal (v3)
**Track A — Validation (do first, nothing else matters until this is done)**: prove the thing that's already built actually works, with a real fixture and real tests, so every claim in the README is verifiable rather than assumed.

**Track B — Extension (do after Track A)**: multi-language support (JS/TS) and CI-driven doc regeneration — the two items already flagged as "deferred to v3" in the v0–v2 doc.

### 3. Non-goals (v3)
- No embeddings-based retrieval yet — Track A's evaluation harness is what decides whether this is even needed. Building it before measuring keyword+hop's actual failure rate is solving a problem you haven't confirmed exists.
- No persistent graph DB — still rebuild-per-run, fine at current scale.
- No cloud hosting / multi-tenancy — still a local tool, per the standing decision.
- No new output modes beyond the 4 already scoped.

### 4. Target user
Same as before (you, then anyone onboarding onto an unfamiliar repo) — but v3 is also implicitly "future-you six months from now," since without tests and a fixture, nobody (including you) can tell if a change broke something.

### 5. Core user stories

**Track A — Validation**
| # | Story | Priority |
|---|---|---|
| 1 | As a developer, I can clone the repo and immediately parse a working sample repo without hunting for test data | P0 |
| 2 | As a maintainer, I have automated tests covering parser correctness, retrieval quality, and agent guardrails, so a change that breaks something fails CI instead of failing silently in a demo | P0 |
| 3 | As a maintainer, I have a small written eval set (10–15 real questions against the sample repo) with expected-correct node citations, so I can measure retrieval quality *before* deciding whether embeddings are worth the complexity | P0 |
| 4 | As a user, agent-mode guardrails (branch isolation, apply-check, sandboxed tests) are exercised by an actual test, not just described in the README | P1 |

**Track B — Extension**
| # | Story | Priority |
|---|---|---|
| 5 | As a user, I can point the parser at a JS/TS repo and get the same graph + chat + explain experience as Python | P1 |
| 6 | As a maintainer, doc pages regenerate automatically on push instead of requiring a manual `POST /docs/generate` | P2 |

### 6. Success criteria for v3
- `sample-repo/shop` exists, is committed, and the README's documented workflow runs against it without edits
- `pytest` passes with meaningful coverage on parser, retrieval, and at least one agent guardrail test (e.g. "dirty worktree refuses to run")
- Retrieval eval set exists with a measured accuracy number (e.g. "12/15 questions cited the correct node") — this number, not a guess, decides if embeddings work starts
- A second language parses successfully on a small real repo in that language
- Doc regeneration can run from a git hook without manual intervention

### 7. Risks
- Writing a good eval set is easy to do badly (vague questions with multiple "correct" answers make the accuracy number meaningless) — keep questions specific enough to have one clearly-right cited node
- Adding a second language risks the parser abstraction leaking Python-specific assumptions (e.g. `imports_symbol` edge type may not map cleanly to JS's `import {x} from 'y'` syntax) — expect some refactor of `parser.py`'s edge extraction, not a clean copy-paste

---

## PART 2 — TRD

### 1. Architecture delta from v0–v2
No new services. This phase adds: one fixture repo, one test suite, one eval harness/dataset, one new tree-sitter grammar + parser dispatch, and one CI config file. Nothing in `main.py`'s endpoint surface changes.

### 2. Components

#### 2.1 Sample repo fixture — `sample-repo/shop`
A small, deliberately-designed Python app (6–10 files) that exercises what the parser and retrieval need to prove:
- Cross-file imports (so `imports` edges get tested)
- A call chain 3+ hops deep (e.g. `router → service → repository → db`), so `expand_hops=2` retrieval has something real to walk
- At least one class with methods (tests `contains` edges)
- At least one docstring-free function (tests the "explain works even without docstrings" path)
- A deliberately ambiguous name collision (two functions named similarly in different files) — this is the actual stress test for keyword-match retrieval's weak point

Suggested shape: a minimal e-commerce backend — `models.py`, `db.py`, `repository.py`, `service.py`, `router.py`, `auth.py`, `utils.py`. Small enough to read end-to-end in 5 minutes, large enough to have real structure.

#### 2.2 Test suite — `backend/tests/`
- `test_parser.py`: parse the fixture, assert expected node count, assert specific known edges exist (e.g. `service.py::create_order` calls `repository.py::save_order`)
- `test_graph_store.py`: `get_subgraph_for_query` returns expected nodes for a few hand-picked queries; `get_neighbors` depth-limiting works
- `test_agent_guardrails.py`: dirty worktree is refused; diff that fails `git apply --check` is rejected; branch created is never the checked-out branch
- Run via `pytest` (already in `requirements.txt` — just unused so far)

#### 2.3 Retrieval eval harness — `backend/eval/questions.json` + `backend/eval/run_eval.py`
- 10–15 hand-written questions against the fixture repo, each with the node ID(s) that *should* be cited
  - e.g. `{"question": "how does an order get saved to the database?", "expected_nodes": ["service.py::create_order", "repository.py::save_order"]}`
- `run_eval.py` calls `/chat` for each, checks if `cited_nodes` overlaps `expected_nodes`, prints a score
- **This number is the actual decision gate for embeddings work** — don't start that until this script has run and shown a real gap

#### 2.4 Multi-language support — JS/TS
- Add `tree-sitter-javascript` (and/or `tree-sitter-typescript`) to `requirements.txt`
- `parser.py` needs a per-language dispatch: file extension → grammar + a language-specific edge-extraction function, since import/call syntax differs enough that one extraction function won't cleanly cover both languages
- Reuse the same `graph.json` schema — no changes needed downstream (graph store, retrieval, chat, agent, docs all stay language-agnostic once nodes/edges exist)
- Start with import + top-level function/class extraction only; defer JS's messier patterns (arrow functions assigned to variables, `module.exports` patterns, dynamic `require()`) to a follow-up pass rather than trying to handle everything at once

#### 2.5 CI-driven doc regeneration
- Simple GitHub Actions workflow: on push to `main`, run `POST /docs/generate` against a running instance (or run the doc generator as a standalone script, not through the HTTP layer, to avoid needing a live server in CI)
- Recommend extracting `docgen.py`'s core logic into a callable that doesn't require the FastAPI app running, if it isn't already — cleaner for CI than spinning up a server

### 3. Build order
| Session | Deliverable | Definition of done |
|---|---|---|
| 1 | `sample-repo/shop` fixture committed | README's documented commands run against it with zero edits |
| 2 | `test_parser.py` + `test_graph_store.py` passing | `pytest` green, covers the fixture's known structure |
| 3 | Eval harness + question set, run once | Real accuracy number in hand — decides next step, not a vibe |
| 4 | `test_agent_guardrails.py` | Guardrails proven by test, not just by README description |
| 5 | JS/TS grammar + parser dispatch | Parses a small real JS repo, produces a sane graph |
| 6 | CI doc regeneration | Push to main regenerates docs without manual `curl` |

### 4. Deferred beyond v3
- Embeddings-based retrieval — contingent entirely on §2.3's measured result
- Persistent graph DB
- Cloud hosting / multi-tenancy
- Additional languages beyond one JS/TS pass (Go, Rust, etc.)
