# CodeGraph — v4 PRD + TRD

> Three parallel scope increases on top of v3: (1) parser support for all major languages via a registry pattern instead of one-off additions, (2) a provider fallback chain so free-tier rate limits stop being a single point of failure, (3) new functionality that's genuinely useful, not just "more features."

---

## PART 1 — PRD

### 1. Goal (v4)
- **Language coverage**: go from "Python + one JS/TS pass" to a parser registry covering the languages you're actually likely to point this at.
- **Resilience**: no single free-tier rate limit (NIM's ~40 RPM) can stall the whole tool — automatic failover across multiple free providers.
- **New functionality**: a focused set of additions that build on the graph you already have, not scope creep for its own sake.

### 2. Non-goals
- Not adding every language tree-sitter supports — prioritize by actual usage likelihood (see §5 table)
- Not building a generic "plug in any provider" system with a UI for configuring it — a config file is enough for a solo tool
- Not every functionality idea that came to mind — cut list is in TRD §5

### 3. Target user
Same as before — but multi-language + fallback resilience matters more now because a tool that only handles Python and dies silently when NIM rate-limits isn't something you can rely on day-to-day, even for your own projects.

### 4. Core user stories

**Language scale-out**
| # | Story | Priority |
|---|---|---|
| 1 | I can point the parser at a repo in any of the top ~8 languages and get a working graph | P0 |
| 2 | Adding a new language later is a config entry + a small extraction function, not a parser rewrite | P0 |

**Provider fallback**
| # | Story | Priority |
|---|---|---|
| 3 | When NIM hits its rate limit, the tool automatically tries the next free provider instead of failing the request | P0 |
| 4 | I can see which provider actually answered a given request (for debugging quota issues) | P1 |
| 5 | I can reorder or disable a provider in the chain via config, without touching code | P1 |

**New functionality**
| # | Story | Priority |
|---|---|---|
| 6 | I can ask "what would break if I change this function?" and get a blast-radius answer (transitive callers) without needing a full chat round-trip | P0 |
| 7 | The graph flags highly-connected "hotspot" nodes (high fan-in/fan-out) — the parts of the codebase most likely to be risky to touch | P0 |
| 8 | The graph auto-refreshes when I save a file, instead of requiring a manual re-parse | P1 |
| 9 | I can use the tool from a terminal (`codegraph chat "..."`) without the frontend running | P1 |
| 10 | I can export the graph as JSON/GraphML/DOT for use in other tools | P2 |
| 11 | I can diff two graph snapshots to see what structurally changed between commits | P2 |

### 5. Language priority table
Ranked by real-world prevalence + tree-sitter grammar maturity — build top to bottom, stop wherever's "enough":

| Priority | Language | Grammar | Notes |
|---|---|---|---|
| Done | Python | `tree-sitter-python` | v0–v3 |
| Done | JS/TS | `tree-sitter-javascript` / `tree-sitter-typescript` | v3 |
| P0 | Java | `tree-sitter-java` | Huge install base, straightforward import/call model |
| P0 | Go | `tree-sitter-go` | Clean package/import model, easiest after Python |
| P1 | C# | `tree-sitter-c-sharp` | Common in enterprise repos |
| P1 | C/C++ | `tree-sitter-c` / `tree-sitter-cpp` | Header/include resolution is genuinely harder — expect more edge cases |
| P2 | Rust | `tree-sitter-rust` | Module system differs enough to need real thought, not copy-paste |
| P2 | Ruby / PHP | `tree-sitter-ruby` / `tree-sitter-php` | Add only if you actually hit a repo in these |

### 6. Success criteria
- At least Java and Go parse a small real repo correctly (import edges + call edges verified against the eval-harness pattern from v3)
- A live demo where killing/exhausting NIM mid-session doesn't break the chat — it silently continues on the next provider
- Blast-radius and hotspot features return correct results on the `sample-repo/shop` fixture
- File-watch re-parse works without needing to restart the backend

---

## PART 2 — TRD

### 1. Architecture delta
Two new abstraction layers get introduced — a **language registry** (parser side) and a **provider chain** (inference side) — plus a handful of new endpoints for the functionality additions. Everything else (graph store, retrieval, agent, docs) stays as-is; these are additive, not rewrites.

### 2. Language registry

Replace any hardcoded "this is Python" assumption in `parser.py` with a registry:

```python
LANGUAGE_REGISTRY = {
    ".py":  {"grammar": "python",     "extractor": extract_python},
    ".js":  {"grammar": "javascript", "extractor": extract_js},
    ".ts":  {"grammar": "typescript", "extractor": extract_ts},
    ".java":{"grammar": "java",       "extractor": extract_java},
    ".go":  {"grammar": "go",         "extractor": extract_go},
    # add one line + one function per new language
}
```

- Dispatch on file extension at parse time; unknown extensions are skipped, not errored
- Each `extract_*` function returns the same node/edge schema (§2.2 from v0's TRD) — this is what keeps graph store, retrieval, agent, and docs language-agnostic
- **Don't try to share one generic AST-walking function across languages.** Import syntax, call syntax, and class/method structure differ enough that a shared abstraction ends up more complex than five separate ~80-line functions. Copy-adapt-diverge, not force-unify.
- Cross-language repos (e.g. a Python backend + JS frontend in one repo) work automatically once both extractors exist — nodes from both land in the same graph, though cross-language edges (JS calling a Python API endpoint) aren't resolved automatically; that's a real limitation worth stating, not solving, for v4.

### 3. Provider fallback chain

#### 3.1 Config
```yaml
# providers.yaml
providers:
  - name: nim
    base_url: https://integrate.api.nvidia.com/v1
    api_key_env: NIM_API_KEY
    models: {explain: nemotron-3.5-lightning-30b-a3b, chat: nemotron-3-super-120b-a12b}
    rpm_limit: 40
  - name: openrouter
    base_url: https://openrouter.ai/api/v1
    api_key_env: OPENROUTER_API_KEY
    models: {explain: "qwen/qwen3-coder:free", chat: "nvidia/nemotron-3-ultra:free"}
    rpm_limit: 20
  - name: gemini
    base_url: https://generativelanguage.googleapis.com/v1beta/openai
    api_key_env: GEMINI_API_KEY
    models: {explain: gemini-2.0-flash-lite, chat: gemini-2.0-flash}
    rpm_limit: 15
```

Notes on the providers themselves (current as of research, verify before relying on them — free-tier numbers and free-model lists both shift without much notice):
- **NIM** stays primary — already integrated, best code-specialized models, ~40 RPM per account.
- **OpenRouter** free tier: models suffixed `:free`, 20 RPM, 50 requests/day (jumps to 1,000/day after the account has ever bought $10 of credit — worth doing once, purely to raise the daily cap, even if you never use paid models). Free model list rotates — pin a model, but expect to update the config occasionally when one gets deprecated.
- **Gemini**: has an OpenAI-compatible endpoint now (`.../v1beta/openai`), so it slots into the same client code as the other two. Free tier limits vary by model and Google has been inconsistent about publishing exact numbers recently — treat any number as a ceiling to verify in Google AI Studio, not a guarantee.
- Optional fourth (add if you actually need more headroom): **Groq** — fast inference, historically ~30 RPM / 1,000 RPD on free tier, OpenAI-compatible.

#### 3.2 Failover logic
```
for provider in providers (in config order):
    if provider.circuit_open: skip
    try:
        response = call(provider, ...)
        return response, provider.name   # tag which provider actually served it
    except RateLimitError (429):
        mark provider circuit_open for cooldown_seconds (e.g. 60s)
        continue to next provider
    except other error:
        log, continue to next provider
raise AllProvidersExhausted
```

- **Circuit breaker per provider**, not a single global backoff — a 429 from NIM shouldn't slow down the OpenRouter attempt that happens next in the same request.
- Each provider keeps its **own** rate limiter (extend the v0 client-side limiter from a single NIM budget to one per provider, using each provider's own `rpm_limit`).
- Return which provider served the request (per user story #4) — log it, and surface it in dev/debug mode in the frontend, not in the normal chat UI (users don't need to see this, you do).
- Model-name mapping is per-provider (a "chat-tier" model on NIM isn't the same string as one on OpenRouter) — the `models: {explain: ..., chat: ...}` block per provider in the config is what makes this a data problem, not a code problem, when a provider swaps its free lineup.

### 4. New functionality — implementation notes

**Blast radius (`GET /node/{id}/blast-radius`)** — pure graph traversal, no LLM call needed: walk incoming edges transitively (who calls this, who calls those callers, N hops) and return the set. Cheap, fast, and the most directly useful new endpoint — this is what you actually want to check before agent mode 3 touches something.

**Hotspot detection (`GET /graph/hotspots`)** — also pure graph math: rank nodes by in-degree + out-degree (or a proper centrality measure like betweenness if `networkx` is already a dependency — it is). No LLM involved. Surface top-N in the frontend as a visual weight/size on nodes.

**File watcher (incremental re-parse)** — use `watchdog` (Python) to monitor the repo path; on file change, re-run just that file's extraction and patch the graph rather than a full re-parse. Full re-parse-on-save is the fallback if incremental patching proves fiddly — don't over-engineer this on the first pass.

**CLI (`codegraph parse|chat|explain <args>`)** — thin wrapper calling the same FastAPI endpoints (or importing the underlying functions directly to skip the HTTP hop). Use `click` or `argparse`; this is a small addition since all the real logic already lives in the backend.

**Graph export (`GET /graph/export?format=json|graphml|dot`)** — `networkx` has built-in writers for GraphML and DOT; JSON is what you already produce. Low effort, real utility (lets people load the graph into Gephi, yEd, or another tool if they want a different visualization than yours).

**Snapshot diff (`POST /graph/diff`)** — given two `graph.json` snapshots (e.g. from two commits), diff node/edge sets and report added/removed/changed. Pure set comparison, no LLM needed. Useful as a "what did this PR structurally touch" check — pairs naturally with agent mode 3's PR flow.

### 5. Cut from this version (explicitly, so it's not silently forgotten)
- Multi-repo cross-referencing — real complexity jump (resolving imports across repo boundaries), not worth it until a concrete use case shows up
- VS Code extension — a real project on its own, not a "add it in" item
- A fifth+ inference provider — three is enough redundancy; more providers past that is marginal resilience for real config complexity

### 6. Build order
| Session | Deliverable |
|---|---|
| 1 | Language registry refactor + Java, Go extractors |
| 2 | Provider config + failover logic (NIM → OpenRouter → Gemini), tested by artificially exhausting NIM's limit |
| 3 | Blast-radius + hotspot endpoints (pure graph math, fastest wins) |
| 4 | File watcher incremental re-parse |
| 5 | CLI |
| 6 | Graph export + snapshot diff |
