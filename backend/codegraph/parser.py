"""Parse a Python repo into a dependency graph.

Extracts nodes (modules, classes, functions) and edges (imports, calls,
contains) using tree-sitter, respecting .gitignore. Output matches the
graph.json schema from the TRD (§2.1).
"""
from __future__ import annotations

import fnmatch
import json
import os
from pathlib import Path

import pathspec
from tree_sitter import Language, Parser
import tree_sitter_python as tspython

PY_LANGUAGE = Language(tspython.language())
_parser = Parser(PY_LANGUAGE)


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------

def _load_gitignore_spec(root: Path) -> pathspec.PathSpec:
    """Load root .gitignore (plus nested ones get a cheap common-case fallback)."""
    patterns = []
    gi = root / ".gitignore"
    if gi.is_file():
        patterns = gi.read_text(encoding="utf-8", errors="replace").splitlines()
    # Always ignore virtualenvs / caches / hidden dirs even without .gitignore
    patterns += [".git/", "__pycache__/", ".venv/", "venv/", "env/", "*.egg-info/", "node_modules/"]
    return pathspec.PathSpec.from_lines("gitwildmatch", patterns)


def discover_python_files(root: Path) -> list[Path]:
    spec = _load_gitignore_spec(root)
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root)
        # prune ignored dirs in-place so os.walk doesn't descend
        dirnames[:] = [
            d for d in dirnames
            if not spec.match_file(str(rel_dir / d) + "/") and not d.startswith(".")
        ]
        for fname in filenames:
            if not fname.endswith(".py"):
                continue
            rel = rel_dir / fname
            if spec.match_file(str(rel)):
                continue
            files.append(root / rel)
    return sorted(files)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _node_text(node, source: bytes) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _docstring(node, source: bytes) -> str | None:
    """First statement of a function/class body, if it's a string literal."""
    body = node.child_by_field_name("body")
    if body is None:
        return None
    stmt = body.children[0] if body.children else None
    if stmt is not None and stmt.type == "expression_statement":
        inner = stmt.children[0] if stmt.children else None
        if inner is not None and inner.type == "string":
            text = _node_text(inner, source)
            # naive triple-quote strip; good enough for docstrings
            for q in ('"""', "'''"):
                if text.startswith(q):
                    return text.strip(q).strip().strip('"\'').strip()
    return None


def _module_id(rel_path: Path) -> str:
    return rel_path.as_posix()


def _sym_id(rel_path: Path, name: str) -> str:
    return f"{rel_path.as_posix()}::{name}"


# ---------------------------------------------------------------------------
# Per-file extraction
# ---------------------------------------------------------------------------

def _walk_definitions(node, source: bytes, rel: Path, module_qualname: str,
                      nodes: list[dict], contains: list[tuple[str, str, str]]):
    """Recursively extract classes/functions, tracking dotted qualnames."""
    for child in node.children:
        kind = child.type
        if kind == "class_definition":
            name = _node_text(child.child_by_field_name("name"), source)
            qualname = f"{module_qualname}.{name}" if module_qualname else name
            nodes.append({
                "id": _sym_id(rel, qualname),
                "type": "class",
                "file": rel.as_posix(),
                "name": name,
                "qualname": qualname,
                "line_start": child.start_point[0] + 1,
                "line_end": child.end_point[0] + 1,
                "docstring": _docstring(child, source),
            })
            if module_qualname:
                contains.append((_module_id(rel), _sym_id(rel, qualname), "contains"))
            _walk_definitions(child, source, rel, qualname, nodes, contains)
        elif kind == "function_definition":
            name = _node_text(child.child_by_field_name("name"), source)
            qualname = f"{module_qualname}.{name}" if module_qualname else name
            is_method = module_qualname != "" and "." in module_qualname
            nodes.append({
                "id": _sym_id(rel, qualname),
                "type": "method" if is_method else "function",
                "file": rel.as_posix(),
                "name": name,
                "qualname": qualname,
                "line_start": child.start_point[0] + 1,
                "line_end": child.end_point[0] + 1,
                "docstring": _docstring(child, source),
            })
            contains.append((_module_id(rel), _sym_id(rel, qualname), "contains"))
            _walk_definitions(child, source, rel, qualname, nodes, contains)
        # descend into anything else that can nest definitions
        elif kind not in ("string", "comment"):
            _walk_definitions(child, source, rel, module_qualname, nodes, contains)


def _module_name(rel: Path) -> str:
    parts = list(rel.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _normalize_import_mod(mod: str, rel: Path) -> str:
    """Turn a (possibly relative) import target into a dotted module name.

    `from .classes import Graph` in networkx/classes/graph.py → networkx.classes
    (1 dot = the file's own package, each extra dot climbs one level).
    """
    if not mod.startswith("."):
        return mod
    level = len(mod) - len(mod.lstrip("."))
    rest = mod[level:]
    pkg_parts = list(rel.parent.parts)  # the file's package
    up = level - 1
    if up > 0:
        if up > len(pkg_parts):
            return ""
        pkg_parts = pkg_parts[:-up]
    parts = pkg_parts + (rest.split(".") if rest else [])
    return ".".join(p for p in parts if p)


def _extract_imports(tree, source: bytes, rel: Path) -> list[dict]:
    """Return edges file -> target for import/from-import statements."""
    edges: list[dict] = []

    def visit(n):
        if n.type == "import_statement":
            # import a.b.c -> edge to module a/b/c.py (best-effort: last module segment)
            for d in n.children:
                if d.type == "dotted_name" or d.type == "aliased_import":
                    base = d.child_by_field_name("name") if d.type == "aliased_import" else d
                    mod = _node_text(base, source).split(".")[0]
                    edges.append({"from": _module_id(rel), "to": f"__module__:{mod}", "type": "imports"})
        elif n.type == "import_from_statement":
            mod_node = n.child_by_field_name("module_name")
            if mod_node is not None:
                mod = _normalize_import_mod(_node_text(mod_node, source), rel)
                edges.append({"from": _module_id(rel), "to": f"__module__:{mod}", "type": "imports"})
                # from x import a, b -> also record imported names for call resolution
                for d in n.children:
                    if d.type in ("dotted_name", "aliased_import") and d is not mod_node:
                        base = d.child_by_field_name("name") if d.type == "aliased_import" else d
                        name = _node_text(base, source).split(".")[0]
                        edges.append({"from": _module_id(rel),
                                      "to": f"__fromimport__:{mod}.{name}",
                                      "type": "imports_symbol"})
        for c in n.children:
            visit(c)

    visit(tree.root_node)
    return edges


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------

def parse_repo(repo_path: str) -> dict:
    root = Path(repo_path).resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"not a directory: {root}")

    files = discover_python_files(root)

    # Pass 1: nodes + module ids
    nodes: list[dict] = []
    module_files: dict[str, Path] = {}          # module name -> file path
    edges: list[dict] = []

    per_file: list[tuple[Path, object, bytes]] = []
    for path in files:
        rel = path.relative_to(root)
        source = path.read_bytes()
        tree = _parser.parse(source)
        per_file.append((rel, tree, source))

        mod_id = _module_id(rel)
        nodes.append({
            "id": mod_id, "type": "module", "file": rel.as_posix(),
            "name": rel.name, "qualname": _module_name(rel),
            "line_start": 1, "line_end": source.count(b"\n") + 1,
            "docstring": None,
        })
        module_files[_module_name(rel)] = rel

        file_nodes: list[dict] = []
        contains: list[tuple[str, str, str]] = []
        _walk_definitions(tree.root_node, source, rel, "", file_nodes, contains)
        nodes.extend(file_nodes)
        edges.extend({"from": f, "to": t, "type": t_} for f, t, t_ in contains)

    # map "module" / "module.symbol" names -> node ids for import resolution
    def resolve_module(target: str) -> str | None:
        if target in module_files:
            return _module_id(module_files[target])
        # try package-style prefix match (a.b.c -> a/b/c/__init__.py or a/b/c.py)
        parts = target.split(".")
        for i in range(len(parts), 0, -1):
            candidate = ".".join(parts[:i])
            if candidate in module_files:
                return _module_id(module_files[candidate])
        return None

    # Pass 2: imports (module + symbol level) and scoped call edges
    import_edges: list[dict] = []
    call_edges: list[dict] = []
    name_to_ids: dict[str, list[str]] = {}
    for n in nodes:
        if n["type"] != "module":
            name_to_ids.setdefault(n["name"], []).append(n["id"])

    for rel, tree, source in per_file:
        for e in _extract_imports(tree, source, rel):
            if e["type"] == "imports":
                resolved = resolve_module(e["to"].replace("__module__:", ""))
                if resolved and resolved != e["from"]:
                    import_edges.append({"from": e["from"], "to": resolved, "type": "imports"})
            else:  # imports_symbol: __fromimport__:mod.sym -> resolve sym in that module
                qual = e["to"].replace("__fromimport__:", "")
                mod, _, sym = qual.rpartition(".")
                target_mod = resolve_module(mod) if mod else None
                candidates = name_to_ids.get(sym, [])
                chosen = None
                for cid in candidates:
                    if target_mod and cid.startswith(target_mod + "::"):
                        chosen = cid
                        break
                if chosen is None and candidates and not mod:
                    chosen = candidates[0]
                if chosen:
                    import_edges.append({"from": e["from"], "to": chosen, "type": "imports_symbol"})

    edges.extend(import_edges)

    # Scoped call edges: attribute each call site to the innermost enclosing
    # function/class (same-file resolution first, then import-resolved best-effort).
    def scoped_calls(rel: Path, tree, source: bytes):
        result: list[tuple[str, str]] = []  # (caller_qualname, callee_name)

        def visit(n, scope: str | None):
            kind = n.type
            new_scope = scope
            if kind in ("function_definition", "class_definition"):
                name_node = n.child_by_field_name("name")
                if name_node is not None:
                    nm = _node_text(name_node, source)
                    new_scope = f"{scope}.{nm}" if scope else nm
            if kind == "call":
                func = n.child_by_field_name("function")
                if func is not None and scope is not None:
                    if func.type == "identifier":
                        result.append((scope, _node_text(func, source)))
                    elif func.type == "attribute":
                        attr = func.child_by_field_name("attribute")
                        if attr is not None:
                            result.append((scope, _node_text(attr, source)))
            for c in n.children:
                visit(c, new_scope)

        visit(tree.root_node, None)
        return result

    symbol_index: dict[str, str] = {n["id"]: n["name"] for n in nodes if n["type"] != "module"}

    for rel, tree, source in per_file:
        rel_str = rel.as_posix()
        for scope, callee in scoped_calls(rel, tree, source):
            caller_id = _sym_id(rel, scope)
            if caller_id not in symbol_index:
                continue
            # same-file first, then any definition anywhere
            target = None
            for cid in name_to_ids.get(callee, []):
                if cid.startswith(rel_str + "::") and cid != caller_id:
                    target = cid
                    break
            if target is None:
                for cid in name_to_ids.get(callee, []):
                    if cid != caller_id:
                        target = cid
                        break
            if target:
                call_edges.append({"from": caller_id, "to": target, "type": "calls"})

    # dedupe edges
    seen: set[tuple[str, str, str]] = set()
    deduped = []
    for e in edges + call_edges:
        key = (e["from"], e["to"], e["type"])
        if key not in seen and e["from"] != e["to"]:
            seen.add(key)
            deduped.append(e)

    graph = {"repo": str(root), "nodes": nodes, "edges": deduped}
    return graph


def write_graph(graph: dict, out_path: str | Path) -> Path:
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(graph, indent=1), encoding="utf-8")
    return out


if __name__ == "__main__":
    import sys
    repo = sys.argv[1] if len(sys.argv) > 1 else "."
    out = sys.argv[2] if len(sys.argv) > 2 else "graph.json"
    g = parse_repo(repo)
    p = write_graph(g, out)
    n_nodes, n_edges = len(g["nodes"]), len(g["edges"])
    print(f"parsed {g['repo']}: {n_nodes} nodes, {n_edges} edges -> {p}")
