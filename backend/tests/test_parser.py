"""Parser correctness against the shop fixture (PRD v3 §2.2 test_parser.py).

Every expected value below was captured by running the parser on the fixture
repo — the tests pin the structure the retrieval and docs layers depend on:
node ids/qualnames, contains/imports/imports_symbol/calls edges, and the
class/method distinction.
"""
from __future__ import annotations

from collections import Counter

import pytest


# ------------------------------------------------------------------ nodes --

def test_every_fixture_module_becomes_a_node(shop_graph):
    ids = {n["id"] for n in shop_graph["nodes"]}
    for module in ("auth.py", "cart.py", "db.py", "utils.py", "test_shop.py"):
        assert module in ids


def test_node_types_include_class_methods_and_functions(shop_graph):
    counts = Counter(n["type"] for n in shop_graph["nodes"])
    assert counts["module"] == 9          # auth, cart, conftest, db, repository,
    #                                        router, service, test_shop, utils
    assert counts["class"] >= 2           # Cart + RegisterRequest
    assert counts["method"] >= 5          # Cart.__init__/add_item/total/checkout,
    #                                        RegisterRequest.__init__
    assert counts["function"] >= 25       # auth/db/repository/router/service/utils


def test_defs_inside_a_class_are_methods_not_functions(shop_graph):
    by_id = {n["id"]: n for n in shop_graph["nodes"]}
    for name in ("__init__", "add_item", "total", "checkout"):
        node = by_id[f"cart.py::Cart.{name}"]
        assert node["type"] == "method", f"Cart.{name} should be a method"


def test_top_level_class_is_contained_by_its_module(shop_graph):
    edges = {(e["from"], e["to"], e["type"]) for e in shop_graph["edges"]}
    assert ("cart.py", "cart.py::Cart", "contains") in edges


def test_nested_def_inside_a_function_is_a_function(shop_graph):
    # user_id() defines nothing nested itself, so assert the general rule on
    # a synthetic parse instead of the fixture
    from codegraph.parser import parse_repo
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "nested.py").write_text(
            "def outer():\n"
            "    def inner():\n"
            "        return 1\n"
            "    return inner\n"
        )
        g = parse_repo(str(root))
    types = {n["id"]: n["type"] for n in g["nodes"]}
    assert types["nested.py::outer"] == "function"
    assert types["nested.py::outer.inner"] == "function"  # not a method


def test_qualnames_are_dotted_for_class_members(shop_graph):
    by_id = {n["id"]: n for n in shop_graph["nodes"]}
    assert by_id["cart.py::Cart.checkout"]["qualname"] == "Cart.checkout"


def test_docstring_extraction(shop_graph):
    by_id = {n["id"]: n for n in shop_graph["nodes"]}
    assert "Persist an order" in by_id["db.py::save_order"]["docstring"]
    # module nodes never carry a docstring in this schema
    assert by_id["db.py"]["type"] == "module"


def test_line_spans_point_at_real_source(shop_graph, shop_dir):
    by_id = {n["id"]: n for n in shop_graph["nodes"]}
    node = by_id["db.py::save_order"]
    lines = (shop_dir / "db.py").read_text(encoding="utf-8").splitlines()
    src = "\n".join(lines[node["line_start"] - 1 : node["line_end"]])
    assert "def save_order" in src


def test_no_edge_connects_a_node_to_itself(shop_graph):
    for e in shop_graph["edges"]:
        assert e["from"] != e["to"], e


# ------------------------------------------------------------------ edges --

def test_import_edges_between_fixture_modules(shop_graph):
    edges = {(e["from"], e["to"], e["type"]) for e in shop_graph["edges"]}
    assert ("auth.py", "db.py", "imports") in edges
    assert ("auth.py", "utils.py", "imports") in edges
    assert ("cart.py", "db.py", "imports") in edges


def test_import_symbol_edges_resolve_to_definitions(shop_graph):
    edges = {(e["from"], e["to"], e["type"]) for e in shop_graph["edges"]}
    assert ("auth.py", "db.py::get_user_by_email", "imports_symbol") in edges
    assert ("cart.py", "db.py::save_order", "imports_symbol") in edges
    assert ("auth.py", "utils.py::hash_pw", "imports_symbol") in edges


def test_cross_file_call_chain_cart_checkout_to_save_order(shop_graph):
    edges = {(e["from"], e["to"], e["type"]) for e in shop_graph["edges"]}
    assert ("cart.py::Cart.checkout", "db.py::get_user", "calls") in edges
    assert ("cart.py::Cart.checkout", "db.py::save_order", "calls") in edges
    # and the db layer sinks into its own helpers
    assert ("db.py::save_order", "db.py::_connect", "calls") in edges


def test_calls_within_auth_login(shop_graph):
    edges = {(e["from"], e["to"], e["type"]) for e in shop_graph["edges"]}
    assert ("auth.py::login", "utils.py::validate_email", "calls") in edges
    assert ("auth.py::login", "db.py::get_user_by_email", "calls") in edges
    assert ("auth.py::login", "utils.py::hash_pw", "calls") in edges


def test_edges_are_deduplicated(shop_graph):
    seen = set()
    for e in shop_graph["edges"]:
        key = (e["from"], e["to"], e["type"])
        assert key not in seen, f"duplicate edge {key}"
        seen.add(key)


def test_unresolvable_calls_are_dropped_not_invented(shop_graph):
    # conn.execute / conn.commit are attribute calls with no matching
    # definition anywhere — the parser must not fabricate targets for them
    targets = {e["to"] for e in shop_graph["edges"] if e["type"] == "calls"}
    assert "execute" not in targets
    assert "commit" not in targets


# ------------------------------------------- PRD §2.1 stress features ----

def test_full_layered_call_chain_resolves(shop_graph):
    """router -> service -> repository -> db, every hop a real calls edge."""
    edges = {(e["from"], e["to"]) for e in shop_graph["edges"]}
    chain = [
        ("router.py::route_place_order", "service.py::place_order"),
        ("service.py::place_order", "service.py::save_order_for_user"),
        ("service.py::save_order_for_user", "db.py::save_order"),
        ("db.py::save_order", "db.py::_insert_order_item"),
        ("router.py::route_order_history", "service.py::order_history"),
        ("service.py::order_history", "repository.py::list_orders"),
        ("service.py::order_history", "repository.py::get_order_items"),
        ("repository.py::list_orders", "db.py::_connect"),
    ]
    for edge in chain:
        assert edge in edges, f"missing chain edge: {edge}"


def test_name_collision_pairs_coexist_as_distinct_nodes(shop_graph):
    """PRD §2.1's keyword-retrieval stress test: same/similar names in
    different files must stay separate nodes."""
    by_id = {n["id"]: n for n in shop_graph["nodes"]}
    for a, b in [
        ("repository.py::total_revenue", "service.py::calc_total_revenue"),
        ("db.py::normalize_user", "service.py::normalize_status"),
        ("db.py::create_order", "repository.py::create_order"),
    ]:
        assert a in by_id and b in by_id
        assert a != b


def test_docstring_free_function_exists(shop_graph):
    """Explain must work from code alone — the fixture guarantees a node
    with no docstring outside test files."""
    n = {x["id"]: x for x in shop_graph["nodes"]}["db.py::normalize_user"]
    assert n["docstring"] is None
    assert not n["file"].startswith("test")


# ------------------------------------------------------- file discovery ----

def test_gitignore_respected_during_discovery(shop_graph, shop_dir):
    ignored = shop_dir / ".gitignore"
    if ignored.is_file() and "shop.db" in ignored.read_text():
        files = {n["file"] for n in shop_graph["nodes"]}
        assert "shop.db" not in files  # and nothing non-python ever appears
    assert all(f.endswith(".py") for f in {n["file"] for n in shop_graph["nodes"]})


def test_parse_repo_rejects_non_directory():
    from codegraph.parser import parse_repo

    with pytest.raises(NotADirectoryError):
        parse_repo("/definitely/not/a/real/dir/codegraph-tests")
