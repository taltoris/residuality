"""
graph.py — Code Structure Graph dispatcher
Routes to tree-sitter pipeline (.py) or prose parser (.md).
"""

import os
import re
import subprocess
import logging
from pathlib import Path
from typing import Optional

import pydot

logger = logging.getLogger(__name__)


def render_dot_to_svg(dot_content: str) -> str:
    """Render a dot string to SVG using Graphviz."""
    try:
        import graphviz
        src = graphviz.Source(dot_content)
        return src.pipe(format="svg").decode("utf-8")
    except Exception as e:
        logger.warning(f"Graphviz render failed: {e}")
        return f"<p>Graph render error: {e}</p>"


def load_graph(dot_path: str) -> Optional[pydot.Dot]:
    """Load a dot file and return the pydot graph."""
    if not Path(dot_path).exists():
        return None
    try:
        graphs = pydot.graph_from_dot_file(dot_path)
        return graphs[0] if graphs else None
    except Exception as e:
        logger.warning(f"Failed to load graph from {dot_path}: {e}")
        return None


def get_node_at_line(graph: pydot.Dot, filepath: str, line: int) -> Optional[dict]:
    """Find the smallest graph node containing a given line in a file."""
    best      = None
    best_size = 999999

    for node in graph.get_nodes():
        attrs = node.get_attributes()
        f = attrs.get("file", "").strip('"')
        if f != filepath:
            continue
        try:
            ls = int(attrs.get("line_start", 0))
            le = int(attrs.get("line_end",   0))
        except (ValueError, TypeError):
            continue
        if ls <= line <= le and (le - ls) < best_size:
            best = {
                "id":         node.get_name().strip('"'),
                "type":       attrs.get("type",      "").strip('"'),
                "line_start": ls,
                "line_end":   le,
                "signature":  attrs.get("signature", "").strip('"'),
                "label":      attrs.get("label",     "").strip('"'),
            }
            best_size = le - ls

    return best


def get_node_by_id(graph: pydot.Dot, node_id: str) -> Optional[dict]:
    """Find a graph node by its ID."""
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
    """Return all edges to/from a node."""
    parents  = []
    children = []
    calls    = []
    called_by = []

    for edge in graph.get_edges():
        src = edge.get_source().strip('"')
        dst = edge.get_destination().strip('"')
        lbl = edge.get_attributes().get("label", "").strip('"')

        if dst == node_id:
            if lbl == "contains":
                parents.append(src)
            elif lbl == "calls":
                called_by.append(src)
        if src == node_id:
            if lbl == "contains":
                children.append(dst)
            elif lbl in ("calls", "imports"):
                calls.append(dst)

    return {
        "parents":   parents,
        "children":  children,
        "calls":     calls,
        "called_by": called_by,
    }


def export_prose(repo_path: str, output_dir: str) -> list:
    """
    Strip rs:section markers from all .md files and write to output_dir.
    Returns list of exported file paths.
    """
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
