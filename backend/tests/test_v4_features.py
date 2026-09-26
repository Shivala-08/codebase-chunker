"""v4 feature tests: language registry, provider chain, analysis, export, diff, watcher.

Per the PRD's own test pattern: expected values captured by running the code
against fixtures first, then pinned. Provider tests never touch the network —
fake OpenAI clients are injected at the Provider object level.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from codegraph import providers as pc
from codegraph.graph_analysis import blast_radius, hotspots
from codegraph.graph_io import diff_snapshots, export, load_snapshot
from codegraph.parser import LANGUAGE_REGISTRY, parse_repo


# ============================================================================
# Language registry (PRD user stories #1, #2)
# ============================================================================

def test_registry_covers_priority_languages():
    expected = {".py", ".js", ".ts", ".java", ".go", ".cs", ".c", ".cpp", ".rs"}
    assert expected <= set(LANGUAGE_REGISTRY)


def test_unknown_extension_files_are_skipped(tmp_path: Path):
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("not code", encoding="utf-8")
    (tmp_path / "image.png").write_bytes(b"\x89PNG")
    g = parse_repo(str(tmp_path))
    assert {n["file"] for n in g["nodes"]} == {"app.py"}


def test_java_repo_parses_classes_methods_and_calls(tmp_path: Path):
    (tmp_path / "Main.java").write_text(
        """
import java.util.List;

public class Main {
    private List<String> items;
    public void run() { helper(); }
    private void helper() { System.out.println(items.size()); }
}
""", encoding="utf-8")
    g = parse_repo(str(tmp_path))
    by_id = {n["id"]: n for n in g["nodes"]}
    assert by_id["Main.java::Main"]["type"] == "class"
    assert by_id["Main.java::Main.run"]["type"] == "method"
    edges = {(e["from"], e["to"], e["type"]) for e in g["edges"]}
    assert ("Main.java::Main.run", "Main.java::Main.helper", "calls") in edges


def test_go_repo_parses_receiver_methods_and_calls(tmp_path: Path):
    (tmp_path / "main.go").write_text(
        '''
package main

import "fmt"

type Repo struct{}

func (r *Repo) Save() error { return nil }

func main() {
    r := &Repo{}
    r.Save()
    fmt.Println("done")
}
''', encoding="utf-8")
    g = parse_repo(str(tmp_path))
    by_id = {n["id"]: n for n in g["nodes"]}
    assert by_id["main.go::Repo.Save"]["type"] == "method"
    assert by_id["main.go::main"]["type"] == "function"
    edges = {(e["from"], e["to"], e["type"]) for e in g["edges"]}
    assert ("main.go::main", "main.go::Repo.Save", "calls") in edges


def test_python_fixture_graph_unchanged_by_registry_refactor(shop_graph):
    """The v3 parser tests pin the shop fixture; this asserts the refactor
    didn't shift the fundamentals (they re-run fully in test_parser.py)."""
    ids = {n["id"] for n in shop_graph["nodes"]}
    assert "db.py::save_order" in ids
    assert "cart.py::Cart.checkout" in ids


def test_python_js_mixed_repo_lands_in_one_graph(tmp_path: Path):
    (tmp_path / "server.py").write_text(
        "def handler():\n    return compute()\n\ndef compute():\n    return 1\n",
        encoding="utf-8")
    (tmp_path / "client.js").write_text(
        "function render() { return fetch_thing(); }\n", encoding="utf-8")
    g = parse_repo(str(tmp_path))
    files = {n["file"] for n in g["nodes"]}
    assert files == {"server.py", "client.js"}
    by_id = {n["id"]: n for n in g["nodes"]}
    assert "client.js::render" in by_id
    assert "server.py::handler" in by_id


# ============================================================================
# Provider chain (PRD user stories #3, #4, #5)
# ============================================================================

class _FakeResponse:
    def __init__(self, text="ok"):
        self.choices = [type("C", (), {
            "message": type("M", (), {"content": text})(),
            "finish_reason": "stop",
        })()]


class _FlakyCompletions:
    """Fails with a 429-shaped error the first N calls, then succeeds."""
    calls = 0

    def __init__(self, fail_times: int = 999):
        self.fail_times = fail_times

    def create(self, **kwargs):
        _FlakyCompletions.calls += 1
        if _FlakyCompletions.calls <= self.fail_times:
            err = Exception("rate limited")
            err.response = type("R", (), {"status_code": 429})()
            raise err
        return _FakeResponse(f"answer-from-{kwargs['model']}")


class _OkCompletions:
    def create(self, **kwargs):
        return _FakeResponse(f"answer-from-{kwargs['model']}")


@pytest.fixture()
def chain(tmp_path, monkeypatch):
    """Two-provider chain with injectable fake clients, no network."""
    cfg = tmp_path / "providers.yaml"
    cfg.write_text("""
providers:
  - name: fakeA
    base_url: https://a.invalid/v1
    api_key_env: FAKE_A_KEY
    models: {explain: a-explain, chat: a-chat}
    rpm_limit: 600
  - name: fakeB
    base_url: https://b.invalid/v1
    api_key_env: FAKE_B_KEY
    models: {explain: b-explain, chat: b-chat}
    rpm_limit: 600
""", encoding="utf-8")
    monkeypatch.setenv("CODEGRAPH_PROVIDERS_YAML", str(cfg))
    monkeypatch.setenv("FAKE_A_KEY", "k-a")
    monkeypatch.setenv("FAKE_B_KEY", "k-b")
    monkeypatch.setenv("CODEGRAPH_CIRCUIT_COOLDOWN", "0.05")  # fast un-trip in tests
    pc.reset_providers()
    yield pc
    pc.reset_providers()


def test_chain_returns_first_healthy_provider_and_name(chain):
    pa = chain.get_providers()[0]
    pa.client = type("Client", (), {"chat": type("Ch", (), {"completions": _OkCompletions()})()})()
    text, name = chain.complete(system="s", user="u", tier="chat")
    assert text.startswith("answer-from-a-chat")
    assert name == "fakeA"


def test_rate_limited_provider_fails_over_to_next(chain):
    pa = chain.get_providers()[0]
    pb = chain.get_providers()[1]
    pa.client = type("Client", (), {"chat": type("Ch", (), {"completions": _FlakyCompletions(999)})()})()
    pb.client = type("Client", (), {"chat": type("Ch", (), {"completions": _OkCompletions()})()})()
    text, name = chain.complete(system="s", user="u", tier="chat")
    assert name == "fakeB"
    assert text.startswith("answer-from-b-chat")


def test_circuit_opens_on_429_and_skips_next_request(chain):
    pa, pb = chain.get_providers()[0], chain.get_providers()[1]
    pa.client = type("Client", (), {"chat": type("Ch", (), {"completions": _FlakyCompletions(999)})()})()
    pb.client = type("Client", (), {"chat": type("Ch", (), {"completions": _OkCompletions()})()})()

    chain.complete(system="s", user="u", tier="chat")
    assert pa.circuit_open() is True  # tripped by the 429

    # while the circuit is open the provider isn't even attempted
    called = {"a": 0}
    class Counting:
        def create(self, **k):
            called["a"] += 1
            raise Exception("should not be called")
    pa.client = type("Client", (), {"chat": type("Ch", (), {"completions": Counting()})()})()
    _text, name = chain.complete(system="s", user="u", tier="chat")
    assert name == "fakeB"
    assert called["a"] == 0

    # after cooldown the circuit resets (cooldown=0.05s in the fixture)
    time.sleep(0.1)
    assert pa.circuit_open() is False


def test_all_providers_exhausted_raises(chain):
    for p in chain.get_providers():
        p.client = type("Client", (), {"chat": type("Ch", (), {"completions": _FlakyCompletions(999)})()})()
    with pytest.raises(chain.AllProvidersExhausted):
        chain.complete(system="s", user="u", tier="chat")


def test_provider_without_key_is_skipped(chain, monkeypatch):
    monkeypatch.delenv("FAKE_A_KEY", raising=False)
    pc.reset_providers()
    assert chain.get_providers()[0].has_key() is False
    pb = chain.get_providers()[1]
    pb.client = type("Client", (), {"chat": type("Ch", (), {"completions": _OkCompletions()})()})()
    _text, name = chain.complete(system="s", user="u", tier="chat")
    assert name == "fakeB"


def test_reordering_config_changes_chain_order(chain, tmp_path, monkeypatch):
    cfg = tmp_path / "providers.yaml"
    cfg.write_text("""
providers:
  - name: fakeB
    base_url: https://b.invalid/v1
    api_key_env: FAKE_B_KEY
    models: {explain: b-explain, chat: b-chat}
    rpm_limit: 600
  - name: fakeA
    base_url: https://a.invalid/v1
    api_key_env: FAKE_A_KEY
    models: {explain: a-explain, chat: a-chat}
    rpm_limit: 600
""", encoding="utf-8")
    monkeypatch.setenv("CODEGRAPH_PROVIDERS_YAML", str(cfg))
    chain.reset_providers()
    p0 = chain.get_providers()[0]
    p0.client = type("Client", (), {"chat": type("Ch", (), {"completions": _OkCompletions()})()})()
    _text, name = chain.complete(system="s", user="u", tier="chat")
    assert name == "fakeB"  # now first in config order (user story #5)


def test_status_surface_reports_keys_and_circuits(chain):
    chain.get_providers()[0].trip_circuit(30)
    status = {s["name"]: s for s in chain.last_provider_status()}
    assert status["fakeA"]["circuit_open"] is True
    assert status["fakeA"]["has_key"] is True
    assert status["fakeB"]["circuit_open"] is False


def test_rate_limited_provider_fails_over_to_next(chain):
    pa, pb = chain.get_providers()[0], chain.get_providers()[1]
    pa.client = type("Client", (), {"chat": type("Ch", (), {"completions": _FlakyCompletions(999)})()})()
    pb.client = type("Client", (), {"chat": type("Ch", (), {"completions": _OkCompletions()})()})()
    text, name = chain.complete(system="s", user="u", tier="chat")
    assert name == "fakeB"
    assert text.startswith("answer-from-b-chat")


# ============================================================================
# Blast radius + hotspots (PRD user stories #6, #7)
# ============================================================================

def test_blast_radius_finds_transitive_callers(shop_store):
    result = blast_radius(shop_store, "db.py::save_order", max_hops=3)
    ids = set(result["affected"])
    # direct caller
    assert "cart.py::Cart.checkout" in ids
    # transitive: checkout is called via the service layer path
    assert any("service.py" in nid or "router.py" in nid for nid in ids)
    assert result["total"] > 5
    # rings are ordered by hop
    assert [r["hop"] for r in result["rings"]] == sorted(r["hop"] for r in result["rings"])


def test_blast_radius_root_excluded_and_hop1_has_direct_callers(shop_store):
    result = blast_radius(shop_store, "db.py::save_order", max_hops=2)
    assert "db.py::save_order" not in set(result["affected"])
    hop1 = {n["id"] for n in result["rings"][0]["nodes"]}
    assert "cart.py::Cart.checkout" in hop1


def test_blast_radius_unknown_node_is_empty(shop_store):
    result = blast_radius(shop_store, "ghost.py::phantom", max_hops=3)
    assert result["total"] == 0 and result["rings"] == []


def test_hotspots_rank_by_degree_and_skip_modules(shop_store):
    spots = hotspots(shop_store, limit=10)
    assert spots, "expected hotspots"
    assert all(shop_store.node_data[s["id"]]["type"] != "module" for s in spots)
    degrees = [s["degree"] for s in spots]
    assert degrees == sorted(degrees, reverse=True)
    # save_order is the fixture's fan-in sink — it must rank at/near the top
    assert any(s["id"] == "db.py::save_order" for s in spots[:3])


# ============================================================================
# Export + snapshot diff (PRD user stories #10, #11)
# ============================================================================

def test_export_graphml_and_dot(shop_store):
    gml = export(shop_store, "graphml")
    assert "graphml" in gml and "db.py::save_order" in gml
    dot = export(shop_store, "dot")
    assert dot.startswith("digraph")
    assert '"db.py::save_order"' in dot
    assert '"cart.py::Cart.checkout" -> "db.py::save_order"' in dot


def test_export_rejects_unknown_format(shop_store):
    with pytest.raises(ValueError):
        export(shop_store, "yaml")


def _write_snapshot(path: Path, graph: dict) -> Path:
    path.write_text(json.dumps(graph), encoding="utf-8")
    return path


def test_snapshot_diff_reports_added_removed_changed(tmp_path: Path, shop_graph):
    before = json.loads(json.dumps(shop_graph))  # deep copy

    # after: remove a node, add a node, move a function down 3 lines
    after_nodes = []
    for n in before["nodes"]:
        if n["id"] == "utils.py::validate_email":
            continue  # removed
        if n["id"] == "db.py::save_order":
            n = dict(n, line_start=n["line_start"] + 3, line_end=n["line_end"] + 3)
        after_nodes.append(n)
    after_nodes.append({
        "id": "newmod.py::brand_new", "type": "function", "file": "newmod.py",
        "name": "brand_new", "qualname": "brand_new", "line_start": 1,
        "line_end": 2, "docstring": None,
    })
    after = {"repo": before["repo"], "nodes": after_nodes,
             "edges": [e for e in before["edges"]
                       if e["from"] != "utils.py::validate_email"
                       and e["to"] != "utils.py::validate_email"]}

    pa = _write_snapshot(tmp_path / "a.json", before)
    pb = _write_snapshot(tmp_path / "b.json", after)
    d = diff_snapshots(load_snapshot(pa), load_snapshot(pb))

    assert d["nodes"]["added"] == ["newmod.py::brand_new"]
    assert d["nodes"]["removed"] == ["utils.py::validate_email"]
    assert any(c["id"] == "db.py::save_order" for c in d["nodes"]["changed"])
    assert d["summary"]["nodes_added"] == 1
    assert d["summary"]["nodes_removed"] == 1
    assert d["summary"]["nodes_changed"] == 1


def test_snapshot_diff_identical_snapshots_is_empty(tmp_path: Path, shop_graph):
    pa = _write_snapshot(tmp_path / "a.json", shop_graph)
    pb = _write_snapshot(tmp_path / "b.json", shop_graph)
    d = diff_snapshots(load_snapshot(pa), load_snapshot(pb))
    assert d["summary"]["nodes_added"] == 0
    assert d["summary"]["edges_removed"] == 0


def test_load_snapshot_missing_file(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        load_snapshot(tmp_path / "nope.json")


# ============================================================================
# File watcher (PRD user story #8)
# ============================================================================

def test_watcher_fires_debounced_reparse(tmp_path: Path):
    from codegraph.watcher import FileWatcher

    (tmp_path / "mod.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    fires = {"n": 0}

    def on_change():
        fires["n"] += 1

    w = FileWatcher(on_change, debounce_seconds=0.2)
    w.start(str(tmp_path))
    try:
        assert w.status()["active"] is True
        assert w.status()["repo"] == str(tmp_path.resolve())

        time.sleep(0.1)  # let the observer spin up
        (tmp_path / "mod.py").write_text("def a():\n    return 2\n", encoding="utf-8")
        (tmp_path / "other.py").write_text("x = 1\n", encoding="utf-8")
        deadline = time.time() + 5
        while fires["n"] == 0 and time.time() < deadline:
            time.sleep(0.05)
        assert fires["n"] >= 1, "watcher never fired on file change"

        # debouncing: the burst above should coalesce to ONE callback within
        # the debounce window (allow a second if timing splits the burst)
        time.sleep(0.6)
        assert fires["n"] <= 2
    finally:
        w.stop()
    assert w.status()["active"] is False


def test_watcher_ignores_non_code_files(tmp_path: Path):
    from codegraph.watcher import FileWatcher

    fires = {"n": 0}
    w = FileWatcher(lambda: fires.__setitem__("n", fires["n"] + 1), debounce_seconds=0.2)
    w.start(str(tmp_path))
    try:
        time.sleep(0.1)
        (tmp_path / "README.md").write_text("change", encoding="utf-8")
        (tmp_path / "data.json").write_text("{}", encoding="utf-8")
        time.sleep(0.8)
        assert fires["n"] == 0, "non-code file change must not trigger re-parse"
    finally:
        w.stop()


def test_watcher_start_is_idempotent_for_same_repo(tmp_path: Path):
    from codegraph.watcher import FileWatcher

    w = FileWatcher(lambda: None, debounce_seconds=0.2)
    w.start(str(tmp_path))
    s1 = w.status()
    w.start(str(tmp_path))   # same repo -> same observer, no churn
    s2 = w.status()
    try:
        assert s1 == s2
    finally:
        w.stop()
