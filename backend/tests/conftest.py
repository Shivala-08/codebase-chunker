"""Shared fixtures for the CodeGraph test suite.

The fixture repo is `sample-repo/shop` (PRD v3 §2.1): it is parsed once per
pytest session and cached in tmp, so parser assertions run against the same
graph.json that retrieval tests load into a GraphStore. Every path is derived
from this file's location — no env vars, no CWD assumptions.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_DIR.parent
SHOP_DIR = PROJECT_ROOT / "sample-repo" / "shop"

# `codegraph` is a plain package under backend/, not an installed distribution
sys.path.insert(0, str(BACKEND_DIR))


@pytest.fixture(scope="session")
def shop_dir() -> Path:
    assert SHOP_DIR.is_dir(), f"fixture repo missing: {SHOP_DIR}"
    return SHOP_DIR


@pytest.fixture(scope="session")
def graph_json_path(shop_dir: Path, tmp_path_factory) -> Path:
    """Parse the shop fixture once; cache graph.json for the whole session."""
    from codegraph.parser import parse_repo

    graph = parse_repo(str(shop_dir))
    out = tmp_path_factory.mktemp("graph") / "shop-graph.json"
    out.write_text(json.dumps(graph), encoding="utf-8")
    return out


@pytest.fixture(scope="session")
def shop_graph(graph_json_path: Path) -> dict:
    return json.loads(graph_json_path.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def shop_store(graph_json_path: Path):
    from codegraph.graph_store import GraphStore

    return GraphStore(graph_json_path)
