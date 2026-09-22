"""
graph.py — Graph rendering for Residuality
Two-level visualization:
  Level 1: File dependency graph (files + import edges)
  Level 2: File detail (functions, classes, imports, __main__)
"""

import re
import logging
from pathlib import Path
from typing import Optional

import pydot

logger = logging.getLogger(__name__)

# ── Dot styling ───────────────────────────────────────────────────────────

GRAPH_ATTRS = """
    graph [bgcolor="#0d1117" rankdir=LR fontname="Courier New" pad=0.5 nodesep=0.4];
    node  [fontname="Courier New" fontsize=11 fontcolor="#c9d1d9"
           style=filled color="#30363d" shape=box];
    edge  [color="#30363d" fontname="Courier New" fontsize=9 fontcolor="#8b949e"];
"""

NODE_COLORS = {
    "file":     ("#1a2e1a", "#2ea043"),   # green
    "chapter":  ("#1a2e1a", "#2ea043"),
    "class":    ("#1a2035", "#388bfd"),   # blue
    "function": ("#161b22", "#58a6ff"),   # lighter blue
    "section":  ("#2e2a1a", "#9e6a03"),   # amber
    "import":   ("#21262d", "#6e7681"),   # grey
    "main":     ("#2e1a2e", "#a371f7"),   # purple
    "external": ("#1a1a1a", "#484f58"),   # dark grey
}


def _node_color(node_type: str) -> tuple:
    return NODE_COLORS.get(node_type, ("#161b22", "#30363d"))


# ── Dot parsing ───────────────────────────────────────────────────────────

def load_graph(dot_path: str) -> Optional[pydot.Dot]:
    if not Path(dot_path).exists():
        return None
    try:
        graphs = pydot.graph_from_dot_file(dot_path)
        return graphs[0] if graphs else None
    except Exception as e:
        logger.warning(f"Failed to load graph: {e}")
        return None


def _attr(node: pydot.Node, key: str) -> str:
    return node.get_attributes().get(key, "").strip('"')


def get_node_by_id(graph: pydot.Dot, node_id: str) -> Optional[dict]:
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


def get_neighbors(graph: pydot.Dot, node_id: str) -> dict:
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
        edge_label = dst.split(".")[-1] if "." in dst else dst

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
                    short = dst.split(".")[-1] if "." in dst else dst
                    lines.append(
                        f'    "{dst}" [label="{short}" fillcolor="{fill}" '
                        f'color="{border}" fontsize=9];'
                    )
                lines.append(f'    "{src}" -> "{dst}" [label="imports" style=dashed];')
            elif lbl == "contains" and src in file_nodes and dst in file_nodes:
                lines.append(f'    "{src}" -> "{dst}" [label="contains"];')

    lines.append("}")
    return _render_dot("\n".join(lines))


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
