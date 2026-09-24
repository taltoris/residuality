"""
artifact.py — ArtifactRepo
Git-backed artifact store for Residuality.
Handles init, read, write, branch, merge, checkout, diff, and optional remotes.
"""

import os
import logging
import re
from pathlib import Path
from typing import Optional
import git

logger = logging.getLogger(__name__)

REPOS_PATH = os.getenv("REPOS_PATH", "/repos")
GIT_USER_NAME  = os.getenv("GIT_USER_NAME",  "Residuality")
GIT_USER_EMAIL = os.getenv("GIT_USER_EMAIL", "residuality@local")
GITHUB_TOKEN   = os.getenv("GITHUB_TOKEN", "")


# ── Language rules table ──────────────────────────────────────────────────

# Per-language tree-sitter rules used by `_update_code_graph`.
#   containers  node types that own a body (class, struct, impl, module...) —
#               name_field is the child field holding the name, body_field is
#               the child field holding the body, node_type is the graph node
#               type.
#   functions   node types that are callable definitions — name_field as
#               above, unwrap walks one (or a chain of) nested fields to reach
#               the name node.
#   imports     {"type": node type, "field": a single child field, or
#               "collect": set of descendant types}. Delimiters the parser
#               keeps on raw module text (`<stdio.h>`, `"x.h"`, `'os'`) are
#               stripped by `_strip_delimiters`.
#
# Adding a language: add the entry here, add the tree-sitter-* binding to
# requirements.txt and the extension to _EXTENSION_TO_LANG. HTML is the one
# exception: its grammar hangs no fields on `element` for a rules entry to key
# off, so it is parsed by `_update_html_graph` instead.

_EXTENSION_TO_LANG = {
    ".py":    "python",
    ".pyi":   "python",
    ".c":     "c",
    ".h":     "c",
    ".cpp":   "cpp",
    ".hpp":   "cpp",
    ".cc":    "cpp",
    ".cxx":   "cpp",
    ".c++":   "cpp",
    ".h++":   "cpp",
    ".js":    "javascript",
    ".mjs":   "javascript",
    ".cjs":   "javascript",
    ".jsx":   "javascript",
    ".ts":    "javascript",
    ".tsx":   "javascript",
    ".rs":    "rust",
    ".html":  "html",
    ".htm":   "html",
}

_LANGUAGE_RULES = {
    "python": {
        "module":     "tree_sitter_python",
        "containers": {
            "class_definition": {"name_field": "name", "body_field": "body"},
        },
        "functions": {
            "function_definition": {"name_field": "name"},
            "decorated_definition": {"name_field": "name",
                                     "unwrap": "definition"},
        },
        "imports": [
            {"type": "import_statement", "field": "name",
             "postprocess": "strip_as"},
            {"type": "import_from_statement", "field": "module_name"},
        ],
    },
    "c": {
        "module":     "tree_sitter_c",
        "containers": {},                     # C has no classes
        "functions": {
            "function_definition": {"name_field": "declarator",
                                    "unwrap": ["declarator", "declarator"],
                                    "name_is_self": True,
                                    "name_includes_params": True},
        },
        "imports": [
            {"type": "preproc_include", "field": "path"},
        ],
    },
    "cpp": {
        "module":     "tree_sitter_cpp",
        "containers": {
            "struct_specifier":   {"name_field": "name", "body_field": "body"},
            "class_specifier":    {"name_field": "name", "body_field": "body"},
            "enum_specifier":     {"name_field": "name", "body_field": "body"},
        },
        "functions": {
            "function_definition": {"name_field": "name",
                                    "unwrap": ["declarator", "declarator"],
                                    "name_is_self": True,
                                    "name_includes_params": True},
        },
        "imports": [
            {"type": "preproc_include", "field": "path"},
        ],
    },
    "javascript": {
        "module":     "tree_sitter_javascript",
        "containers": {
            "class_declaration": {"name_field": "name", "body_field": "body"},
        },
        "functions": {
            "method_definition":    {"name_field": "name"},
            "constructor":          {"name_field": "name"},
            "function_declaration": {"name_field": "name"},
            # arrow functions are skipped — their name lives on the
            # variable_declarator, not the arrow function node.
        },
        "imports": [
            {"type": "import_statement", "field": "source"},
        ],
    },
    "rust": {
        "module":     "tree_sitter_rust",
        "containers": {
            "struct_item": {"name_field": "name", "body_field": "body"},
            "enum_item":   {"name_field": "name", "body_field": "body"},
            "mod_item":    {"name_field": "name", "body_field": "body",
                            "node_type": "module"},
            "impl_item":   {"name_field": "type", "body_field": "body",
                            "node_type": "class", "container_skip": True},
            "trait_item":  {"name_field": "name", "body_field": "body"},
        },
        "functions": {
            "function_item": {"name_field": "name"},
        },
        "imports": [
            {"type": "use_declaration", "field": "argument",
             "postprocess": "strip_as"},
        ],
    },
}


def _strip_delimiters(text: str) -> str:
    """Drop parser-delimiters tree-sitter retains on raw module text.

    C/C++ `system_lib_string` -> <stdio.h>      -> stdio.h
    C/C++ `string_literal`    -> "x.h"          -> x.h
    JS      `string`          -> 'os'           -> os
    Python  `dotted_name`     -> os.path        -> os.path (unchanged)
    """
    # compare matching delimiter pairs; built with chr() so the
    pairs = [(chr(60), chr(62)), (chr(39), chr(39)), (chr(34), chr(34))]
    if len(text) >= 2 and (text[0], text[-1]) in pairs:
        return text[1:-1]
    return text


# ── HTML / Jinja ──────────────────────────────────────────────────────────

# `src`/`href` on these elements is a real dependency of the page (a script, a
# stylesheet); an <img>/<video>/<a> is content or navigation, not composition,
# so it is left out.
_HTML_ASSET_ATTR = {"script": "src", "link": "href"}

# `{{ url_for('static', filename='vendor/react.js') }}` is the one template
# form that carries a repo path; every other template-shaped value is skipped
# rather than guessed at.
_HTML_URL_FOR_STATIC = re.compile(r"filename\s*=\s*['\"]([^'\"]+)['\"]")

# Jinja composition: `{% extends "base.html" %}`, `{% include "x.html" %}`,
# and the macro forms `{% import "forms.html" %}` / `{% from "forms.html"
# import field %}`.
_HTML_TEMPLATE_IMPORT = re.compile(
    r"\{%-?\s*(?:extends|include|import|from)\s+['\"]([^'\"]+)['\"]"
)


def _html_asset_target(value: Optional[str]) -> Optional[str]:
    """Repo path (or off-repo URL) an HTML asset attribute points at.

      '/static/residuality.js'                          -> '/static/residuality.js'
      "{{ url_for('static', filename='vendor/r.js') }}" -> 'static/vendor/r.js'
      'https://cdn.example.com/x.js'                    -> unchanged (off-repo)
      '#chat-log', '{{ url_for("chat") }}'              -> None (not a file)

    Values are handed on un-resolved: `graph.resolve_import` decides whether an
    off-repo URL becomes an external stub, so the same string the template
    contains is what an unresolved edge shows.
    """
    v = (value or "").strip()
    if not v or v.startswith("#"):
        return None
    if "{" in v:
        m = _HTML_URL_FOR_STATIC.search(v)
        return f"static/{m.group(1).lstrip('/')}" if m else None
    return v


class ArtifactRepo:

    def __init__(self, project_id: str):
        self.project_id = project_id
        self.repo_path  = Path(REPOS_PATH) / project_id
        if self.repo_path.exists():
            self.repo = git.Repo(self.repo_path)
            self._ensure_residuality()
        else:
            self.repo = None

    def _ensure_residuality(self) -> None:
        """Create .residuality dir and files if they don't exist yet."""
        res_dir = self.repo_path / ".residuality"
        res_dir.mkdir(exist_ok=True)

        src_scm = Path("/app/.residuality/extract-python.scm")
        dst_scm = res_dir / "extract-python.scm"
        if src_scm.exists() and not dst_scm.exists():
            dst_scm.write_text(src_scm.read_text())

        dot_path = res_dir / "graph.dot"
        if not dot_path.exists():
            dot_path.write_text("digraph residuality {\n\n}\n")

        gitignore = self.repo_path / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text(
                "# Residuality default .gitignore\n"
                "__pycache__/\n"
                "*.pyc\n"
                "*.pyo\n"
                "build/\n"
                "dist/\n"
                "*.egg-info/\n"
                "*.so\n"
                "*.o\n"
                "*.out\n"
                "*.log\n"
                "export/\n"
                ".residuality/\n"
                ".DS_Store\n"
            )

    # ── Init ─────────────────────────────────────────────────────────────

    @classmethod
    def create(cls, project_id: str, description: str = "") -> "ArtifactRepo":
        """Initialize a project — uses existing git repo if present, otherwise creates one."""
        repo_path = Path(REPOS_PATH) / project_id
        repo_path.mkdir(parents=True, exist_ok=True)

        git_exists = (repo_path / ".git").exists()

        # Init .residuality directory
        res_dir = repo_path / ".residuality"
        res_dir.mkdir(exist_ok=True)

        # Copy extract-python.scm into the project
        src_scm = Path("/app/.residuality/extract-python.scm")
        dst_scm = res_dir / "extract-python.scm"
        if src_scm.exists() and not dst_scm.exists():
            dst_scm.write_text(src_scm.read_text())

        # Initialize empty graph.dot
        dot_path = res_dir / "graph.dot"
        if not dot_path.exists():
            dot_path.write_text("digraph residuality {\n\n}\n")

        # Default .gitignore — only write if it doesn't exist
        gitignore = repo_path / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text(
                "# Residuality default .gitignore\n"
                "__pycache__/\n"
                "*.pyc\n"
                "*.pyo\n"
                "build/\n"
                "dist/\n"
                "*.egg-info/\n"
                "*.so\n"
                "*.o\n"
                "*.out\n"
                "*.log\n"
                "export/\n"
                ".residuality/\n"
                ".DS_Store\n"
            )

        if git_exists:
            # Use existing git repo as-is
            repo = git.Repo(repo_path)
            logger.info(f"Using existing git repo for project: {project_id}")
        else:
            # Initialize fresh git repo
            repo = git.Repo.init(repo_path)
            logger.info(f"Initialized new git repo for project: {project_id}")

        with repo.config_writer() as cw:
            cw.set_value("user", "name",  GIT_USER_NAME)
            cw.set_value("user", "email", GIT_USER_EMAIL)

        # Commit Residuality metadata — .residuality is ignored by default
        # (graph.dot, extract-python.scm), so only stage what git allows.
        files_to_add = []
        for f in [".residuality/extract-python.scm", ".residuality/graph.dot", ".gitignore"]:
            full = repo_path / f
            if not full.exists():
                continue
            try:
                repo.repo.git.check_ignore("--quiet", str(full))
                continue
            except Exception:
                pass
            files_to_add.append(f)

        if files_to_add:
            repo.index.add(files_to_add)

        if not git_exists and description:
            (repo_path / "README.md").write_text(f"# {project_id}\n\n{description}\n")
            repo.index.add(["README.md"])

        repo.index.commit(f"Init project: {project_id}")
        logger.info(f"Created project repo: {project_id}")

        instance = cls.__new__(cls)
        instance.project_id = project_id
        instance.repo_path  = repo_path
        instance.repo       = repo
        return instance

    # ── Read / Write ──────────────────────────────────────────────────────

    def read(self, path: str, commit: Optional[str] = None) -> str:
        """Read a file at HEAD or at a specific commit hash."""
        if commit:
            try:
                blob = self.repo.commit(commit).tree / path
                return blob.data_stream.read().decode("utf-8", errors="replace")
            except KeyError:
                raise FileNotFoundError(f"{path} not found at commit {commit}")
        else:
            return (self.repo_path / path).read_text(encoding="utf-8", errors="replace")

    def write(self, path: str, content: str, message: str,
              branch: Optional[str] = None,
              model_used: Optional[str] = None,
              turn: Optional[int] = None) -> str:
        """Write content to a file and commit. Returns commit hash."""
        if branch and branch != self._current_branch():
            self.repo.git.checkout(branch)

        full_path = self.repo_path / path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content, encoding="utf-8")

        self.repo.index.add([path])

        full_message = message
        if model_used:
            full_message += f"\n\nModel: {model_used}"
        if turn is not None:
            full_message += f"\nTurn: {turn}"

        commit = self.repo.index.commit(full_message)
        logger.info(f"Committed {path} → {commit.hexsha[:8]}: {message[:60]}")
        return commit.hexsha

    def apply_edits(self, path: str, edits: list, message: str,
                    model_used: Optional[str] = None,
                    turn: Optional[int] = None) -> str:
        """
        Apply multiple line-range edits to a file bottom-up, then commit.
        edits: [{"line_start": int, "line_end": int, "new_content": str}, ...]
        """
        full_path = self.repo_path / path
        lines = full_path.read_text(encoding="utf-8").splitlines()

        # Sort descending — edit from bottom of file upward
        edits_sorted = sorted(edits, key=lambda e: e["line_start"], reverse=True)

        for edit in edits_sorted:
            new_lines = edit["new_content"].splitlines()
            lines = (
                lines[:edit["line_start"] - 1] +
                new_lines +
                lines[edit["line_end"]:]
            )

        full_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        # Reparse graph
        self._update_graph(path)

        return self.write(path, "\n".join(lines) + "\n", message,
                          model_used=model_used, turn=turn)

    # ── Branch / Merge ────────────────────────────────────────────────────

    def branch(self, name: str, from_commit: str, reason: str = "") -> str:
        """Create a new branch from a specific commit hash."""
        self.repo.git.checkout(from_commit, b=name)
        if reason:
            logger.info(f"Branch '{name}' from {from_commit[:8]}: {reason}")
        return name

    def merge_commits(self, commit_a: str, commit_b: str,
                      reconciled_files: dict, message: str) -> str:
        """
        Create a merge commit with two parents.
        reconciled_files: {path: content} — model-reconciled content per file.
        Returns merge commit hash.
        """
        # Write reconciled content
        for path, content in reconciled_files.items():
            full_path = self.repo_path / path
            full_path.write_text(content, encoding="utf-8")
            self.repo.index.add([path])

        # Create merge commit with two parents
        parent_a = self.repo.commit(commit_a)
        parent_b = self.repo.commit(commit_b)

        commit = self.repo.index.commit(
            message,
            parent_commits=[parent_a, parent_b]
        )
        logger.info(f"Merge commit {commit.hexsha[:8]}: {commit_a[:8]} + {commit_b[:8]}")
        return commit.hexsha

    def checkout(self, commit: str) -> None:
        """Restore working tree to a specific commit (detached HEAD)."""
        self.repo.git.checkout(commit)
        logger.info(f"Checked out {commit[:8]}")

    # ── History / DAG ─────────────────────────────────────────────────────

    def get_dag(self) -> list:
        """Return all commits as a list of nodes for DAG visualization."""
        commits = list(self.repo.iter_commits("--all"))
        nodes = []
        for c in commits:
            # Determine branch name(s)
            branches = [
                ref.name.replace("refs/heads/", "")
                for ref in self.repo.references
                if hasattr(ref, "commit") and ref.commit == c
                   and not ref.name.startswith("refs/remotes")
            ]
            nodes.append({
                "hash":      c.hexsha,
                "short":     c.hexsha[:8],
                "message":   c.message.split("\n")[0],
                "parents":   [p.hexsha[:8] for p in c.parents],
                "branches":  branches,
                "timestamp": c.committed_date,
                "author":    str(c.author),
                "is_merge":  len(c.parents) > 1,
            })
        return nodes

    def diff(self, commit_a: str, commit_b: str) -> str:
        """Return unified diff between two commits."""
        ca = self.repo.commit(commit_a)
        cb = self.repo.commit(commit_b)
        diff = ca.diff(cb, create_patch=True)
        result = []
        for d in diff:
            result.append(d.diff.decode("utf-8", errors="replace"))
        return "\n".join(result)

    def log(self, limit: int = 50) -> list:
        """Return recent commit history."""
        commits = list(self.repo.iter_commits("HEAD", max_count=limit))
        return [
            {
                "hash":      c.hexsha[:8],
                "message":   c.message.split("\n")[0],
                "timestamp": c.committed_date,
                "is_merge":  len(c.parents) > 1,
            }
            for c in commits
        ]

    def list_files(self, commit: Optional[str] = None) -> list:
        """List all tracked files at HEAD or a specific commit."""
        c = self.repo.commit(commit) if commit else self.repo.head.commit
        return [item.path for item in c.tree.traverse()
                if item.type == "blob"
                and not item.path.startswith(".residuality")]

    # ── Graph ─────────────────────────────────────────────────────────────

    def _update_graph(self, filepath: str) -> None:
        """Parse file and update its section in graph.dot."""
        full_path = self.repo_path / filepath
        dot_path  = self.repo_path / ".residuality" / "graph.dot"

        if not full_path.exists():
            return

        lang = self._lang_key(filepath)
        if lang == "html":
            # Not `_LANGUAGE_RULES` — see `_update_html_graph`.
            self._update_html_graph(filepath, dot_path)
        elif lang:
            self._update_code_graph(filepath, _LANGUAGE_RULES[lang], dot_path)
        elif filepath.endswith(".md"):
            self._update_prose_graph(filepath)

    @staticmethod
    def _lang_key(filepath: str) -> Optional[str]:
        """Map a file extension onto the language rules table key."""
        name = Path(filepath).name.lower()
        for ext, lang in _EXTENSION_TO_LANG.items():
            if name.endswith(ext):
                return lang
        return None

    def _update_code_graph(self, filepath: str, rules: dict,
                           dot_path: Path) -> None:
        """Parse a code file with its tree-sitter grammar and update graph.dot.

        One walker, one dot writer for every language. The per-language rules
        table declares which node types are classes, functions and imports, so
        adding a language is a table entry plus a tree-sitter-* binding.
        """
        import importlib

        from tree_sitter import Language, Parser

        full_path = self.repo_path / filepath
        source    = full_path.read_bytes()

        lang_module = importlib.import_module(rules["module"])
        language    = Language(lang_module.language())
        parser      = Parser(language)
        tree        = parser.parse(source)

        nodes = []
        edges = []
        # A trailing newline *terminates* the last line rather than starting a
        # new one, so counting without the +1 keeps the file node from claiming
        # one line more than the file has. Prose files already used the line
        # count, so the two paths disagreed on every file ending in a newline.
        lines = source.count(b'\n')
        if not source.endswith(b'\n'):
            lines += 1
        lines = max(lines, 1)

        nodes.append(
            f'    "{filepath}" [type="file", line_start=1, line_end={lines}];'
        )

        def escape(s: str) -> str:
            return s.replace('"', '\\"').replace('\n', ' ')

        def node_text(n) -> str:
            return source[n.start_byte:n.end_byte].decode("utf-8", errors="replace")

        def collect_nodes(node, types, out):
            """Gather every descendant of `node` whose type is in `types`."""
            if node.type in types:
                out.append(node)
            for child in node.children:
                collect_nodes(child, types, out)

        # Walk the tree directly — avoids query API version differences.
        # `class_id`/`class_end` track the innermost enclosing class so methods
        # become "file.py::Class::method" instead of "file.py::method". Both are
        # byte offsets — the old row-vs-byte mix mis-attributed methods across
        # nested classes (see README known limitations).
        def walk(node, parent_id, class_id=None, class_end=0):
            # --- class / struct / enum / impl / module container -----------
            for ctype, cspec in rules["containers"].items():
                if node.type != ctype:
                    continue
                name_node = node.child_by_field_name(cspec["name_field"])
                if not name_node:
                    break
                class_name = node_text(name_node)
                class_type = cspec.get("node_type", "class")
                class_node_id = f"{filepath}::{class_name}"
                if cspec.get("container_skip"):
                    # scopes its body (e.g. Rust `impl Point`) but shares
                    # the ID with the real definition, so emit no node
                    body_field = cspec.get("body_field")
                    if body_field:
                        body = node.child_by_field_name(body_field)
                        if body:
                            for child in body.children:
                                walk(child, parent_id=class_node_id,
                                     class_id=class_node_id,
                                     class_end=node.end_byte)
                    return
                sig = escape(f"{class_type} {class_name}")
                nodes.append(
                    f'    "{class_node_id}" [type="{class_type}", '
                    f'file="{filepath}", line_start={node.start_point[0]+1}, '
                    f'line_end={node.end_point[0]+1}, signature="{sig}"];'
                )
                edges.append(f'    "{filepath}" -> "{class_node_id}" '
                             f'[label="contains"];')
                body_field = cspec.get("body_field")
                if body_field:
                    body = node.child_by_field_name(body_field)
                    if body:
                        for child in body.children:
                            walk(child, parent_id=class_node_id,
                                 class_id=class_node_id, class_end=node.end_byte)
                return  # body already walked

            # --- function / method ---------------------------------------
            for ftype, fspec in rules["functions"].items():
                if node.type != ftype:
                    continue
                # `unwrap` descends to the *declarator* to read the name, but
                # the span has to come from this outermost node. In C/C++ a
                # declarator covers only the signature line, so recording its
                # span gave every function a one-line body -- parse_array came
                # out as 81-81 instead of 81-102, and the detail modal had
                # nothing to show but the declaration.
                span = node
                fn_node = node
                unwrap = fspec.get("unwrap")
                if unwrap:
                    if isinstance(unwrap, list):
                        for field in unwrap:
                            fn_node = fn_node.child_by_field_name(field) or fn_node
                    else:
                        fn_node = fn_node.child_by_field_name(unwrap) or fn_node
                if fspec.get("name_is_self"):
                    name_node = fn_node
                else:
                    name_node = fn_node.child_by_field_name(
                        fspec["name_field"])
                if not name_node:
                    break
                fn_name  = node_text(name_node)
                fn_parent = class_id if class_id else parent_id
                fn_id    = f"{fn_parent}::{fn_name}"
                # A C/C++ declarator already carries the parameter list, so
                # appending "(...)" gave "parse_array(cJSON *item, const char
                # *value)(...)". Only elide params where the name lacks them
                # (Python/JS/Rust read the bare identifier).
                sig      = escape(fn_name if fspec.get("name_includes_params")
                                  else fn_name + "(...)")
                nodes.append(
                    f'    "{fn_id}" [type="function", file="{filepath}", '
                    f'line_start={span.start_point[0]+1}, '
                    f'line_end={span.end_point[0]+1}, signature="{sig}"];'
                )
                edges.append(f'    "{fn_parent}" -> "{fn_id}" '
                             f'[label="contains"];')
                return  # don't recurse into function bodies

            # --- import edges --------------------------------------------
            for imp in rules["imports"]:
                if node.type != imp["type"]:
                    continue
                field = imp.get("field")
                if field:
                    mod_node = node.child_by_field_name(field)
                    if mod_node:
                        mod = _strip_delimiters(node_text(mod_node))
                        if imp.get("postprocess") == "strip_as":
                            mod = mod.split(" as ")[0].strip()
                        edges.append(f'    "{filepath}" -> "{mod}" '
                                     f'[label="imports"];')
                collect = imp.get("collect")
                if collect:
                    mods = []
                    collect_nodes(node, set(collect), mods)
                    for mod_node in mods:
                        mod = _strip_delimiters(node_text(mod_node))
                        if mod:
                            edges.append(f'    "{filepath}" -> "{mod}" '
                                         f'[label="imports"];')
                break

            # --- recurse for all other node types ------------------------
            for child in node.children:
                walk(child, parent_id, class_id, class_end)

        walk(tree.root_node, parent_id=filepath)
        self._write_dot_section(filepath, nodes, edges, dot_path)
        logger.info(f"graph: {filepath} -> {len(nodes)} nodes, {len(edges)} edges")

    def _write_dot_section(self, filepath: str, nodes: list,
                           edges: list, dot_path: Path) -> None:
        """Replace the file's section in graph.dot atomically."""
        section = "\n".join(
            [f"    // --- file: {filepath} ---"] +
            nodes + [""] + edges +
            [f"    // --- end: {filepath} ---"]
        )

        dot_content = dot_path.read_text() if dot_path.exists() \
                      else "digraph residuality {\n\n}\n"

        start_marker = f"    // --- file: {filepath} ---"
        end_marker   = f"    // --- end: {filepath} ---"
        p_start = dot_content.find(start_marker)
        p_end   = dot_content.find(end_marker)

        if p_start >= 0 and p_end >= 0:
            p_end_full = p_end + len(end_marker)
            while p_end_full < len(dot_content) and dot_content[p_end_full] == '\n':
                p_end_full += 1
            updated = dot_content[:p_start] + section + "\n\n" + dot_content[p_end_full:]
        else:
            closing = dot_content.rfind("}")
            if closing >= 0:
                updated = dot_content[:closing] + "\n" + section + "\n}\n"
            else:
                updated = f"digraph residuality {{\n\n{section}\n}}\n"

        # Atomic write
        tmp = dot_path.with_suffix(".tmp")
        tmp.write_text(updated)
        tmp.rename(dot_path)
        logger.info(f"Updated graph.dot for {filepath}")

    def _update_prose_graph(self, filepath: str) -> None:
        """Parse rs:section markers and update graph.dot for a prose file."""
        import re
        full_path = self.repo_path / filepath
        dot_path  = self.repo_path / ".residuality" / "graph.dot"

        source = full_path.read_text(encoding="utf-8", errors="replace")
        pattern = re.compile(
            r'<!--\s*rs:section\s+id="([^"]+)"\s+label="([^"]+)"\s*-->'
            r'(.*?)'
            r'<!--\s*/rs:section\s*-->',
            re.DOTALL
        )

        lines   = source.splitlines()
        nodes   = []
        edges   = []
        prev_id = None

        nodes.append(
            f'    "{filepath}" [type="chapter", line_start=1, '
            f'line_end={len(lines)}, label="{filepath}"];'
        )

        for m in pattern.finditer(source):
            sec_id     = m.group(1)
            sec_label  = m.group(2).replace('"', '\\"')
            sec_text   = m.group(3)

            # Span the prose, not the marker pair. The `<!-- rs:section -->`
            # markers are metadata for downstream prompt-building; counting
            # them in the span made a section's source view two lines of HTML
            # comment around one line of story, and disagreed with word_count,
            # which only ever counted the prose.
            if sec_text.strip():
                lead      = len(sec_text) - len(sec_text.lstrip())
                trail     = len(sec_text) - len(sec_text.rstrip())
                start_off = m.start(3) + lead
                end_off   = m.end(3) - trail
            else:
                start_off, end_off = m.start(), m.end()

            start_line = source[:start_off].count('\n') + 1
            end_line   = source[:end_off].count('\n') + 1
            word_count = len(sec_text.split())
            node_id    = f"{filepath}::{sec_id}"

            nodes.append(
                f'    "{node_id}" [type="section", file="{filepath}", '
                f'line_start={start_line}, line_end={end_line}, '
                f'label="{sec_label}", word_count={word_count}];'
            )
            edges.append(f'    "{filepath}" -> "{node_id}" [label="contains"];')
            if prev_id:
                edges.append(f'    "{prev_id}" -> "{node_id}" [label="precedes"];')
            prev_id = node_id

        self._write_dot_section(filepath, nodes, edges, dot_path)

    def _update_html_graph(self, filepath: str, dot_path: Path) -> None:
        """Parse an HTML/Jinja file with tree-sitter-html and update graph.dot.

        Dispatched here rather than through `_LANGUAGE_RULES`, because the
        grammar gives a rules entry nothing to hold on to: `element` carries
        no fields at all (both the tag name and every attribute live inside
        the `start_tag` child, and `tag_name` is not a named field either), so
        there is no `name_field` to read a node's name from — and one node per
        element would be thousands of nodes for a single page.

        What is worth a node in a template is narrower:
          - every element declaring an `id` — the addressable chunk a surgical
            node edit targets, and the same idea as a prose `rs:section`;
          - inline `<script>`/`<style>` blocks — where a page's real code
            lives, and the only node covering it;
          - `src`/`href` assets and Jinja `{% extends %}` / `{% include %}` —
            the page's import edges.

        Jinja braces are opaque to the grammar (a `{% extends %}` ends up
        inside a plain text node), so composition is read off the raw text
        while structure comes from the parse.
        """
        import importlib

        from tree_sitter import Language, Parser

        full_path = self.repo_path / filepath
        source    = full_path.read_bytes()
        text      = source.decode("utf-8", errors="replace")

        html_module = importlib.import_module("tree_sitter_html")
        parser      = Parser(Language(html_module.language()))
        tree        = parser.parse(source)

        # Same line count as the code path: a trailing newline terminates the
        # last line rather than starting a new one.
        n_lines = source.count(b'\n')
        if not source.endswith(b'\n'):
            n_lines += 1
        n_lines = max(n_lines, 1)

        def escape(s: str) -> str:
            return s.replace('"', '\\"').replace('\n', ' ')

        def node_text(n) -> str:
            return source[n.start_byte:n.end_byte].decode("utf-8", errors="replace")

        def start_tags(node):
            """The element's opening tag — `start_tag`, or `self_closing_tag`."""
            for child in node.children:
                if child.type in ("start_tag", "self_closing_tag"):
                    yield child

        def tag_name(node) -> str:
            """'div' for a `<div>`; empty when the body has no opening tag."""
            for tag in start_tags(node):
                for child in tag.children:
                    if child.type == "tag_name":
                        return node_text(child).strip().lower()
            return ""

        def attr(node, want: str) -> Optional[str]:
            """Value of one attribute on the element's opening tag, or None.

            Read positionally, not by field name: this grammar exposes no
            field metadata at all — `child_by_field_name` answers None for
            every `attribute`, `name` and `value` included — so the name is
            whatever precedes the first '=' and the value is the one child
            after it, quoted or bare. A boolean attribute (`disabled`) has no
            value child and reads as "".
            """
            for tag in start_tags(node):
                for child in tag.children:
                    if child.type != "attribute":
                        continue
                    if node_text(child).split("=", 1)[0].strip().lower() != want:
                        continue
                    for part in child.children:
                        if part.type in ("quoted_attribute_value",
                                         "attribute_value"):
                            return node_text(part).strip().strip("'\"")
                    return ""
            return None

        nodes = [f'    "{filepath}" [type="file", line_start=1, '
                 f'line_end={n_lines}];']
        edges = []

        taken: set = {filepath}
        blocks: dict = {}

        def unique(nid: str) -> str:
            """A node id nothing else on this page already owns.

            A duplicate `id=` is invalid HTML but everywhere, and a second
            node under the first one's id would overwrite its Qdrant point —
            the same silent collision the minified-JS bundle causes.
            """
            if nid in taken:
                n = 2
                while f"{nid}~{n}" in taken:
                    n += 1
                nid = f"{nid}~{n}"
            taken.add(nid)
            return nid

        def walk(node, parent_id: str) -> None:
            """Depth-first, carrying the nearest enclosing node id down."""
            nid = parent_id
            if node.type in ("element", "script_element", "style_element"):
                tag     = tag_name(node)
                elem_id = (attr(node, "id") or "").strip() if tag else ""
                if tag and elem_id and "{" not in elem_id:
                    nid   = unique(f"{filepath}::{tag}#{elem_id}")
                    label = elem_id
                    # No '"' in a signature: `render_file_detail` interpolates
                    # it straight into a DOT label, and the escaped quote
                    # written here swallowed the rest of the attribute list
                    # when it was read back and re-emitted.
                    sig   = f"{tag} #{elem_id}"
                elif tag and any(c.type == "raw_text"
                                 and node_text(c).strip() for c in node.children):
                    # A block with real text in it. `<script src="...">` is not
                    # one: its raw_text is empty, and its content is the import
                    # edge below.
                    blocks[tag] = blocks.get(tag, 0) + 1
                    nid   = unique(f"{filepath}::{tag}#{blocks[tag]}")
                    label = f"inline {tag}"
                    # Never '<script>': graphviz reads any quoted label that
                    # opens with '<' and closes with '>' as an HTML-like label,
                    # and `<script>` is not valid HTML-label markup — it fails
                    # the whole file's render with a syntax error.
                    sig   = (f"inline {tag}" if blocks[tag] == 1
                             else f"inline {tag} #{blocks[tag]}")
                else:
                    nid = parent_id
                if nid != parent_id:
                    nodes.append(
                        f'    "{nid}" [type="section", file="{filepath}", '
                        f'line_start={node.start_point[0] + 1}, '
                        f'line_end={node.end_point[0] + 1}, '
                        f'signature="{escape(sig)}", '
                        f'label="{escape(label)}"];'
                    )
                    edges.append(f'    "{parent_id}" -> "{nid}" '
                                 f'[label="contains"];')
            for child in node.children:
                walk(child, nid)

        walk(tree.root_node, parent_id=filepath)

        targets = []

        def collect_assets(node) -> None:
            if node.type in ("element", "script_element"):
                want = _HTML_ASSET_ATTR.get(tag_name(node))
                if want:
                    target = _html_asset_target(attr(node, want))
                    if target:
                        targets.append(target)
            for child in node.children:
                collect_assets(child)

        collect_assets(tree.root_node)
        targets.extend(_HTML_TEMPLATE_IMPORT.findall(text))

        for target in dict.fromkeys(targets):
            edges.append(f'    "{filepath}" -> "{escape(target)}" '
                         f'[label="imports"];')

        self._write_dot_section(filepath, nodes, edges, dot_path)
        logger.info(f"graph(html): {filepath} -> {len(nodes)} nodes, "
                    f"{len(edges)} edges")

    def get_graph_dot(self) -> str:
        """Return current graph.dot contents."""
        dot_path = self.repo_path / ".residuality" / "graph.dot"
        if dot_path.exists():
            return dot_path.read_text()
        return "digraph residuality {\n\n}\n"

    def get_node_at_line(self, filepath: str, line: int) -> Optional[dict]:
        """Return the graph node (function/section) containing a given line."""
        from graph import load_graph
        dot_path = self.repo_path / ".residuality" / "graph.dot"
        if not dot_path.exists():
            return None
        try:
            graph = load_graph(str(dot_path))
            if not graph:
                return None
            best  = None
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
                        "type":       attrs.get("type", "").strip('"'),
                        "line_start": ls,
                        "line_end":   le,
                        "signature":  attrs.get("signature", "").strip('"'),
                        "label":      attrs.get("label", "").strip('"'),
                    }
                    best_size = le - ls
            return best
        except Exception as e:
            logger.warning(f"get_node_at_line failed: {e}")
            return None

    # ── Optional remote ───────────────────────────────────────────────────

    def add_remote(self, url: str, name: str = "origin") -> None:
        """Link an optional GitHub/GitLab remote."""
        if name in [r.name for r in self.repo.remotes]:
            self.repo.delete_remote(name)
        self.repo.create_remote(name, url)
        logger.info(f"Remote '{name}' → {url}")

    def push(self, remote: str = "origin", branch: str = "main") -> None:
        """Push to remote. Uses GITHUB_TOKEN env var if set."""
        if remote not in [r.name for r in self.repo.remotes]:
            raise ValueError(f"No remote '{remote}' configured")
        env = {}
        if GITHUB_TOKEN:
            env["GIT_ASKPASS"] = "echo"
            env["GIT_USERNAME"] = "token"
            env["GIT_PASSWORD"] = GITHUB_TOKEN
        self.repo.remotes[remote].push(branch, env=env or None)
        logger.info(f"Pushed {branch} → {remote}")

    def pull(self, remote: str = "origin", branch: str = "main") -> None:
        """Pull from remote."""
        if remote not in [r.name for r in self.repo.remotes]:
            raise ValueError(f"No remote '{remote}' configured")
        self.repo.remotes[remote].pull(branch)
        logger.info(f"Pulled {branch} ← {remote}")

    def remove_remote(self, name: str = "origin") -> None:
        self.repo.delete_remote(name)

    def get_remotes(self) -> list:
        return [{"name": r.name, "url": r.url} for r in self.repo.remotes]

    # ── Helpers ───────────────────────────────────────────────────────────

    def _current_branch(self) -> str:
        try:
            return self.repo.active_branch.name
        except TypeError:
            return "HEAD"

    def exists(self) -> bool:
        return self.repo_path.exists() and self.repo is not None
