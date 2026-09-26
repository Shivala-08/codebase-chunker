"""Parse a repo into a dependency graph.

Extracts nodes (modules, classes, functions) and edges (imports, calls,
contains) using tree-sitter, respecting .gitignore. Output matches the
graph.json schema from the TRD (§2.1).

v4: language support via a registry — dispatch on file extension, one
extractor per language, all producing the same node/edge schema so the
graph store, retrieval, agent, and docs layers stay language-agnostic.
Unknown extensions are skipped, not errored.
"""
from __future__ import annotations

import fnmatch
import json
import os
from pathlib import Path

import pathspec
from tree_sitter import Language, Parser

# ---------------------------------------------------------------------------
# Language registry (PRD v4 TRD §2)
#
# Each entry maps one or more file extensions to a grammar + extractor.
# Extractors share the contract: given (tree, source_bytes, rel_path) they
# append node dicts and (from, to, type) edge tuples to the lists passed in.
# Don't force one generic AST walker across languages — import/call/class
# syntax differs enough that five ~80-line functions beat one abstraction.
# ---------------------------------------------------------------------------

# Lazy-loaded grammars: import cost is paid once per language actually seen.
_grammar_cache: dict[str, Language] = {}
_parser_cache: dict[str, Parser] = {}


def _get_parser(grammar: str) -> Parser:
    if grammar not in _parser_cache:
        import importlib

        mod = importlib.import_module(f"tree_sitter_{grammar}")
        lang = Language(mod.language())
        _grammar_cache[grammar] = lang
        _parser_cache[grammar] = Parser(lang)
    return _parser_cache[grammar]


def _module_id(rel_path: Path) -> str:
    return rel_path.as_posix()


def _sym_id(rel_path: Path, name: str) -> str:
    return f"{rel_path.as_posix()}::{name}"


def _node_text(node, source: bytes) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _child_text(node, field: str, source: bytes) -> str | None:
    child = node.child_by_field_name(field)
    return _node_text(child, source) if child is not None else None


def _docstring(node, source: bytes) -> str | None:
    """First statement of a function/class body, if it's a string literal."""
    body = node.child_by_field_name("body")
    if body is None or not body.children:
        return None
    stmt = body.children[0]
    if stmt is not None and stmt.type == "expression_statement":
        inner = stmt.children[0] if stmt.children else None
        if inner is not None and inner.type == "string":
            text = _node_text(inner, source)
            for q in ('"""', "'''"):
                if text.startswith(q):
                    return text.strip(q).strip().strip('"\'').strip()
    return None


def _mk_node(rel: Path, ntype: str, name: str, qualname: str,
             node, docstring: str | None) -> dict:
    return {
        "id": _sym_id(rel, qualname),
        "type": ntype,
        "file": rel.as_posix(),
        "name": name,
        "qualname": qualname,
        "line_start": node.start_point[0] + 1,
        "line_end": node.end_point[0] + 1,
        "docstring": docstring,
    }


def _mk_module_node(rel: Path, source: bytes) -> dict:
    return {
        "id": _module_id(rel), "type": "module", "file": rel.as_posix(),
        "name": rel.name, "qualname": _module_name(rel),
        "line_start": 1, "line_end": source.count(b"\n") + 1,
        "docstring": None,
    }


def _module_name(rel: Path) -> str:
    """Dotted module name. `__init__.py` names its package; other extensions
    (e.g. Go's no-suffix packages) just use the stem."""
    parts = list(rel.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


# ---------------------------------------------------------------------------
# Extractor: Python
# ---------------------------------------------------------------------------

def extract_python(tree, source: bytes, rel: Path, nodes: list, contains: list) -> None:
    def walk(node, module_qualname: str, parent_is_class: bool):
        for child in node.children:
            kind = child.type
            if kind == "class_definition":
                name = _child_text(child, "name", source) or ""
                qualname = f"{module_qualname}.{name}" if module_qualname else name
                nodes.append(_mk_node(rel, "class", name, qualname, child, _docstring(child, source)))
                contains.append((_module_id(rel), _sym_id(rel, qualname), "contains"))
                walk(child, qualname, True)
            elif kind == "function_definition":
                name = _child_text(child, "name", source) or ""
                qualname = f"{module_qualname}.{name}" if module_qualname else name
                nodes.append(_mk_node(rel, "method" if parent_is_class else "function",
                                      name, qualname, child, _docstring(child, source)))
                contains.append((_module_id(rel), _sym_id(rel, qualname), "contains"))
                walk(child, qualname, False)
            elif kind not in ("string", "comment"):
                walk(child, module_qualname, parent_is_class)

    walk(tree.root_node, "", False)


def extract_python_imports(tree, source: bytes, rel: Path) -> list[dict]:
    """import / from-import edges, with relative-import normalization."""
    edges: list[dict] = []

    def normalize_import_mod(mod: str) -> str:
        if not mod.startswith("."):
            return mod
        level = len(mod) - len(mod.lstrip("."))
        rest = mod[level:]
        pkg_parts = list(rel.parent.parts)
        up = level - 1
        if up > 0:
            if up > len(pkg_parts):
                return ""
            pkg_parts = pkg_parts[:-up]
        parts = pkg_parts + (rest.split(".") if rest else [])
        return ".".join(p for p in parts if p)

    def visit(n):
        if n.type == "import_statement":
            for d in n.children:
                if d.type in ("dotted_name", "aliased_import"):
                    base = d.child_by_field_name("name") if d.type == "aliased_import" else d
                    mod = _node_text(base, source).split(".")[0]
                    edges.append({"from": _module_id(rel), "to": f"__module__:{mod}", "type": "imports"})
        elif n.type == "import_from_statement":
            mod_node = n.child_by_field_name("module_name")
            if mod_node is not None:
                mod = normalize_import_mod(_node_text(mod_node, source))
                edges.append({"from": _module_id(rel), "to": f"__module__:{mod}", "type": "imports"})
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


def extract_python_calls(tree, source: bytes, rel: Path) -> list[tuple[str, str]]:
    """(enclosing qualname, callee-name) pairs, attribute calls attributed to
    their method name (obj.method() -> method)."""
    result: list[tuple[str, str]] = []

    def visit(n, scope: str | None):
        new_scope = scope
        if n.type in ("function_definition", "class_definition"):
            name_node = n.child_by_field_name("name")
            if name_node is not None:
                nm = _node_text(name_node, source)
                new_scope = f"{scope}.{nm}" if scope else nm
        if n.type == "call":
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


# ---------------------------------------------------------------------------
# Extractor: JavaScript / TypeScript
# ---------------------------------------------------------------------------

_JS_DEF_TYPES = {
    "function_declaration": "function",
    "generator_function_declaration": "function",
    "class_declaration": "class",
    "method_definition": "method",
}

# shorthand function arrow assigned to an identifier
_JS_VAR_DEF_TYPES = {"variable_declarator", "lexical_declaration"}


def _js_walk_defs(node, source: bytes, rel: Path, module_qualname: str,
                  nodes: list, contains: list):
    for child in node.children:
        kind = child.type
        if kind in _JS_DEF_TYPES:
            name_node = child.child_by_field_name("name")
            name = _node_text(name_node, source) if name_node is not None else None
            if name:
                ntype = _JS_DEF_TYPES[kind]
                qualname = f"{module_qualname}.{name}" if module_qualname else name
                nodes.append(_mk_node(rel, ntype, name, qualname, child, None))
                contains.append((_module_id(rel), _sym_id(rel, qualname), "contains"))
                if kind == "class_declaration":
                    _js_walk_defs(child, source, rel, qualname, nodes, contains)
                continue
        elif kind in _JS_VAR_DEF_TYPES:
            # const foo = () => {} / const foo = function() {}
            for decl in (child.children if kind == "lexical_declaration" else [child]):
                if decl.type == "variable_declarator":
                    name_node = decl.child_by_field_name("name")
                    value = decl.child_by_field_name("value")
                    if (name_node is not None and value is not None
                            and value.type in ("arrow_function", "function_expression",
                                               "function")):
                        name = _node_text(name_node, source)
                        qualname = f"{module_qualname}.{name}" if module_qualname else name
                        nodes.append(_mk_node(rel, "function", name, qualname, decl, None))
                        contains.append((_module_id(rel), _sym_id(rel, qualname), "contains"))
        # class bodies: descend so methods are found
        if kind == "class_declaration" or kind == "class":
            _js_walk_defs(child, source, rel, module_qualname, nodes, contains)
        elif kind not in ("function_declaration", "generator_function_declaration",
                          "arrow_function", "function_expression", "function",
                          "statement_block"):
            _js_walk_defs(child, source, rel, module_qualname, nodes, contains)


def extract_js(tree, source: bytes, rel: Path, nodes: list, contains: list) -> None:
    _js_walk_defs(tree.root_node, source, rel, "", nodes, contains)


def extract_ts(tree, source: bytes, rel: Path, nodes: list, contains: list) -> None:
    # TS adds interface/type declarations — capture interfaces as classes-ish
    # nodes so cross-references don't dangle; type aliases stay unmodeled.
    for child in tree.root_node.children:
        if child.type == "interface_declaration":
            name = _child_text(child, "name", source)
            if name:
                nodes.append(_mk_node(rel, "class", name, name, child, None))
                contains.append((_module_id(rel), _sym_id(rel, name), "contains"))
    _js_walk_defs(tree.root_node, source, rel, "", nodes, contains)


def _js_imports(tree, source: bytes, rel: Path) -> list[dict]:
    edges: list[dict] = []
    for n in _walk_all(tree.root_node):
        if n.type == "import_statement":
            src = n.child_by_field_name("source")
            if src is not None:
                target = _node_text(src, source).strip("'\"")
                edges.append({"from": _module_id(rel), "to": f"__module__:{target}", "type": "imports"})
        elif n.type in ("call_expression",):
            fn = n.child_by_field_name("function")
            if fn is not None and _node_text(fn, source) == "require":
                args = n.child_by_field_name("arguments")
                if args is not None and args.named_child_count > 0:
                    arg = args.named_children[0]
                    if arg.type == "string":
                        target = _node_text(arg, source).strip("'\"")
                        edges.append({"from": _module_id(rel),
                                      "to": f"__module__:{target}", "type": "imports"})
    return edges


def _js_calls(tree, source: bytes, rel: Path) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []

    def scope_of(n, scope: str | None):
        kind = n.type
        if kind in _JS_DEF_TYPES:
            name_node = n.child_by_field_name("name")
            if name_node is not None:
                nm = _node_text(name_node, source)
                return f"{scope}.{nm}" if scope else nm
        if kind == "variable_declarator":
            name_node = n.child_by_field_name("name")
            value = n.child_by_field_name("value")
            if (name_node is not None and value is not None
                    and value.type in ("arrow_function", "function_expression", "function")):
                nm = _node_text(name_node, source)
                return f"{scope}.{nm}" if scope else nm
        return scope

    def visit(n, scope: str | None):
        new_scope = scope_of(n, scope)
        if n.type == "call_expression":
            fn = n.child_by_field_name("function")
            if fn is not None and scope is not None:
                if fn.type == "identifier":
                    result.append((scope, _node_text(fn, source)))
                elif fn.type == "member_expression":
                    prop = fn.child_by_field_name("property")
                    if prop is not None:
                        result.append((scope, _node_text(prop, source)))
        for c in n.children:
            visit(c, new_scope)

    visit(tree.root_node, None)
    return result


# ---------------------------------------------------------------------------
# Extractor: Java
# ---------------------------------------------------------------------------

def extract_java(tree, source: bytes, rel: Path, nodes: list, contains: list) -> None:
    def visit(node, class_qualname: str):
        for child in node.children:
            kind = child.type
            if kind == "class_declaration":
                name = _child_text(child, "name", source) or ""
                qualname = f"{class_qualname}.{name}" if class_qualname else name
                nodes.append(_mk_node(rel, "class", name, qualname, child, None))
                contains.append((_module_id(rel), _sym_id(rel, qualname), "contains"))
                visit(child, qualname)
            elif kind == "interface_declaration":
                name = _child_text(child, "name", source) or ""
                qualname = f"{class_qualname}.{name}" if class_qualname else name
                nodes.append(_mk_node(rel, "class", name, qualname, child, None))
                contains.append((_module_id(rel), _sym_id(rel, qualname), "contains"))
                visit(child, qualname)
            elif kind == "method_declaration":
                name = _child_text(child, "name", source) or ""
                qualname = f"{class_qualname}.{name}" if class_qualname else name
                nodes.append(_mk_node(rel, "method", name, qualname, child, None))
                contains.append((_module_id(rel), _sym_id(rel, qualname), "contains"))
            elif kind == "constructor_declaration":
                name = _child_text(child, "name", source) or ""
                qualname = f"{class_qualname}.{name}" if class_qualname else name
                nodes.append(_mk_node(rel, "method", name, qualname, child, None))
                contains.append((_module_id(rel), _sym_id(rel, qualname), "contains"))
            elif kind not in ("line_comment", "block_comment"):
                visit(child, class_qualname)

    visit(tree.root_node, "")


def _java_imports(tree, source: bytes, rel: Path) -> list[dict]:
    edges: list[dict] = []
    for n in _walk_all(tree.root_node):
        if n.type == "import_declaration":
            # import a.b.Class; or import a.b.*; — take the full dotted path
            text = _node_text(n, source)
            text = text.replace("import", "", 1).replace("static", "", 1)
            text = text.strip().rstrip(";").strip()
            if text:
                edges.append({"from": _module_id(rel), "to": f"__module__:{text}", "type": "imports"})
    return edges


def _java_calls(tree, source: bytes, rel: Path) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []

    def visit(n, scope: str | None):
        new_scope = scope
        if n.type in ("class_declaration", "interface_declaration",
                      "method_declaration", "constructor_declaration"):
            name_node = n.child_by_field_name("name")
            if name_node is not None:
                nm = _node_text(name_node, source)
                new_scope = f"{scope}.{nm}" if scope else nm
        if n.type == "method_invocation":
            name_node = n.child_by_field_name("name")
            if name_node is not None and scope is not None:
                result.append((scope, _node_text(name_node, source)))
        for c in n.children:
            visit(c, new_scope)

    visit(tree.root_node, None)
    return result


# ---------------------------------------------------------------------------
# Extractor: Go
# ---------------------------------------------------------------------------

def extract_go(tree, source: bytes, rel: Path, nodes: list, contains: list) -> None:
    for child in tree.root_node.children:
        kind = child.type
        if kind == "function_declaration":
            name = _child_text(child, "name", source) or ""
            nodes.append(_mk_node(rel, "function", name, name, child, None))
            contains.append((_module_id(rel), _sym_id(rel, name), "contains"))
        elif kind == "method_declaration":
            # func (r *Repo) Save() — receiver type + method name
            recv = child.child_by_field_name("receiver")
            name_node = child.child_by_field_name("name")
            if name_node is not None:
                name = _node_text(name_node, source)
                recv_type = ""
                if recv is not None:
                    # receiver field types may be pointer_receiver or type_identifier
                    for sub in _walk_all(recv):
                        if sub.type in ("type_identifier", "pointer_type"):
                            txt = _node_text(sub, source).lstrip("*")
                            if txt:
                                recv_type = txt
                                break
                qualname = f"{recv_type}.{name}" if recv_type else name
                nodes.append(_mk_node(rel, "method", name, qualname, child, None))
                contains.append((_module_id(rel), _sym_id(rel, qualname), "contains"))
        elif kind == "type_declaration":
            # type Foo struct{...} / interface{...}
            for spec in _walk_all(child):
                if spec.type == "type_spec":
                    name_node = spec.child_by_field_name("name")
                    value = spec.child_by_field_name("type")
                    if name_node is not None and value is not None and \
                            value.type in ("struct_type", "interface_type"):
                        name = _node_text(name_node, source)
                        nodes.append(_mk_node(rel, "class", name, name, spec, None))
                        contains.append((_module_id(rel), _sym_id(rel, name), "contains"))


def _go_imports(tree, source: bytes, rel: Path) -> list[dict]:
    edges: list[dict] = []
    for n in _walk_all(tree.root_node):
        if n.type == "import_declaration":
            for spec in _walk_all(n):
                if spec.type in ("import_spec",):
                    path_node = spec.child_by_field_name("path")
                    if path_node is not None:
                        target = _node_text(path_node, source).strip('"')
                        edges.append({"from": _module_id(rel), "to": f"__module__:{target}", "type": "imports"})
                elif spec.type == "import_spec_list":
                    continue
    return edges


def _go_calls(tree, source: bytes, rel: Path) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []

    def visit(n, scope: str | None):
        new_scope = scope
        if n.type in ("function_declaration", "method_declaration"):
            name_node = n.child_by_field_name("name")
            if name_node is not None:
                nm = _node_text(name_node, source)
                new_scope = f"{scope}.{nm}" if scope else nm
        if n.type == "call_expression":
            fn = n.child_by_field_name("function")
            if fn is not None and scope is not None:
                if fn.type == "identifier":
                    result.append((scope, _node_text(fn, source)))
                elif fn.type == "selector_expression":
                    field = fn.child_by_field_name("field")
                    if field is not None:
                        result.append((scope, _node_text(field, source)))
        for c in n.children:
            visit(c, new_scope)

    visit(tree.root_node, None)
    return result


# ---------------------------------------------------------------------------
# Extractor: C#  (P1)
# ---------------------------------------------------------------------------

def extract_csharp(tree, source: bytes, rel: Path, nodes: list, contains: list) -> None:
    def visit(node, ns_qualname: str):
        for child in node.children:
            kind = child.type
            if kind == "namespace_declaration":
                name = _child_text(child, "name", source) or ""
                qualname = f"{ns_qualname}.{name}" if ns_qualname else name
                visit(child, qualname)
            elif kind in ("class_declaration", "interface_declaration", "struct_declaration"):
                name = _child_text(child, "name", source) or ""
                qualname = f"{ns_qualname}.{name}" if ns_qualname else name
                nodes.append(_mk_node(rel, "class", name, qualname, child, None))
                contains.append((_module_id(rel), _sym_id(rel, qualname), "contains"))
                visit(child, qualname)
            elif kind in ("method_declaration", "constructor_declaration"):
                name = _child_text(child, "name", source) or ""
                qualname = f"{ns_qualname}.{name}" if ns_qualname else name
                nodes.append(_mk_node(rel, "method", name, qualname, child, None))
                contains.append((_module_id(rel), _sym_id(rel, qualname), "contains"))
            elif kind not in ("comment",):
                visit(child, ns_qualname)

    visit(tree.root_node, "")


def _csharp_imports(tree, source: bytes, rel: Path) -> list[dict]:
    edges: list[dict] = []
    for n in _walk_all(tree.root_node):
        if n.type == "using_directive":
            name = n.child_by_field_name("name")
            if name is not None:
                target = _node_text(name, source).rstrip(";").strip()
                if target:
                    edges.append({"from": _module_id(rel), "to": f"__module__:{target}", "type": "imports"})
    return edges


def _csharp_calls(tree, source: bytes, rel: Path) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []

    def visit(n, scope: str | None):
        new_scope = scope
        if n.type in ("class_declaration", "interface_declaration", "struct_declaration",
                      "method_declaration", "constructor_declaration", "namespace_declaration"):
            name_node = n.child_by_field_name("name")
            if name_node is not None and n.type != "namespace_declaration":
                nm = _node_text(name_node, source)
                new_scope = f"{scope}.{nm}" if scope else nm
        if n.type == "invocation_expression":
            fn = n.child_by_field_name("function")
            if fn is not None and scope is not None:
                if fn.type == "identifier":
                    result.append((scope, _node_text(fn, source)))
                else:
                    # member access: take the last identifier segment
                    parts = _node_text(fn, source).split(".")
                    result.append((scope, parts[-1]))
        for c in n.children:
            visit(c, new_scope)

    visit(tree.root_node, None)
    return result


# ---------------------------------------------------------------------------
# Extractor: C / C++  (P1 — best-effort; header resolution stays simple)
# ---------------------------------------------------------------------------

_C_DEF_TYPES = {"function_definition", "struct_specifier"}


def extract_c(tree, source: bytes, rel: Path, nodes: list, contains: list) -> None:
    def visit(node, scope: str):
        for child in node.children:
            kind = child.type
            if kind == "function_definition":
                decl = child.child_by_field_name("declarator")
                name = None
                if decl is not None:
                    # function_declarator -> declarator (identifier)
                    for sub in _walk_all(decl):
                        if sub.type == "function_declarator":
                            inner = sub.child_by_field_name("declarator")
                            if inner is not None and inner.type == "identifier":
                                name = _node_text(inner, source)
                                break
                            if inner is not None:
                                # pointer declarator: *foo
                                txt = _node_text(inner, source).lstrip("*").strip()
                                name = txt.split("(")[0].strip() or None
                                break
                if name:
                    qualname = f"{scope}.{name}" if scope else name
                    nodes.append(_mk_node(rel, "function", name, qualname, child, None))
                    contains.append((_module_id(rel), _sym_id(rel, qualname), "contains"))
                    visit(child, qualname)
            elif kind == "struct_specifier":
                name_node = child.child_by_field_name("name")
                if name_node is not None:
                    name = _node_text(name_node, source)
                    qualname = f"{scope}.{name}" if scope else name
                    nodes.append(_mk_node(rel, "class", name, qualname, child, None))
                    contains.append((_module_id(rel), _sym_id(rel, qualname), "contains"))
                visit(child, scope)
            elif kind not in ("comment", "preproc_comment"):
                visit(child, scope)

    visit(tree.root_node, "")


def extract_cpp(tree, source: bytes, rel: Path, nodes: list, contains: list) -> None:
    def visit(node, scope: str):
        for child in node.children:
            kind = child.type
            if kind == "function_definition":
                decl = child.child_by_field_name("declarator")
                name = None
                if decl is not None:
                    for sub in _walk_all(decl):
                        if sub.type == "function_declarator":
                            inner = sub.child_by_field_name("declarator")
                            if inner is not None:
                                txt = _node_text(inner, source)
                                # Class::method — take the method name, keep the scope
                                if "::" in txt:
                                    txt = txt.split("::")[-1]
                                name = txt.split("(")[0].strip().lstrip("*").strip() or None
                                break
                if name:
                    qualname = f"{scope}.{name}" if scope else name
                    nodes.append(_mk_node(rel, "function", name, qualname, child, None))
                    contains.append((_module_id(rel), _sym_id(rel, qualname), "contains"))
                    visit(child, qualname)
            elif kind in ("class_specifier", "struct_specifier"):
                name_node = child.child_by_field_name("name")
                if name_node is not None:
                    name = _node_text(name_node, source)
                    qualname = f"{scope}.{name}" if scope else name
                    nodes.append(_mk_node(rel, "class", name, qualname, child, None))
                    contains.append((_module_id(rel), _sym_id(rel, qualname), "contains"))
                    visit(child, qualname)
                else:
                    visit(child, scope)
            elif kind not in ("comment", "preproc_comment"):
                visit(child, scope)

    visit(tree.root_node, "")


def _c_like_imports(tree, source: bytes, rel: Path) -> list[dict]:
    """#include edges."""
    edges: list[dict] = []
    for n in _walk_all(tree.root_node):
        if n.type == "preproc_include":
            path_node = n.child_by_field_name("path")
            if path_node is not None:
                target = _node_text(path_node, source).strip('<"')
                edges.append({"from": _module_id(rel), "to": f"__module__:{target}", "type": "imports"})
    return edges


def _c_like_calls(tree, source: bytes, rel: Path) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []

    def visit(n, scope: str | None):
        new_scope = scope
        if n.type == "function_definition":
            decl = n.child_by_field_name("declarator")
            if decl is not None:
                for sub in _walk_all(decl):
                    if sub.type == "function_declarator":
                        inner = sub.child_by_field_name("declarator")
                        if inner is not None:
                            txt = _node_text(inner, source).split("::")[-1]
                            nm = txt.split("(")[0].strip().lstrip("*").strip()
                            if nm:
                                new_scope = f"{scope}.{nm}" if scope else nm
                        break
        if n.type == "call_expression":
            fn = n.child_by_field_name("function")
            if fn is not None and scope is not None:
                txt = _node_text(fn, source).split("::")[-1]
                if "." in txt:
                    txt = txt.split(".")[-1]
                result.append((scope, txt))
        for c in n.children:
            visit(c, new_scope)

    visit(tree.root_node, None)
    return result


# ---------------------------------------------------------------------------
# Extractor: Rust  (P2 — best-effort)
# ---------------------------------------------------------------------------

def extract_rust(tree, source: bytes, rel: Path, nodes: list, contains: list) -> None:
    def visit(node, scope: str):
        for child in node.children:
            kind = child.type
            if kind == "function_item":
                name = _child_text(child, "name", source)
                if name:
                    qualname = f"{scope}.{name}" if scope else name
                    nodes.append(_mk_node(rel, "function", name, qualname, child, None))
                    contains.append((_module_id(rel), _sym_id(rel, qualname), "contains"))
                    visit(child, qualname)
            elif kind in ("struct_item", "enum_item", "trait_item"):
                name = _child_text(child, "name", source)
                if name:
                    qualname = f"{scope}.{name}" if scope else name
                    nodes.append(_mk_node(rel, "class", name, qualname, child, None))
                    contains.append((_module_id(rel), _sym_id(rel, qualname), "contains"))
                    visit(child, qualname)
            elif kind == "impl_item":
                # impl Foo { fn bar } — methods under the impl'd type
                type_node = child.child_by_field_name("type")
                impl_name = _node_text(type_node, source) if type_node is not None else ""
                visit(child, f"{scope}.{impl_name}" if scope else impl_name)
            elif kind == "mod_item":
                name = _child_text(child, "name", source)
                if name:
                    visit(child, f"{scope}.{name}" if scope else name)
            elif kind not in ("line_comment", "block_comment"):
                visit(child, scope)

    visit(tree.root_node, "")


def _rust_imports(tree, source: bytes, rel: Path) -> list[dict]:
    edges: list[dict] = []
    for n in _walk_all(tree.root_node):
        if n.type == "use_declaration":
            arg = n.named_children[0] if n.named_children else None
            if arg is not None:
                target = _node_text(arg, source).lstrip(": ").strip()
                if target:
                    edges.append({"from": _module_id(rel), "to": f"__module__:{target}", "type": "imports"})
    return edges


def _rust_calls(tree, source: bytes, rel: Path) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []

    def visit(n, scope: str | None):
        new_scope = scope
        if n.type == "function_item":
            name = _child_text(n, "name", source)
            if name:
                new_scope = f"{scope}.{name}" if scope else name
        if n.type == "call_expression":
            fn = n.child_by_field_name("function")
            if fn is not None and scope is not None:
                if fn.type == "identifier":
                    result.append((scope, _node_text(fn, source)))
                elif fn.type in ("field_expression", "scoped_identifier"):
                    txt = _node_text(fn, source).split("::")[-1].split(".")[-1]
                    result.append((scope, txt))
        for c in n.children:
            visit(c, new_scope)

    visit(tree.root_node, None)
    return result


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

LANGUAGE_REGISTRY: dict[str, dict] = {
    ".py":   {"grammar": "python",     "extractor": extract_python,     "imports": extract_python_imports, "calls": extract_python_calls},
    ".js":   {"grammar": "javascript", "extractor": extract_js,         "imports": _js_imports,  "calls": _js_calls},
    ".jsx":  {"grammar": "javascript", "extractor": extract_js,         "imports": _js_imports,  "calls": _js_calls},
    ".mjs":  {"grammar": "javascript", "extractor": extract_js,         "imports": _js_imports,  "calls": _js_calls},
    ".cjs":  {"grammar": "javascript", "extractor": extract_js,         "imports": _js_imports,  "calls": _js_calls},
    ".ts":   {"grammar": "typescript", "extractor": extract_ts,         "imports": _js_imports,  "calls": _js_calls},
    ".tsx":  {"grammar": "typescript", "extractor": extract_ts,         "imports": _js_imports,  "calls": _js_calls},
    ".java": {"grammar": "java",       "extractor": extract_java,       "imports": _java_imports, "calls": _java_calls},
    ".go":   {"grammar": "go",         "extractor": extract_go,         "imports": _go_imports,  "calls": _go_calls},
    ".cs":   {"grammar": "c-sharp",    "extractor": extract_csharp,     "imports": _csharp_imports, "calls": _csharp_calls},
    ".c":    {"grammar": "c",          "extractor": extract_c,          "imports": _c_like_imports, "calls": _c_like_calls},
    ".h":    {"grammar": "c",          "extractor": extract_c,          "imports": _c_like_imports, "calls": _c_like_calls},
    ".cpp":  {"grammar": "cpp",        "extractor": extract_cpp,        "imports": _c_like_imports, "calls": _c_like_calls},
    ".cc":   {"grammar": "cpp",        "extractor": extract_cpp,        "imports": _c_like_imports, "calls": _c_like_calls},
    ".cxx":  {"grammar": "cpp",        "extractor": extract_cpp,        "imports": _c_like_imports, "calls": _c_like_calls},
    ".hpp":  {"grammar": "cpp",        "extractor": extract_cpp,        "imports": _c_like_imports, "calls": _c_like_calls},
    ".hh":   {"grammar": "cpp",        "extractor": extract_cpp,        "imports": _c_like_imports, "calls": _c_like_calls},
    ".rs":   {"grammar": "rust",       "extractor": extract_rust,       "imports": _rust_imports, "calls": _rust_calls},
}

# Environment override to disable languages whose wheels failed to install —
# e.g. CODEGRAPH_DISABLED_LANGUAGES="c-sharp,rust"
_disabled = {s.strip() for s in os.environ.get("CODEGRAPH_DISABLED_LANGUAGES", "").split(",") if s.strip()}


def _available_languages() -> set[str]:
    """Grammars that can actually be loaded (import worked)."""
    ok: set[str] = set()
    for ext, entry in LANGUAGE_REGISTRY.items():
        g = entry["grammar"]
        if g in _disabled or g in ok:
            continue
        try:
            _get_parser(g)
            ok.add(g)
        except Exception:
            ok.add(g)  # mark attempted; parse_repo will skip files needing it
            ok.discard(g)
            ok.add("__failed__:" + g)
    return ok


def discover_files(root: Path) -> list[Path]:
    """All files with a registered extension, .gitignore respected."""
    spec = _load_gitignore_spec(root)
    exts = set(LANGUAGE_REGISTRY)
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root)
        dirnames[:] = [
            d for d in dirnames
            if not spec.match_file(str(rel_dir / d) + "/") and not d.startswith(".")
        ]
        for fname in filenames:
            ext = Path(fname).suffix.lower()
            if ext not in exts:
                continue
            rel = rel_dir / fname
            if spec.match_file(str(rel)):
                continue
            files.append(root / rel)
    return sorted(files)


def _load_gitignore_spec(root: Path) -> pathspec.PathSpec:
    """Load root .gitignore, plus the always-on ignore set."""
    patterns = []
    gi = root / ".gitignore"
    if gi.is_file():
        patterns = gi.read_text(encoding="utf-8", errors="replace").splitlines()
    patterns += [".git/", "__pycache__/", ".venv/", "venv/", "env/", "*.egg-info/",
                 "node_modules/", "target/", "dist/", "build/", "bin/", "obj/"]
    return pathspec.PathSpec.from_lines("gitwildmatch", patterns)


def _walk_all(node):
    """Yield node and all descendants, pre-order."""
    yield node
    for c in node.children:
        yield from _walk_all(c)


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------

def parse_repo(repo_path: str) -> dict:
    root = Path(repo_path).resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"not a directory: {root}")

    files = discover_files(root)

    # Pass 1: nodes + module ids (registry-dispatched extractors)
    nodes: list[dict] = []
    module_files: dict[str, Path] = {}
    edges: list[dict] = []

    per_file: list[tuple[Path, object, bytes, str]] = []  # (rel, tree, source, grammar)
    for path in files:
        rel = path.relative_to(root)
        entry = LANGUAGE_REGISTRY.get(path.suffix.lower())
        if entry is None:
            continue
        try:
            parser = _get_parser(entry["grammar"])
        except Exception:
            continue  # grammar not installed; skip this language, don't fail the parse
        source = path.read_bytes()
        tree = parser.parse(source)
        per_file.append((rel, tree, source, entry["grammar"]))

        mod_id = _module_id(rel)
        nodes.append(_mk_module_node(rel, source))
        module_files[_module_name(rel)] = rel

        file_nodes: list[dict] = []
        contains: list[tuple[str, str, str]] = []
        entry["extractor"](tree, source, rel, file_nodes, contains)
        nodes.extend(file_nodes)
        edges.extend({"from": f, "to": t, "type": t_} for f, t, t_ in contains)

    def resolve_module(target: str) -> str | None:
        if target in module_files:
            return _module_id(module_files[target])
        parts = target.split(".")
        for i in range(len(parts), 0, -1):
            candidate = ".".join(parts[:i])
            if candidate in module_files:
                return _module_id(module_files[candidate])
        return None

    # Pass 2: imports (module + symbol level for Python; module level elsewhere)
    import_edges: list[dict] = []
    call_edges: list[dict] = []
    name_to_ids: dict[str, list[str]] = {}
    for n in nodes:
        if n["type"] != "module":
            name_to_ids.setdefault(n["name"], []).append(n["id"])

    for rel, tree, source, grammar in per_file:
        entry = LANGUAGE_REGISTRY[rel.suffix.lower()]
        if entry.get("imports") is None:
            continue
        for e in entry["imports"](tree, source, rel):
            if e["type"] == "imports":
                resolved = resolve_module(e["to"].replace("__module__:", ""))
                if resolved and resolved != e["from"]:
                    import_edges.append({"from": e["from"], "to": resolved, "type": "imports"})
            else:  # imports_symbol (Python only)
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

    # Pass 3: scoped call edges (same-file first, then global best-effort)
    for rel, tree, source, grammar in per_file:
        entry = LANGUAGE_REGISTRY[rel.suffix.lower()]
        if entry.get("calls") is None:
            continue
        rel_str = rel.as_posix()
        for scope, callee in entry["calls"](tree, source, rel):
            caller_id = _sym_id(rel, scope)
            if caller_id not in name_to_ids and caller_id not in {n["id"] for n in nodes}:
                # scope may be a method qualname (Cart.checkout); symbol_index
                # keyed by id — direct membership check on known ids
                pass
            if caller_id not in {n["id"] for n in nodes}:
                continue
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
