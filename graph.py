"""
graph.py — Graph rendering for Residuality
Two-level visualization:
  Level 1: File dependency graph (files + import edges)
  Level 2: File detail (functions, classes, imports, __main__)
"""

import re
import posixpath
import logging
from pathlib import Path
from typing import Optional

import os

logger = logging.getLogger(__name__)

# ── Dot styling ───────────────────────────────────────────────────────────

GRAPH_ATTRS = """
    graph [bgcolor="#0d1117" rankdir=LR fontname="Courier New" pad=0.5 nodesep=0.4];
    node  [fontname="Courier New" fontsize=11 fontcolor="#c9d1d9"
           style=filled color="#30363d" shape=box];
    edge  [color="#30363d" fontname="Courier New" fontsize=9 fontcolor="#8b949e"];
"""

# rankdir=LR is what makes a folder read like the flat file graph: the files
# nothing else in the folder imports land in the first column and their
# imports fan out to their right, stacked vertically. rank=same therefore
# means "vertical stack" here, which is exactly what the child-directory
# column wants. newrank=true lets a rank=same span the cluster boundary, so
# the child stack can be parked beside the folder's own files.
DIR_GRAPH_ATTRS = """
    graph [bgcolor="#0d1117" rankdir=LR newrank=true compound=false
           ranksep=0.9 nodesep=0.28 fontname="Courier New" pad=0.1];
    node  [fontname="Courier New" fontsize=11 fontcolor="#c9d1d9"
           style=filled color="#30363d" shape=box];
    edge  [color="#484f58" fontname="Courier New" fontsize=9 fontcolor="#8b949e"
           arrowsize=0.7];
"""

# Only these leaf files get a "leaf" type in graph.dot.
LEAF_FILE_TYPES = ("file", "chapter")

# Probed when an import is recorded without an extension (the JS/TS norm, and
# common in Python's relative imports too).
_MODULE_EXTS = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".py",
                ".c", ".h", ".cpp", ".hpp", ".rs", ".json")

NODE_COLORS = {
    "file":     ("#1a2e1a", "#2ea043"),   # green
    "chapter":  ("#1a2e1a", "#2ea043"),
    "class":    ("#1a2035", "#388bfd"),   # blue
    "function": ("#161b22", "#58a6ff"),   # lighter blue
    "section":  ("#2e2a1a", "#9e6a03"),   # amber
    "import":   ("#21262d", "#6e7681"),   # grey
    "main":     ("#2e1a2e", "#a371f7"),   # purple
    "external": ("#1a1a1a", "#484f58"),   # dark grey
    "dir":       ("#1c2128", "#58a6ff"),   # directory label
    "dir_focus": ("#1a2035", "#388bfd"),   # directory being drilled into
}


def _node_color(node_type: str) -> tuple:
    return NODE_COLORS.get(node_type, ("#161b22", "#30363d"))


# ── Dot parsing ───────────────────────────────────────────────────────────

class _DotNode:
    """Stand-in for a pydot node: a name and an attribute dict.

    Names and attribute values keep their surrounding quotes, exactly as pydot
    reports them, so every existing .strip('"') call site is unaffected.
    """
    __slots__ = ("_name", "_attrs")

    def __init__(self, name: str, attrs: dict):
        self._name, self._attrs = name, attrs

    def get_name(self) -> str:
        return self._name

    def get_attributes(self) -> dict:
        return self._attrs


class _DotEdge:
    __slots__ = ("_src", "_dst", "_attrs")

    def __init__(self, src: str, dst: str, attrs: dict):
        self._src, self._dst, self._attrs = src, dst, attrs

    def get_source(self) -> str:
        return self._src

    def get_destination(self) -> str:
        return self._dst

    def get_attributes(self) -> dict:
        return self._attrs


class _DotGraph:
    __slots__ = ("_nodes", "_edges")

    def __init__(self, nodes: list, edges: list):
        self._nodes, self._edges = nodes, edges

    def get_nodes(self) -> list:
        return self._nodes

    def get_edges(self) -> list:
        return self._edges


def _unquote(s: str, i: int):
    """s[i] == '"'. Return (raw token incl. the quotes, index just past it)."""
    j, n = i + 1, len(s)
    while j < n:
        if s[j] == "\\":
            j += 2
            continue
        if s[j] == '"':
            return s[i:j + 1], j + 1
        j += 1
    return s[i:], n


def _find_arrow(s: str) -> int:
    """Index of the '->' that is not inside a quoted string, else -1."""
    i, n = 0, len(s)
    while i < n:
        if s[i] == '"':
            _, i = _unquote(s, i)
            continue
        if s[i] == "-" and i + 1 < n and s[i + 1] == ">":
            return i
        i += 1
    return -1


def _strip_dot_comment(line: str) -> str:
    """Drop a trailing // comment, ignoring // inside a quoted string."""
    i, n = 0, len(line)
    while i < n:
        if line[i] == '"':
            _, i = _unquote(line, i)
            continue
        if line[i] == "/" and i + 1 < n and line[i + 1] == "/":
            return line[:i]
        i += 1
    return line


def _parse_attr_list(blob: str) -> dict:
    """'a="b", c=1' -> {'a': '"b"', 'c': '1'}, quotes preserved."""
    out, i, n = {}, 0, len(blob)
    while i < n:
        while i < n and blob[i] in " \t,":
            i += 1
        if i >= n:
            break
        j = i
        while j < n and (blob[j].isalnum() or blob[j] in "_:"):
            j += 1
        key = blob[i:j]
        while j < n and blob[j] in " \t":
            j += 1
        if j < n and blob[j] == "=":
            j += 1
            while j < n and blob[j] in " \t":
                j += 1
            if j < n and blob[j] == '"':
                val, j = _unquote(blob, j)
            else:
                k = j
                while k < n and blob[k] not in ", \t":
                    k += 1
                val, j = blob[j:k], k
            if key:
                out[key] = val
        i = j if j > i else i + 1
    return out


def _parse_dot_file(dot_path: str):
    """Read a dot file as one statement per line.

    Every writer emits exactly one node or edge per line, so a line walk is
    sufficient -- a general DOT parser buys nothing here. Blank lines, `//`
    comments, the `digraph {`/`}` braces and the graph/node/edge default
    statements are skipped, because no caller reads them.
    """
    nodes, edges = [], []
    with open(dot_path, "r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = _strip_dot_comment(raw).strip()
            if not line.startswith('"') or not line.endswith(";"):
                continue
            body = line[:-1].rstrip()
            lb   = body.find("[")
            head = body if lb < 0 else body[:lb].rstrip()
            attrs = {}
            if lb >= 0:
                rb = body.rfind("]")
                if rb > lb:
                    attrs = _parse_attr_list(body[lb + 1:rb])
            arrow = _find_arrow(head)
            if arrow >= 0:
                src, _ = _unquote(head, 0)
                dq = head.find('"', arrow)
                if dq < 0:
                    continue
                dst, _ = _unquote(head, dq)
                edges.append(_DotEdge(src, dst, attrs))
            else:
                name, _ = _unquote(head, 0)
                nodes.append(_DotNode(name, attrs))
    return nodes, edges


# Parsed graphs, keyed on (path, mtime_ns, size) so an edit invalidates it.
_DOT_CACHE: dict = {}
_DOT_CACHE_MAX = 8


def load_graph(dot_path: str) -> Optional[_DotGraph]:
    """Parse graph.dot into a read-only graph.

    Hand-rolled rather than pydot. pydot spends ~2.3s on this repo's 147 KB /
    554-node dot file building an object tree per statement, and *every*
    graph request used to pay that: each drill-down, each file detail, each
    node pop-up. This walk is ~25ms on the same file and yields identical
    node names and attribute dicts.

    Memoised on (path, mtime_ns, size), so repeated requests against an
    unchanged graph skip the parse entirely.
    """
    try:
        st = os.stat(dot_path)
    except OSError:
        return None
    key = (dot_path, st.st_mtime_ns, st.st_size)
    cached = _DOT_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        nodes, edges = _parse_dot_file(dot_path)
    except Exception as e:
        logger.warning(f"Failed to load graph: {e}")
        return None
    graph = _DotGraph(nodes, edges)
    if len(_DOT_CACHE) >= _DOT_CACHE_MAX:
        _DOT_CACHE.clear()
    _DOT_CACHE[key] = graph
    logger.info(f"loaded {dot_path}: {len(nodes)} nodes {len(edges)} edges")
    return graph


def _attr(node, key: str) -> str:
    return node.get_attributes().get(key, "").strip('"')


def get_node_by_id(graph, node_id: str) -> Optional[dict]:
    for node in graph.get_nodes():
        if node.get_name().strip('"') == node_id:
            attrs = node.get_attributes()
            return {
                "id":         node_id,
                "type":       attrs.get("type",      "").strip('"'),
                "file":       attrs.get("file",      "").strip('"'),
                "line_start": int(attrs.get("line_start", 0)),
                "line_end":   int(attrs.get("line_end",   0)),
                "signature":  attrs.get("signature", "").strip('"'),
                "label":      attrs.get("label",     "").strip('"'),
            }
    return None


def get_neighbors(graph, node_id: str) -> dict:
    parents, children, calls, called_by = [], [], [], []
    for edge in graph.get_edges():
        src = edge.get_source().strip('"')
        dst = edge.get_destination().strip('"')
        lbl = edge.get_attributes().get("label", "").strip('"')
        if dst == node_id:
            if lbl == "contains":  parents.append(src)
            elif lbl == "calls":   called_by.append(src)
        if src == node_id:
            if lbl == "contains":  children.append(dst)
            elif lbl in ("calls", "imports"): calls.append(dst)
    return {"parents": parents, "children": children,
            "calls": calls, "called_by": called_by}


# Trailing dot-segments that are file extensions, not module components.
_FILENAME_TAILS = {
    "h", "hh", "hpp", "hxx", "inc", "c", "cc", "cpp", "cxx", "rs",
    "py", "pyi", "js", "mjs", "cjs", "jsx", "ts", "tsx", "json",
    "html", "htm", "xhtml",
}


def _import_label(name: str) -> str:
    """Shorten an import target for display.

      'os.path'    -> 'path'      python module namespace
      'stdio.h'    -> 'stdio.h'   C include: '.h' is an extension, not a scope
      'sys/stat.h' -> 'sys/stat.h'
      'base.html'  -> 'base.html' Jinja extends/include target

    A naive `split('.')[-1]` reduced every C `#include` to its extension, so
    the whole import column in a C file's detail view read 'h'.
    """
    if "/" in name or "." not in name:
        return name
    tail = name.rsplit(".", 1)[1]
    return name if tail.lower() in _FILENAME_TAILS else tail


# ── Level 1: File dependency graph ────────────────────────────────────────

def render_file_graph(dot_path: str, show_external: bool = False) -> str:
    graph = load_graph(dot_path)
    if not graph:
        return "<p style='color:#f85149'>No graph.dot found — click ⚙ Build Graph first.</p>"

    # Collect project files
    project_files = set()
    for node in graph.get_nodes():
        ntype = _attr(node, "type")
        if ntype in ("file", "chapter"):
            project_files.add(node.get_name().strip('"'))

    # Build lookup: module_name → filename (e.g. "bible" → "bible.py")
    module_to_file = {}
    for f in project_files:
        stem = f.replace(".py", "").replace(".md", "")
        module_to_file[stem] = f
        module_to_file[f]    = f  # also match exact filename

    # Collect import edges
    internal_edges = []  # (from_file, to_file, label)
    external_edges = []  # (from_file, to_package, label)
    for edge in graph.get_edges():
        lbl = edge.get_attributes().get("label", "").strip('"')
        if lbl != "imports":
            continue
        src = edge.get_source().strip('"')
        dst = edge.get_destination().strip('"')
        if src not in project_files:
            continue

        # Edge label = the imported module name
        edge_label = _import_label(dst)

        resolved = module_to_file.get(dst)
        if resolved:
            internal_edges.append((src, resolved, edge_label))
        else:
            external_edges.append((src, dst, edge_label))

    # Build dot
    lines = ["digraph residuality {", GRAPH_ATTRS]

    # Project file nodes — always visible, clickable
    for f in sorted(project_files):
        label = f.split("/")[-1]
        fill, border = _node_color("file")
        lines.append(
            f'    "{f}" [label="{label}" fillcolor="{fill}" color="{border}" '
            f'tooltip="Click to expand {label}" '
            f'href="javascript:void(0)"];'
        )

    # Internal import edges — labeled with module name
    for src, dst, label in internal_edges:
        lines.append(f'    "{src}" -> "{dst}" [label="{label}" fontsize=9];')

    # External package nodes + edges — only if show_external
    if show_external:
        external_pkgs = set(dst for _, dst, _ in external_edges)
        for pkg in sorted(external_pkgs):
            fill, border = _node_color("external")
            lines.append(
                f'    "{pkg}" [label="{pkg}" fillcolor="{fill}" '
                f'color="{border}" fontsize=9 shape=ellipse];'
            )
        for src, dst, label in external_edges:
            lines.append(
                f'    "{src}" -> "{dst}" [label="{label}" fontsize=8 '
                f'style=dashed color="#484f58" fontcolor="#484f58"];'
            )

    lines.append("}")
    return _render_dot("\n".join(lines))


# ── Level 2: File detail graph ────────────────────────────────────────────

def render_file_detail(dot_path: str, filepath: str) -> str:
    """
    Render the internal structure of a single file.
    Shows: imports section, classes, functions, __main__ block.
    """
    graph = load_graph(dot_path)
    if not graph:
        return "<p style='color:#f85149'>Graph not found.</p>"

    lines = [f'digraph "{filepath}" {{', GRAPH_ATTRS]

    # Find all nodes belonging to this file
    file_nodes   = []
    import_targets = []

    for node in graph.get_nodes():
        attrs    = node.get_attributes()
        nid      = node.get_name().strip('"')
        ntype    = attrs.get("type", "").strip('"')
        nfile    = attrs.get("file", "").strip('"')
        sig      = attrs.get("signature", "").strip('"')
        label_v  = attrs.get("label", "").strip('"')

        # File node itself
        if nid == filepath and ntype in ("file", "chapter"):
            fill, border = _node_color("file")
            short = filepath.split("/")[-1]
            lines.append(
                f'    "{nid}" [label="{short}" fillcolor="{fill}" '
                f'color="{border}" shape=folder];'
            )
            file_nodes.append(nid)

        # Nodes inside this file
        elif nfile == filepath:
            display = sig or label_v or nid.split("::")[-1]

            # Detect __main__
            if "__main__" in nid or "main" == nid.split("::")[-1].lower():
                ntype = "main"
                display = "if __name__ == '__main__'"

            fill, border = _node_color(ntype)
            ls = attrs.get("line_start", "").strip('"')
            le = attrs.get("line_end",   "").strip('"')
            tooltip = f"{nid} lines {ls}-{le}" if ls else nid
            lines.append(
                f'    "{nid}" [label="{display}" fillcolor="{fill}" '
                f'color="{border}" tooltip="{tooltip}"];'
            )
            file_nodes.append(nid)

    # Edges involving this file's nodes
    for edge in graph.get_edges():
        src = edge.get_source().strip('"')
        dst = edge.get_destination().strip('"')
        lbl = edge.get_attributes().get("label", "").strip('"')

        if src in file_nodes or dst in file_nodes:
            if lbl == "imports":
                # Show import target as external node if not already in file_nodes
                if dst not in file_nodes:
                    fill, border = _node_color("external")
                    short = _import_label(dst)
                    lines.append(
                        f'    "{dst}" [label="{short}" fillcolor="{fill}" '
                        f'color="{border}" fontsize=9];'
                    )
                lines.append(f'    "{src}" -> "{dst}" [label="imports" style=dashed];')
            elif lbl == "contains" and src in file_nodes and dst in file_nodes:
                lines.append(f'    "{src}" -> "{dst}" [label="contains"];')

    lines.append("}")
    return _render_dot("\n".join(lines))


# ── Directory drill-down graph ────────────────────────────────────────────
#
# A second, directory-shaped view of the same graph.dot. Instead of one flat
# canvas of every file, the canvas is built around the *active directory* P:
#
#   left     P's label, with its files laid out to the right by their imports
#   right    P's child directories, stacked and clickable to drill in
#
# P's ancestors are deliberately NOT in the canvas: they are an HTML
# breadcrumb above it, so the graph never spends a column on the path up.
#
# Only P's files are drawn, which is why the static/*.js files do not appear
# at the root — they belong to `static` and show up when it is opened.
# Siblings that are not on P's path are absent by construction: open `static`
# and `templates` has nowhere to live.
#
# Directory nodes are named with a trailing slash ("./", "static/",
# "src/helper/") so the page JS can tell a drill target from a leaf file node
# by inspecting the node's <title>; leaf nodes keep their plain repo path.
# graphviz puts the node NAME in <title> and the tooltip in xlink:title, so
# tooltips are free-form and the click wiring keys off <title>.

def _dir_of(filepath: str) -> str:
    return filepath.rsplit("/", 1)[0] if "/" in filepath else ""


def _dir_chain(directory: str) -> list:
    """'' -> ['']; 'src/helper' -> ['', 'src', 'src/helper']."""
    chain = [""]
    if directory:
        parts = directory.split("/")
        chain.extend("/".join(parts[:i + 1]) for i in range(len(parts)))
    return chain


def _dir_node(directory: str) -> str:
    return "./" if directory == "" else directory + "/"


def _dir_label(directory: str) -> str:
    return "./" if directory == "" else directory.rsplit("/", 1)[-1] + "/"


def _q(text) -> str:
    """Escape a string for a double-quoted DOT attribute."""
    return str(text).replace("\\", "\\\\").replace('"', '\\"')


def _html(text) -> str:
    """Escape text for a graphviz HTML-like label (angle-bracket, not quoted)."""
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _dir_label_html(label: str, border: str) -> str:
    """The active directory's name as a cluster label.

    Returned *unquoted*: graphviz HTML-like labels are wrapped in <...>, so
    this is the whole `label=<<TABLE...>>` right-hand side. A one-cell table
    rather than bare text so the directory keeps the boxed, tab-like look it
    had as a node.
    """
    return ('<<TABLE BORDER="1" CELLBORDER="0" CELLSPACING="0" '
            'CELLPADDING="4" COLOR="%s" BGCOLOR="#161b22">'
            '<TR><TD><FONT FACE="Courier New" POINT-SIZE="11" '
            'COLOR="#c9d1d9">%s</FONT></TD></TR></TABLE>>'
            % (border, _html(label)))


def _index_repo_files(files) -> dict:
    """{dir: [sorted file paths]} for every tracked, non-ignored file."""
    by_dir: dict = {}
    for f in files or []:
        f = (f or "").strip().strip("/")
        if not f or f.startswith(".residuality/"):
            continue
        by_dir.setdefault(_dir_of(f), []).append(f)
    for d in list(by_dir):
        by_dir[d] = sorted(set(by_dir[d]))
    return by_dir


def _all_dirs(by_dir: dict) -> set:
    dirs: set = set()
    for d in by_dir:
        dirs.update(_dir_chain(d))
    return dirs


def _child_dirs(directory: str, dirs: set) -> list:
    prefix = directory + "/" if directory else ""
    kids = [
        d for d in dirs
        if d != directory and d.startswith(prefix)
        and "/" not in d[len(prefix):]
    ]
    return sorted(kids)


def _build_import_index(known: set, dirs: set):
    """Filename, extensionless-stem and package-path indexes."""
    stems: dict = {}
    bases: dict = {}
    dir_keys: dict = {}
    for f in known:
        base = f.rsplit("/", 1)[-1]
        bases.setdefault(base, []).append(f)
        stem = base.rsplit(".", 1)[0] if "." in base else base
        stems.setdefault(stem, []).append(f)
    for d in dirs:
        if not d:
            continue
        dir_keys.setdefault(d, d)
        dir_keys.setdefault(d.replace("/", "."), d)
        dir_keys.setdefault(d.replace("/", "::"), d)
    return stems, bases, dir_keys


def _pick_candidate(cands, *preferred_dirs):
    """Least-arbitrary match: same dir as the importer, then the focus, then A-Z."""
    if not cands:
        return None
    if len(cands) == 1:
        return cands[0]
    for d in preferred_dirs:
        near = sorted(c for c in cands if _dir_of(c) == d)
        if near:
            return near[0]
    return sorted(cands)[0]


def resolve_import(raw_target: str, importer_dir: str, current_dir: str,
                  stems: dict, bases: dict, dir_keys: dict, known: set):
    """Map a raw import string from graph.dot onto a repo file or package dir.

    Covers the spellings the parsers leave behind:
      'artifact'         python `import artifact`         -> artifact.py
      'src.helper'       python `from src.helper imp x`   -> src/helper/
      'cJSON.h'          C `#include "cJSON.h"`           -> cJSON.h
      'stdio.h'          C `#include <stdio.h>`           -> None (external)
      './residuality.js' JS relative import               -> static/residuality.js
      'crate::foo::Bar'  rust `use crate::foo::Bar`       -> None (external)

    Returns ("file", path), ("dir", package_dir) or (None, None) if the target
    is outside the repo — the caller decides whether to render an external stub.
    """
    t = (raw_target or "").strip().strip("'\"<>")
    if not t:
        return (None, None)

    # A URL with a scheme (or protocol-relative) is never a repo path. It has
    # to be caught before the bare-filename fallback below, or a CDN copy of a
    # vendored bundle -- 'https://unpkg.com/.../react.production.min.js' --
    # draws an edge to the local file of the same name and the page reads as
    # depending on a bundle it never loads.
    if re.match(r"[A-Za-z][A-Za-z0-9+.\-]*://", t) or t.startswith("//"):
        return (None, None)

    # Relative-path import (JS/TS): resolve against the importing file's dir.
    # A '/'-rooted target is ambiguous: in an HTML asset it is repo-root
    # relative ('/static/residuality.js'), while in a JS bundle it is relative
    # to the importing file ('/helpers/util.js'). Try the root reading first
    # and keep the old one as the fallback, so nothing that resolves today
    # stops resolving.
    if t.startswith("/"):
        cands = [posixpath.normpath(t.lstrip("/")),
                 posixpath.normpath(posixpath.join(importer_dir, t.lstrip("/")))]
    elif t.startswith("."):
        cands = [posixpath.normpath(posixpath.join(importer_dir, t))]
    else:
        cands = []
    if cands:
        for cand in cands:
            for probe in (cand, *(cand + e for e in _MODULE_EXTS)):
                if probe in known:
                    return ("file", probe)
        dir_set = set(dir_keys.values())
        for cand in cands:
            if cand in dir_set:
                return ("dir", cand)
        return (None, None)

    # Exact repo path, or a bare filename such as 'cJSON.h'.
    if t in known:
        return ("file", t)
    hit = _pick_candidate(bases.get(t.rsplit("/", 1)[-1]), importer_dir, current_dir)
    if hit:
        return ("file", hit)

    # Dotted module paths — 'src.helper' (python `from src.helper import x`),
    # 'static.widget' (`from static.widget import y`). Longest prefix first:
    # file inside the matching directory if there is one, else the package
    # directory itself, so the edge still points at the right branch of the
    # tree and the drill-down walks the rest of the way to the file.
    parts = t.split("::")[-1].split(".")
    for n in range(len(parts), 1, -1):
        dirpart = "/".join(parts[:n - 1])
        cands   = [c for c in stems.get(parts[n - 1], []) if _dir_of(c) == dirpart]
        if cands:
            return ("file", sorted(cands)[0])
    for n in range(len(parts), 0, -1):
        key = ".".join(parts[:n])
        if key in dir_keys:
            return ("dir", dir_keys[key])

    # Extensionless module name: python 'artifact'; rust 'foo' from crate::foo.
    module = t.split("::")[-1]
    if "." in module:
        module = module.rsplit(".", 1)[0]
    hit = _pick_candidate(stems.get(module), importer_dir, current_dir)
    return ("file", hit) if hit else (None, None)


def _visible_anchor(target: str, kind: str, current_dir: str,
                    visible_dirs: list) -> str:
    """Deepest visible node on the target's path — the file, or a dir label.

    A cross-directory edge lands on the deepest node of the target's directory
    path that is on screen right now. At root, an edge into
    src/helper/assistant.py lands on 'src'; open 'src' and the same edge moves
    to 'helper'; open 'helper' and it reaches assistant.py itself. A target
    whose directory is off-screen entirely (templates/ while focused in
    static/) falls back to the nearest visible ancestor — './' at worst.
    """
    tdir = target if kind == "dir" else _dir_of(target)
    if kind == "file" and tdir == current_dir:
        return target
    best = ""
    for d in visible_dirs:
        if d == "" or tdir == d or tdir.startswith(d + "/"):
            if len(d) >= len(best):
                best = d
    return _dir_node(best)


def _node_ids(graph, filepath: str) -> list:
    """Interior node ids recorded for a file (tooltips only)."""
    if not graph:
        return []
    return [n.get_name().strip('"') for n in graph.get_nodes()
            if _attr(n, "file") == filepath]


def build_directory_dot(dot_path: str, files, current_path: str = "",
                        show_external: bool = False,
                        show_children: bool = True) -> str:
    """Assemble the DOT source for the directory drill-down view.

    Kept separate from render_directory_graph so the DOT can be asserted on
    without a working graphviz binary.

    show_children=False omits the child-directory column. The strip uses that
    for every directory but the last one on the path: once the strip has slid
    across to static/, the static/ and templates/ labels still sitting on the
    root panel are only repeating the panel next to them, and their click
    targets are the panels next to them. The files themselves stay, so the
    whole path is still readable at a glance.
    """
    current_dir = (current_path or "").strip("/")
    by_dir      = _index_repo_files(files)
    dirs        = _all_dirs(by_dir)
    if current_dir not in dirs:
        current_dir = ""                      # unknown path -> root
    known = {f for fs in by_dir.values() for f in fs}

    # An empty `kids` turns the whole child-stack block below into a no-op, and
    # nothing has to special-case "no column" anywhere else: visible_dirs,
    # rendered_dirs and the edge filter all key off it already. Imports that
    # would have landed on a child label now resolve to the directory's own
    # label instead -- not a node when the cluster is drawn -- and drop out as
    # off-canvas, which is exactly the intent: the column they pointed at is
    # the panel to the right now.
    kids         = _child_dirs(current_dir, dirs) if show_children else []
    active_files = list(by_dir.get(current_dir, []))
    active_set   = set(active_files)

    # Only the current directory and its children are drawn. The ancestors are
    # rendered as an HTML breadcrumb above the canvas instead, so no rank is
    # spent on them and scrolling can never lose the way back up.
    visible_dirs  = [current_dir] + kids
    rendered_dirs = {_dir_node(d) for d in visible_dirs}

    # ---- structure + imports out of graph.dot ---------------------------
    parsed, imports = set(), []
    graph = load_graph(dot_path)
    if graph:
        for node in graph.get_nodes():
            if _attr(node, "type") in LEAF_FILE_TYPES:
                parsed.add(_attr(node, "file") or node.get_name().strip('"'))
        for edge in graph.get_edges():
            if edge.get_attributes().get("label", "").strip('"') != "imports":
                continue
            src, dst = edge.get_source().strip('"'), edge.get_destination().strip('"')
            if src in known and dst:
                imports.append((src, dst))

    # ---- resolve the edges that stay on screen --------------------------
    # Only edges whose *source* lives in the active directory are drawn; each
    # target is retargeted to the deepest visible node on its path, so an edge
    # into static/vendor/react.js lands on './' -> 'static/' -> ... and walks
    # the rest of the way in as those directories are opened. Edges that land
    # on a directory label are drawn with constraint=false below: they must
    # not drag the ranked layout around.
    stems, bases, dir_keys = _build_import_index(known, dirs)
    edges, externals, seen = [], [], set()
    for src, raw in imports:
        if _dir_of(src) != current_dir:       # the source itself is off-screen
            continue
        kind, tgt = resolve_import(raw, current_dir, current_dir,
                                   stems, bases, dir_keys, known)
        if kind is None:
            if show_external:
                ext = raw.strip().strip("'\"<>")
                if ext:
                    externals.append((src, ext))
            continue
        anchor = _visible_anchor(tgt, kind, current_dir, visible_dirs)
        # Drop anything that resolves off-canvas (up the tree or into a
        # sibling branch): its node is not rendered, and emitting an edge to
        # it would make graphviz invent a phantom node. The breadcrumb is how
        # you get back up, so nothing is actually lost.
        if anchor not in active_set and anchor not in rendered_dirs:
            continue
        if anchor != src and (src, anchor) not in seen:
            seen.add((src, anchor))
            edges.append((src, anchor))

    # Every file is positioned by its own import edges, which is what produces
    # the left-to-right dependency flow inside the folder: files nothing else
    # here imports land in the first column.
    root_name  = _dir_node(current_dir)
    root_label = _dir_label(current_dir)
    focus_fill, focus_border = _node_color("dir_focus")
    fill_f, border_f         = _node_color("file")
    lines = []

    # ---- the active directory: one cluster holding its files ------------
    # The directory's own name is the cluster's *label*, drawn at the top of
    # the box, so the folder costs no node and no rank: the file columns get
    # the whole first rank. It is an HTML table rather than bare text so the
    # tab keeps the bordered look the old folder node had.
    if active_files:
        cid = "cluster_" + (re.sub(r"[^A-Za-z0-9_]", "_", current_dir) or "root")
        lines.append(f"    subgraph {cid} {{")
        lines.append('        style="rounded,filled"; fillcolor="#0a0d12"; '
                     'color="#30363d"; penwidth=0.6; margin=16; '
                     'labeljust=l; labelloc=t;')
        lines.append("        label=%s;" % _dir_label_html(root_label,
                                                         focus_border))
        for f in active_files:
            base = f.rsplit("/", 1)[-1]
            tip  = f"{f} - {len(_node_ids(graph, f))} nodes" if f in parsed else f
            lines.append(
                f'        "{f}" [label="{_q(base)}", fillcolor="{fill_f}", '
                f'color="{border_f}", href="javascript:void(0)", '
                f'tooltip="{_q(tip)}"];'
            )
        lines.append("    }")
    else:
        # A directory whose files all live further down: there is nothing to
        # put inside a cluster, so the folder falls back to a plain label.
        lines.append(
            f'    "{root_name}" [label="{_q(root_label)}", shape=folder, '
            f'fillcolor="{focus_fill}", color="{focus_border}", penwidth=1.4, '
            f'href="javascript:void(0)", tooltip="Focused: {_q(root_label)}"];'
        )

    # ---- child directories: one vertical stack at the far right ---------
    if kids:
        kid_fill, kid_border = _node_color("dir")
        for d in kids:
            lines.append(
                f'    "{_dir_node(d)}" [label="{_q(_dir_label(d))}", '
                f'shape=folder, fillcolor="{kid_fill}", color="{kid_border}", '
                f'style="dashed,filled", href="javascript:void(0)", '
                f'tooltip="Open {_q(_dir_label(d))}"];'
            )
        # rank=sink parks the whole stack in the *last* rank, which under
        # rankdir=LR is the rightmost column: the sub-folders become the
        # right-hand border of the canvas instead of a column parked just past
        # the files. It also puts every kid in that one rank, so the stack
        # stays vertical without needing rank=same on top of it.
        lines.append("    { rank=sink; "
                     + "; ".join(f'"{_dir_node(d)}"' for d in kids) + "; }")
        # Invisible edges from every file: these are what make dot's ordering
        # pass centre the stack against the folder's files instead of dumping
        # it at the top of the rank. They all point into the sink rank, so
        # they cannot perturb the ranking itself.
        for f in active_files:
            for d in kids:
                lines.append(f'    "{f}" -> "{_dir_node(d)}" [style=invis];')
        # Heavy weight: the chain is what keeps the stack contiguous and in
        # path order once dot starts shuffling nodes within the rank.
        for a, b in zip(kids, kids[1:]):
            lines.append(f'    "{_dir_node(a)}" -> "{_dir_node(b)}" '
                         f'[style=invis, weight=1000];')

    # ---- import edges ---------------------------------------------------
    for src, anchor in edges:
        if anchor.endswith("/"):              # lands on a directory label
            lines.append(f'    "{src}" -> "{anchor}" [constraint=false];')
        else:
            lines.append(f'    "{src}" -> "{anchor}";')

    if show_external:
        fill, border = _node_color("external")
        # `externals` is (importer, package) pairs, so the declaration takes one
        # node per distinct *package*: a -Tplain of the old form listed a node
        # literally named "('app.py', 'flask')", one per importer, because the
        # loop was iterating the pairs instead of unpacking them.
        for pkg in sorted({p for _, p in externals}):
            lines.append(
                f'    "{pkg}" [label="{_q(pkg)}", shape=ellipse, fontsize=9, '
                f'fillcolor="{fill}", color="{border}"];'
            )
        for src, pkg in sorted(set(externals)):
            lines.append(f'    "{src}" -> "{pkg}" '
                         f'[style=dashed, color="#484f58", constraint=false];')

    logger.info(f"directory graph: focus='{current_dir or './'}' "
                f"files={len(active_files)} kids={len(kids)} "
                f"edges={len(edges)} external={len(externals)}")

    return "\n".join(["digraph residuality {", DIR_GRAPH_ATTRS] + lines + ["}", ""])

def render_directory_graph(dot_path: str, files, current_path: str = "",
                          show_external: bool = False,
                          show_children: bool = True) -> str:
    """Render the directory drill-down view to SVG."""
    return _render_dot(build_directory_dot(dot_path, files, current_path,
                                           show_external=show_external,
                                           show_children=show_children))


# ── Shared renderer ───────────────────────────────────────────────────────

def _render_dot(dot_str: str) -> str:
    try:
        import graphviz
        src = graphviz.Source(dot_str)
        return src.pipe(format="svg").decode("utf-8")
    except Exception as e:
        logger.error(f"Graphviz render failed: {e}")
        return f"<p style='color:#f85149'>Render error: {e}</p>"


# ── Prose export ──────────────────────────────────────────────────────────

def export_prose(repo_path: str, output_dir: str) -> list:
    marker_pattern = re.compile(r'<!--\s*/?rs:[^>]*-->\n?')
    repo  = Path(repo_path)
    out   = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    exported = []
    for md_file in sorted(repo.glob("*.md")):
        if md_file.name == "README.md":
            continue
        clean = marker_pattern.sub('', md_file.read_text(encoding="utf-8"))
        dest  = out / md_file.name
        dest.write_text(clean, encoding="utf-8")
        exported.append(str(dest))
    return exported
