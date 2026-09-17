# CodeGraph (working name) — PRD + TRD (v0)

> AI tool that parses a codebase into a dependency graph, visualizes it, and answers questions grounded in that graph — instead of raw-text RAG or blind grepping.

---

## PART 1 — PRODUCT REQUIREMENTS DOCUMENT (PRD)

### 1. Problem
New devs (and AI coding assistants) waste time building a mental model of a codebase from scratch. Existing AI coding tools do RAG over text chunks — they retrieve *similar-sounding* code, not *structurally relevant* code. Nobody has a fast way to answer "what touches this?" or "what happens if I change this?" without manually tracing imports and calls.

### 2. Goal (v0)
Point the tool at a repo → get:
1. An interactive visual graph of the codebase (files, functions, imports, call relationships)
2. A chat interface that answers questions by walking that graph, not by dumping the repo into a prompt

### 3. Non-goals (v0)
- No automated code modification / PR generation (that's v1, "mode 3")
- No auto-synced living documentation site (v1, "mode 4")
- No multi-language support at launch — one language only
- No auth, multi-tenancy, or hosted SaaS — local/single-user tool for v0

### 4. Target user (v0)
You, first. Then: a solo dev or small team onboarding onto an unfamiliar repo (their own old project, an open-source repo, a new job's codebase).

### 5. Core user stories
| # | Story | Priority |
|---|---|---|
| 1 | As a user, I can point the tool at a local repo path and get a graph built | P0 |
| 2 | As a user, I can see the graph rendered interactively (pan/zoom/click) | P0 |
| 3 | As a user, I can click a node and see a plain-English explanation of what it does | P0 |
| 4 | As a user, I can ask a free-text question ("how does auth work here?") and get an answer grounded in relevant graph nodes | P0 |
| 5 | As a user, I can see *which* nodes the AI used to answer (traceability) | P1 |
| 6 | As a user, I can filter the graph (by file, by type: function/class/module) | P1 |
| 7 | As a user, the graph highlights the answer-relevant subgraph when I ask a question | P1 |

### 6. Success criteria for v0
- Runs end-to-end on a real repo (start: 500–3000 LOC, one language) in under 2 minutes
- Chat answers are noticeably more accurate/specific than pasting the repo into a raw LLM chat, on at least 5 test questions you write yourself
- You can demo it live without it breaking

### 7. Out of scope risks to watch
- Tree-sitter grammar quirks per language (start with Python — cleanest grammar)
- Graph size blowup on large repos (v0 caps at a few thousand nodes; no pagination/clustering yet)
- LLM context limits when a question touches too many nodes (need a "top-k relevant nodes" cutoff)

---

## PART 2 — TECHNICAL REQUIREMENTS DOCUMENT (TRD)

### 1. Architecture overview

```
┌─────────────┐     ┌──────────────┐     ┌─────────────┐     ┌──────────────┐
│  Repo (fs)  │ --> │ Parser        │ --> │ Graph Store │ --> │ Graph JSON    │
│             │     │ (tree-sitter) │     │ (networkx)  │     │ (served)      │
└─────────────┘     └──────────────┘     └─────────────┘     └──────┬───────┘
                                                                     │
                                                 ┌───────────────────┴───────────────────┐
                                                 │                                        │
                                          ┌──────▼──────┐                        ┌────────▼────────┐
                                          │ Frontend      │                        │ Chat endpoint    │
                                          │ (force-graph  │                        │ (FastAPI + graph │
                                          │  viz, React)  │                        │  retrieval +     │
                                          │               │                        │  NIM API)        │
                                          └───────────────┘                        └──────────────────┘
```

### 2. Components

#### 2.1 Parser service
- **Input**: repo root path
- **Tool**: `tree-sitter` with the Python grammar (`tree-sitter-python`)
- **Process**:
  1. Walk repo, collect `.py` files (respect `.gitignore`)
  2. Parse each file to an AST
  3. Extract nodes: modules, classes, functions (with file path, line range, docstring if present)
  4. Extract edges:
     - `imports`: file → file (or file → module)
     - `calls`: function → function (best-effort static resolution — same-file first, then import-resolved cross-file)
     - `contains`: class → method, module → function
- **Output**: `graph.json`
  ```json
  {
    "nodes": [
      {"id": "auth.py::login", "type": "function", "file": "auth.py", "name": "login", "line_start": 12, "line_end": 30, "docstring": "..."}
    ],
    "edges": [
      {"from": "auth.py::login", "to": "db.py::get_user", "type": "calls"}
    ]
  }
  ```

#### 2.2 Graph store
- `networkx.DiGraph` in memory for v0 (no persistent DB yet — rebuild on each run)
- Exposes query functions the chat layer needs:
  - `get_neighbors(node_id, depth=1)` — direct callers/callees/imports
  - `get_subgraph_for_query(keywords)` — naive first pass: substring/name match on node names + docstrings to seed relevant nodes, then expand via neighbors
  - Later upgrade path: embed node summaries, do semantic search to seed instead of substring match

#### 2.3 Backend (FastAPI)
Endpoints:
| Method | Route | Purpose |
|---|---|---|
| POST | `/parse` | Trigger parse of a repo path, returns graph.json |
| GET | `/graph` | Return current graph.json |
| GET | `/node/{id}/explain` | LLM-generated explanation for one node (uses node code + immediate neighbors as context) |
| POST | `/chat` | `{question: str}` → retrieves relevant subgraph, injects into prompt, returns `{answer, used_nodes: [...]}` |

#### 2.4 AI layer — retrieval, injection, generation (the actual novelty)

Split into three concerns, each with a v0 (ship now) and v1 (only if v0 proves insufficient):

**a) Retrieval — which graph nodes to pull in**
- v0: score nodes by keyword/name match against the question, against `name`, `file`, `docstring`. Cheap, no extra infra, works well since function names are usually descriptive.
- Expand seeds by 1–2 graph hops (direct callers + callees + same-file siblings) — this is the core differentiator vs. text-chunk RAG: retrieval follows *structure*, not similarity.
- Cap at top-N nodes (start N=15–20) by relevance heuristic (match strength, centrality)
- v1 upgrade (only if v0 visibly misses things in testing): embed `name + docstring + signature` per node at parse time, swap keyword match for cosine similarity search

**b) Injection — how context is structured in the prompt**
- Never dump raw file contents. Structure the prompt so the LLM sees the graph, not just code:
  ```
  Relevant nodes:
  - auth.py::login (function) — calls: db.py::get_user, utils.py::hash_pw
    """docstring if present"""
    <source, ~20 lines>
  - db.py::get_user (function) — called by: auth.py::login
    <source>

  Relationships:
  login -> get_user (calls)
  login -> hash_pw (calls)
  ```
- This lets the model answer wiring questions ("what breaks if I change get_user") without a separate prompt path.

**c) Generation — constraints on the actual model call**
- System prompt instructs the model to answer only from provided nodes/relationships, and to explicitly say when an answer needs code not shown (no guessing/hallucinating structure)
- Force structured JSON output so `cited_nodes` can be extracted programmatically and used to highlight the graph:
  ```json
  {"answer": "...", "cited_nodes": ["auth.py::login", "db.py::get_user"]}
  ```
- Model tiering: `/node/{id}/explain` calls are small (single node + neighbors) — use a smaller/cheaper NIM model (e.g. a Llama 8B-class model), since these fire often during graph exploration. `/chat` calls carry larger expanded subgraphs — use a stronger NIM-hosted model (e.g. Llama 70B-class or a code-specialized model like `nvidia/nemotron` or a Qwen/DeepSeek-Coder NIM). Don't hardcode one model everywhere. NIM exposes an OpenAI-compatible `/v1/chat/completions` endpoint, so swapping models is a config change, not a code change.

#### 2.5 Frontend
- React app, single page
- Graph rendering: `react-force-graph-3d` (fastest path to something visually impressive, reuse your DeployForge 3D-UI patterns) or `react-force-graph-2d` if 3D is overkill for v0 — **recommend starting 2D**, upgrade to 3D once the data layer is solid, so you're not debugging rendering and parsing at the same time
- Click node → side panel with explanation (calls `/node/{id}/explain`)
- Chat panel: question in, answer out, clicking a cited node highlights it in the graph

### 3. Data flow for one demo scenario
1. User runs `python parse.py ./my-repo` → `graph.json` generated
2. Frontend loads `graph.json`, renders force-directed graph
3. User clicks node `auth.py::login` → sidebar calls `/node/auth.py::login/explain` → NIM-hosted model explains using function code + neighbor context
4. User types "how does login talk to the database?" → `/chat` retrieves `login`, `get_user`, `db connection` nodes → NIM-hosted model answers, cites those 3 nodes → frontend highlights them in the graph

### 4. Build order (matches session plan from before)
| Session | Deliverable | Definition of done |
|---|---|---|
| 1 | Parser → `graph.json` on a real repo | JSON has correct nodes/edges, eyeballed manually |
| 2 | Graph served via FastAPI, basic 2D render in browser | Can see nodes/edges rendered, pan/zoom works |
| 3 | Node click → `/explain` endpoint working | Click any node, get a coherent LLM explanation |
| 4 | Chat endpoint with graph-grounded retrieval | 5 test questions answered correctly, cited nodes shown |
| 5 | Polish: highlight cited nodes on graph, filter by type | Demo-ready |

### 5. Open technical decisions (flag before building, not during)
- **Call resolution accuracy**: static call-graph resolution in Python is inherently imperfect (dynamic dispatch, `getattr`, decorators). v0 should accept ~80% accuracy and note this as a known limitation, not chase 100%.
- **Node retrieval method**: keyword match (v0, fast) vs. embeddings (better recall, more setup). Recommend keyword match first, swap in embeddings only if retrieval quality is visibly bad in testing.
- **Persistence**: rebuild graph every run (v0) vs. cache/diff on file change (v1). Rebuild-every-run is fine until repos get large.

### 6. Deployment target — local app, NVIDIA-hosted NIM for inference

**Decision locked in**: app stays local (parser, graph store, frontend all run on your machine) + **NVIDIA-hosted NIM** (`https://integrate.api.nvidia.com/v1`) for inference, since self-hosting NIM needs a GPU you don't have. No self-hosted-NIM path for this project — drop that option entirely rather than carry it as a "maybe later."

- OpenAI-compatible endpoint — same `base_url` swap as any other provider, retrieval/injection/generation logic in §2.4 doesn't change based on this
- API key + pay-per-token (or free tier credits), same shape as any hosted LLM API
- Only inference calls leave your machine — parsing, graph storage, and frontend never touch the network

### 7. Explicitly deferred to v1+
- Multi-language support (add tree-sitter grammars incrementally: JS/TS next, likely)
- Agentic modification mode (graph-aware PR generation)
- Auto-synced doc generation
- Embedding-based semantic retrieval
- Persistent graph DB (e.g., swap networkx for a real graph DB if repos get big)
- Cloud hosting / multi-tenancy (see §6 — deliberately local-first for v0)
