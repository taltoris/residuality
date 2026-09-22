"""
artifact.py — ArtifactRepo
Git-backed artifact store for Residuality.
Handles init, read, write, branch, merge, checkout, diff, and optional remotes.
"""

import os
import logging
from pathlib import Path
from typing import Optional
import git

logger = logging.getLogger(__name__)

REPOS_PATH = os.getenv("REPOS_PATH", "/repos")
GIT_USER_NAME  = os.getenv("GIT_USER_NAME",  "Residuality")
GIT_USER_EMAIL = os.getenv("GIT_USER_EMAIL", "residuality@local")
GITHUB_TOKEN   = os.getenv("GITHUB_TOKEN", "")


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

        # Commit Residuality metadata (graph.dot, extract-python.scm, .gitignore)
        # Only add files that aren't already tracked
        files_to_add = []
        for f in [".residuality/extract-python.scm", ".residuality/graph.dot", ".gitignore"]:
            full = repo_path / f
            if full.exists():
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

        # Also add graph.dot if it was updated
        dot_rel = ".residuality/graph.dot"
        dot_abs = self.repo_path / dot_rel
        if dot_abs.exists():
            try:
                self.repo.index.add([dot_rel])
            except Exception:
                pass

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

        if filepath.endswith(".py"):
            self._update_python_graph(filepath, dot_path)
        elif filepath.endswith(".md"):
            self._update_prose_graph(filepath)

    def _update_python_graph(self, filepath: str, dot_path: Path) -> None:
        """Parse a Python file using tree-sitter Python bindings."""
        import tree_sitter_python as tspython
        from tree_sitter import Language, Parser

        full_path = self.repo_path / filepath
        source    = full_path.read_bytes()

        PY_LANGUAGE = Language(tspython.language())
        parser      = Parser(PY_LANGUAGE)
        tree        = parser.parse(source)

        nodes = []
        edges = []
        lines = source.count(b'\n') + 1

        nodes.append(
            f'    "{filepath}" [type="file", line_start=1, line_end={lines}];'
        )

        def escape(s: str) -> str:
            return s.replace('"', '\\"').replace('\n', ' ')

        def node_text(n) -> str:
            return source[n.start_byte:n.end_byte].decode("utf-8", errors="replace")

        # Walk the tree directly — avoids query API version differences
        def walk(node, parent_class_id=None, parent_class_end=0):
            if node.type == "import_from_statement":
                mod_node = node.child_by_field_name("module_name")
                if mod_node:
                    mod = node_text(mod_node)
                    edges.append(f'    "{filepath}" -> "{mod}" [label="imports"];')

            elif node.type == "import_statement":
                for child in node.children:
                    if child.type in ("dotted_name", "aliased_import"):
                        mod = node_text(child).split(" as ")[0].strip()
                        edges.append(f'    "{filepath}" -> "{mod}" [label="imports"];')

            elif node.type == "class_definition":
                name_node = node.child_by_field_name("name")
                if name_node:
                    class_name = node_text(name_node)
                    class_id   = f"{filepath}::{class_name}"
                    sig        = escape(f"class {class_name}")
                    nodes.append(
                        f'    "{class_id}" [type="class", file="{filepath}", '
                        f'line_start={node.start_point[0]+1}, '
                        f'line_end={node.end_point[0]+1}, '
                        f'signature="{sig}"];'
                    )
                    edges.append(f'    "{filepath}" -> "{class_id}" [label="contains"];')
                    # Recurse into class body with class context
                    body = node.child_by_field_name("body")
                    if body:
                        for child in body.children:
                            walk(child, parent_class_id=class_id,
                                 parent_class_end=node.end_byte)
                    return  # already walked body

            elif node.type in ("function_definition", "decorated_definition"):
                fn_node = node
                if node.type == "decorated_definition":
                    fn_node = node.child_by_field_name("definition") or node
                name_node = fn_node.child_by_field_name("name")
                if name_node:
                    fn_name   = node_text(name_node)
                    parent_id = parent_class_id if parent_class_id else filepath
                    func_id   = f"{parent_id}::{fn_name}"
                    sig       = escape(fn_name + "(...)")
                    nodes.append(
                        f'    "{func_id}" [type="function", file="{filepath}", '
                        f'line_start={fn_node.start_point[0]+1}, '
                        f'line_end={fn_node.end_point[0]+1}, '
                        f'signature="{sig}"];'
                    )
                    edges.append(f'    "{parent_id}" -> "{func_id}" [label="contains"];')
                return  # don't recurse into function bodies

            # Recurse for all other node types
            for child in node.children:
                walk(child, parent_class_id, parent_class_end)

        walk(tree.root_node)

        self._write_dot_section(filepath, nodes, edges, dot_path)
        logger.info(f"graph: {filepath} → {len(nodes)} nodes, {len(edges)} edges")

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
            start_line = source[:m.start()].count('\n') + 1
            end_line   = source[:m.end()].count('\n') + 1
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

    def get_graph_dot(self) -> str:
        """Return current graph.dot contents."""
        dot_path = self.repo_path / ".residuality" / "graph.dot"
        if dot_path.exists():
            return dot_path.read_text()
        return "digraph residuality {\n\n}\n"

    def get_node_at_line(self, filepath: str, line: int) -> Optional[dict]:
        """Return the graph node (function/section) containing a given line."""
        import pydot
        dot_path = self.repo_path / ".residuality" / "graph.dot"
        if not dot_path.exists():
            return None
        try:
            graphs = pydot.graph_from_dot_file(str(dot_path))
            if not graphs:
                return None
            graph = graphs[0]
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
