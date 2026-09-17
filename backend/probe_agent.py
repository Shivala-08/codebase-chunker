"""Probe round 3: /no_think on an actionable task, and reduced-context variant."""
import os
import sys

sys.path.insert(0, ".")
from dotenv import load_dotenv

load_dotenv()
from codegraph.agent import _build_task_prompt, _read_source
from codegraph.graph_store import GraphStore
from codegraph import nim
from openai import OpenAI

store = GraphStore("data/graph.json")
task = "rename _row_to_user to deserialize_user and update all call sites"


def build_blocks(node_ids):
    blocks = []
    for nid in node_ids:
        n = store.get_node(nid)
        if n is None:
            continue
        src = _read_source(n["file"], n["line_start"], n["line_end"], str(store.repo))
        lines = src.splitlines()
        if len(lines) > 40:
            src = "\n".join(lines[:40]) + f"\n... (+{len(lines) - 40} lines)"
        rel = store.get_neighbors(nid, depth=1)
        blocks.append(
            f"- {nid} ({n['type']}) — neighbors: {', '.join(rel[:6]) or '(none)'}\n"
            f"  {(n.get('docstring') or '')[:200]}\n  ```python\n  {src}\n  ```"
        )
    return "\n".join(blocks)


def attempt(label, system, user, max_tokens=8192):
    client = OpenAI(base_url=nim.BASE_URL, api_key=os.environ["NVIDIA_API_KEY"])
    resp = client.chat.completions.create(
        model=nim.MODEL_CHAT,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=0.2, max_tokens=max_tokens,
    )
    ch = resp.choices[0]
    raw = ch.message.content or ""
    extra = ch.message.model_extra or {}
    rc = extra.get("reasoning_content") or ""
    has_hunk = "@@" in raw and "+" in raw
    print(f"\n===== {label}: finish={ch.finish_reason} out_len={len(raw)} think_len={len(rc)} usable_diff={has_hunk} =====", file=sys.stderr)
    if has_hunk:
        with open(f"/tmp/probe-diff-{label.split()[0]}.txt", "w") as f:
            f.write(raw)
        print("DIFF SAVED", file=sys.stderr)
    else:
        print(raw[:400], file=sys.stderr)
    return raw


full_ids = store.get_subgraph_for_query(task, max_nodes=12, expand_hops=2)
small_ids = store.get_subgraph_for_query(task, max_nodes=5, expand_hops=1)
print("full:", full_ids, file=sys.stderr)
print("small:", small_ids, file=sys.stderr)

sys0, user_full = _build_task_prompt(task, build_blocks(full_ids), "\n".join(store.describe_edges(full_ids)))
_, user_small = _build_task_prompt(task, build_blocks(small_ids), "\n".join(store.describe_edges(small_ids)))

# D: /no_think, full context
attempt("D nothink-full", sys0 + "\n/no_think", user_full)
# E: /no_think, small context
attempt("E nothink-small", sys0 + "\n/no_think", user_small)
