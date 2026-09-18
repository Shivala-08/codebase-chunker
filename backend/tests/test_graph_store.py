"""GraphStore behavior (PRD v3 §2.2 test_graph_store.py).

Covers the retrieval surface the chat/agent layers sit on: neighbor walks with
depth cutoffs, edge-type reporting, keyword-seeded subgraph retrieval with
structural expansion, and graceful handling of junk input.

Expected retrievals were captured empirically against the shop fixture before
being pinned here.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from codegraph.graph_store import GraphStore


# ------------------------------------------------------------- load/keys --

def test_store_loads_fixture_graph(shop_store):
    assert len(shop_store.node_data) >= 40
    assert shop_store.g.number_of_edges() >= 100
    assert shop_store.repo.endswith("sample-repo/shop")


def test_store_ignores_edges_with_unknown_endpoints(shop_graph, tmp_path: Path):
    broken = {
        "repo": "broken",
        "nodes": [n for n in shop_graph["nodes"] if n["id"] == "db.py::save_order"] + [
            {"id": "x.py::solo", "type": "function", "file": "x.py", "name": "solo",
             "qualname": "solo", "line_start": 1, "line_end": 2, "docstring": None},
        ],
        "edges": [
            {"from": "x.py::solo", "to": "ghost.py::phantom", "type": "calls"},
            {"from": "phantom.py", "to": "x.py::solo", "type": "imports"},
        ],
    }
    p = tmp_path / "broken.json"
    p.write_text(json.dumps(broken), encoding="utf-8")
    store = GraphStore(p)
    assert store.g.number_of_edges() == 0
    assert set(store.node_data) == {"x.py::solo", "db.py::save_order"}


# --------------------------------------------------------------- queries --

def test_get_node_roundtrip(shop_store):
    node = shop_store.get_node("db.py::save_order")
    assert node is not None
    assert node["type"] == "function"
    assert node["file"] == "db.py"
    assert shop_store.get_node("nope.py::missing") is None


def test_get_neighbors_depth_1(shop_store):
    nb = set(shop_store.get_neighbors("db.py::save_order", depth=1))
    assert "cart.py::Cart.checkout" in nb                     # caller
    assert "db.py::_connect" in nb                            # callee
    assert "db.py" in nb                                      # container
    assert "auth.py::login" not in nb                         # 2+ hops away


def test_get_neighbors_depth_grows_monotonically(shop_store):
    d1 = set(shop_store.get_neighbors("db.py::save_order", depth=1))
    d2 = set(shop_store.get_neighbors("db.py::save_order", depth=2))
    assert d1 < d2
    # 2 undirected hops from save_order reach auth.py's module node (via the
    # imports edge) and checkout's cart-container family — but not login the
    # function, which is 3 hops out (login -> get_user_by_email -> db.py -> ...)
    assert "auth.py" in d2
    assert "auth.py::login" not in d2
    assert "cart.py::Cart.total" in d2  # save_order -> checkout -> Cart -> Cart.total


def test_get_neighbors_unknown_node_returns_empty(shop_store):
    assert shop_store.get_neighbors("ghost.py::phantom", depth=2) == []


def test_get_edge_types_reports_both_directions(shop_store):
    a = shop_store.get_edge_types("cart.py::Cart.checkout", "db.py::save_order")
    b = shop_store.get_edge_types("db.py::save_order", "cart.py::Cart.checkout")
    assert "calls" in a
    assert "calls" in b  # direction of the query must not matter
    assert shop_store.get_edge_types("db.py::save_order", "auth.py::login") == []


# ------------------------------------------------------------- retrieval --

def test_retrieval_finds_save_order_for_order_question(shop_store):
    q = "how does an order get saved to the database?"
    ids = shop_store.get_subgraph_for_query(q, max_nodes=18, expand_hops=2)
    assert ids, "retrieval returned nothing"
    assert "db.py::save_order" in ids
    assert ids.index("db.py::save_order") <= 2, "top hit should rank first"
    # structural expansion pulls in the caller even though its name overlaps
    # the question not at all
    assert "cart.py::Cart.checkout" in ids


def test_retrieval_finds_login_for_auth_question(shop_store):
    ids = shop_store.get_subgraph_for_query("how does login verify a password?",
                                            max_nodes=18, expand_hops=2)
    assert "auth.py::login" in ids
    assert ids.index("auth.py::login") == 0


def test_retrieval_drops_module_nodes(shop_store):
    ids = shop_store.get_subgraph_for_query("orders orders orders", max_nodes=18)
    for nid in ids:
        assert shop_store.node_data[nid]["type"] != "module"


def test_retrieval_garbage_question_returns_empty(shop_store):
    assert shop_store.get_subgraph_for_query("zzzqxv unlikelyword") == []


def test_retrieval_max_nodes_is_respected(shop_store):
    for k in (3, 8, 15):
        ids = shop_store.get_subgraph_for_query("user login register email db",
                                                max_nodes=k)
        assert len(ids) <= k


def test_retrieval_expand_hops_1_covers_less_than_2(shop_store):
    q = "how does an order get saved to the database?"
    near = set(shop_store.get_subgraph_for_query(q, max_nodes=18, expand_hops=1))
    far = set(shop_store.get_subgraph_for_query(q, max_nodes=18, expand_hops=2))
    assert near <= far or near == far
    assert len(far) >= len(near)


def test_retrieval_demotes_test_hits(shop_store):
    # test functions match question wording constantly; they must rank below
    # the real implementation for the same topic
    ids = shop_store.get_subgraph_for_query("save order items to the database",
                                            max_nodes=5, expand_hops=1)
    assert "db.py::save_order" in ids
    assert ids.index("db.py::save_order") <= 1
    assert "test_shop.py::test_save_order_persists_line_items" not in ids[:2]


def test_describe_edges_lists_edges_among_selected_nodes(shop_store):
    picked = ["cart.py::Cart.checkout", "db.py::save_order", "db.py"]
    lines = shop_store.describe_edges(picked)
    assert any("cart.py::Cart.checkout -> db.py::save_order (calls)" in ln for ln in lines)
    assert any("contains" in ln for ln in lines)


def test_describe_edges_empty_for_disjoint_nodes(shop_store):
    lines = shop_store.describe_edges(["auth.py::login", "db.py::_row_to_user"])
    assert lines == []
