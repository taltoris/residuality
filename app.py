"""
app.py — Residuality Flask Application
AI-assisted artifact versioning with git + Qdrant + Open WebUI.
"""

import os
import json
import logging
import zipfile
import threading
from pathlib import Path
from functools import wraps
from datetime import datetime

from flask import (
    Flask, render_template, request, redirect,
    url_for, jsonify, session, send_file, flash, abort
)
from dotenv import load_dotenv

load_dotenv()

from artifact  import ArtifactRepo
from indexer   import ensure_collections, index_commit, search_commits, search_graph_nodes
from snapshot  import (ensure_collection as ensure_snapshot_collection,
                       detect_project_type, generate_snapshot,
                       store_snapshot, search_snapshots,
                       format_snapshot_for_context)
from context   import ContextBuilder
from owui_client import OWUIClient
from graph import render_file_graph, render_file_detail, load_graph, get_node_by_id, get_neighbors, export_prose

# ── Logging ───────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s"
)
logger = logging.getLogger("residuality")

# ── App setup ─────────────────────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "change-me")

REPOS_PATH   = os.getenv("REPOS_PATH", "/repos")
RES_USERNAME = os.getenv("RESIDUALITY_USERNAME", "admin")
RES_PASSWORD = os.getenv("RESIDUALITY_PASSWORD", "changeme")

ctx_builder = ContextBuilder()
owui        = OWUIClient()

# ── Auth ──────────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login", next=request.url))
        return f(*args, **kwargs)
    return decorated


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        if (request.form["username"] == RES_USERNAME and
                request.form["password"] == RES_PASSWORD):
            session["logged_in"] = True
            return redirect(request.args.get("next") or url_for("projects"))
        error = "Invalid credentials"
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# ── Helpers ───────────────────────────────────────────────────────────────

def list_projects() -> list:
    repos = Path(REPOS_PATH)
    repos.mkdir(parents=True, exist_ok=True)
    projects = []
    for d in sorted(repos.iterdir()):
        if d.is_dir() and (d / ".git").exists():
            projects.append({
                "id":          d.name,
                "path":        str(d),
                "initialized": (d / ".residuality" / "graph.dot").exists(),
            })
    return projects


def get_repo(project_id: str) -> ArtifactRepo:
    repo = ArtifactRepo(project_id)
    if not repo.exists():
        abort(404, f"Project '{project_id}' not found")
    return repo


def index_commit_async(repo: ArtifactRepo, commit_hash: str,
                        project_id: str, artifact_path: str,
                        message: str, model_used: str = "",
                        summary: str = None, turn: int = 0) -> None:
    """Index a commit in Qdrant and generate an episodic snapshot."""
    def _index():
        try:
            c = repo.repo.commit(commit_hash)
            index_commit({
                "commit_hash":   commit_hash,
                "project_id":    project_id,
                "branch":        repo._current_branch(),
                "parent_hashes": [p.hexsha for p in c.parents],
                "artifact_path": artifact_path,
                "message":       message,
                "timestamp":     c.committed_date,
                "is_merge":      len(c.parents) > 1,
                "model_used":    model_used,
            })
            project_type = detect_project_type(repo.repo_path)
            snapshot     = generate_snapshot(
                project_id       = project_id,
                commit_hash      = commit_hash,
                commit_message   = message,
                summary          = summary,
                project_type     = project_type,
                owui_client      = owui,
                summarizer_model = owui.SUMMARIZER_MODEL,
            )
            if snapshot:
                store_snapshot(
                    project_id     = project_id,
                    commit_hash    = commit_hash,
                    commit_message = message,
                    snapshot       = snapshot,
                    turn           = turn,
                    timestamp      = c.committed_date,
                )
                logger.info(f"Snapshot stored for {commit_hash[:8]}: {snapshot.get('state','')[:80]}")
        except Exception as e:
            logger.warning(f"Async index failed: {e}")
    threading.Thread(target=_index, daemon=True).start()

# ── Project routes ────────────────────────────────────────────────────────

@app.route("/")
@login_required
def projects():
    return render_template("projects.html", projects=list_projects(),
                           repos_path=REPOS_PATH)


@app.route("/projects/new", methods=["GET", "POST"])
@login_required
def new_project():
    if request.method == "POST":
        project_id  = request.form["project_id"].strip().replace(" ", "_")
        description = request.form.get("description", "").strip()
        if not project_id:
            flash("Project ID is required", "error")
            return render_template("new_project.html")
        try:
            repo_path  = Path(REPOS_PATH) / project_id
            git_exists = (repo_path / ".git").exists()
            ArtifactRepo.create(project_id, description)
            if git_exists:
                flash(f"Project '{project_id}' linked to existing git repo", "success")
            else:
                flash(f"Project '{project_id}' created", "success")
            return redirect(url_for("dag", project_id=project_id))
        except Exception as e:
            flash(str(e), "error")
    existing = []
    repos = Path(REPOS_PATH)
    if repos.exists():
        linked = {p["id"] for p in list_projects()}
        for d in sorted(repos.iterdir()):
            if d.is_dir() and (d / ".git").exists() and d.name not in linked:
                existing.append(d.name)
    return render_template("new_project.html", existing=existing)


@app.route("/projects/<project_id>")
@login_required
def dag(project_id: str):
    repo  = get_repo(project_id)
    nodes = repo.get_dag()
    return render_template("dag.html", project_id=project_id, dag_nodes=nodes)


@app.route("/projects/<project_id>/commit/<commit_hash>")
@login_required
def view_commit(project_id: str, commit_hash: str):
    repo  = get_repo(project_id)
    files = repo.list_files(commit_hash)
    file_contents = {}
    for f in files:
        try:
            file_contents[f] = repo.read(f, commit_hash)
        except Exception:
            file_contents[f] = "(error reading file)"
    c = repo.repo.commit(commit_hash)
    commit_info = {
        "hash":    commit_hash,
        "short":   commit_hash[:8],
        "message": c.message,
        "date":    datetime.fromtimestamp(c.committed_date).strftime("%Y-%m-%d %H:%M"),
        "parents": [p.hexsha[:8] for p in c.parents],
    }
    return render_template("commit.html",
                           project_id=project_id,
                           commit=commit_info,
                           files=file_contents)


@app.route("/projects/<project_id>/diff/<commit_a>/<commit_b>")
@login_required
def diff_view(project_id: str, commit_a: str, commit_b: str):
    repo      = get_repo(project_id)
    diff_text = repo.diff(commit_a, commit_b)
    return render_template("diff.html",
                           project_id=project_id,
                           commit_a=commit_a[:8],
                           commit_b=commit_b[:8],
                           diff=diff_text)


@app.route("/projects/<project_id>/branch", methods=["POST"])
@login_required
def create_branch(project_id: str):
    repo        = get_repo(project_id)
    name        = request.form["name"].strip()
    from_commit = request.form["from_commit"].strip()
    reason      = request.form.get("reason", "").strip()
    try:
        repo.branch(name, from_commit, reason)
        flash(f"Branch '{name}' created from {from_commit[:8]}", "success")
    except Exception as e:
        flash(str(e), "error")
    return redirect(url_for("dag", project_id=project_id))


@app.route("/projects/<project_id>/checkout/<commit_hash>", methods=["POST"])
@login_required
def checkout(project_id: str, commit_hash: str):
    repo = get_repo(project_id)
    try:
        repo.checkout(commit_hash)
        flash(f"Checked out {commit_hash[:8]}", "success")
    except Exception as e:
        flash(str(e), "error")
    return redirect(url_for("dag", project_id=project_id))

# ── Search ────────────────────────────────────────────────────────────────

@app.route("/projects/<project_id>/search", methods=["GET", "POST"])
@login_required
def search(project_id: str):
    results = []
    query   = ""
    mode    = "commits"
    if request.method == "POST":
        query  = request.form.get("query", "").strip()
        mode   = request.form.get("mode", "commits")
        if query:
            if mode == "commits":
                results = search_commits(query, project_id=project_id)
            else:
                results = search_graph_nodes(query, project_id=project_id)
    return render_template("search.html",
                           project_id=project_id,
                           query=query,
                           mode=mode,
                           results=results)

# ── Chat ──────────────────────────────────────────────────────────────────

_conversation_state: dict = {}


@app.route("/projects/<project_id>/chat", methods=["GET"])
@login_required
def chat(project_id: str):
    repo  = get_repo(project_id)
    files = repo.list_files()
    state = _conversation_state.get(project_id, {})
    return render_template("chat.html",
                           project_id=project_id,
                           files=files,
                           summary=state.get("summary"),
                           history=state.get("history", []))


@app.route("/projects/<project_id>/chat", methods=["POST"])
@login_required
def chat_send(project_id: str):
    repo         = get_repo(project_id)
    user_message = request.form.get("message", "").strip()
    artifact_path = request.form.get("artifact_path", "").strip() or None

    if not user_message:
        return jsonify({"error": "Empty message"}), 400

    state   = _conversation_state.setdefault(project_id, {})
    summary = state.get("summary")
    history = state.get("history", [])

    artifact_content = None
    artifact_label   = None
    node_id          = request.form.get("node_id", "").strip() or None

    if node_id:
        dot_path = repo.repo_path / ".residuality" / "graph.dot"
        import pydot as _pydot
        graphs = _pydot.graph_from_dot_file(str(dot_path))
        if graphs:
            node = get_node_by_id(graphs[0], node_id)
            if node and node.get("file"):
                try:
                    file_lines = repo.read(node["file"]).splitlines()
                    artifact_content = "\n".join(
                        file_lines[node["line_start"]-1 : node["line_end"]]
                    )
                    artifact_label = f"{node['file']} :: {node_id} (lines {node['line_start']}-{node['line_end']})"
                except Exception as e:
                    logger.warning(f"Could not read node {node_id}: {e}")

    elif artifact_path:
        dot_path = repo.repo_path / ".residuality" / "graph.dot"
        is_code  = any(artifact_path.endswith(ext) for ext in
                      ('.py', '.js', '.ts', '.cpp', '.c', '.h', '.rs', '.go'))
        if is_code and dot_path.exists():
            import pydot as _pydot
            graph = load_graph(str(dot_path))
            if graph:
                node_results = search_graph_nodes(user_message, project_id=project_id, limit=1)
                if node_results and node_results[0].get("file") == artifact_path:
                    best = node_results[0]
                    try:
                        file_lines = repo.read(artifact_path).splitlines()
                        artifact_content = "\n".join(
                            file_lines[best["line_start"]-1 : best["line_end"]]
                        )
                        artifact_label = f"{artifact_path} :: {best['node_id']} (lines {best['line_start']}-{best['line_end']})"
                    except Exception as e:
                        logger.warning(f"Could not read node content: {e}")

        if not artifact_content:
            try:
                artifact_content = repo.read(artifact_path)
                artifact_label   = artifact_path
            except Exception:
                pass

    last_exchange    = history[-1] if history else None
    recent_exchanges = history[-3:] if history else []

    COMMIT_KEYWORDS = {
        'commit', 'version', 'when', 'which', 'find', 'search', 'history',
        'remember', 'worked', 'broke', 'changed', 'last', 'before', 'after',
        'birthday', 'memory', 'recall', 'know', 'told', 'said', 'mentioned'
    }
    CODE_KEYWORDS = {
        'function', 'class', 'method', 'def', 'where', 'file', 'code',
        'implements', 'handles', 'does', 'defined', 'located'
    }
    words = set(user_message.lower().split())

    relevant_commits   = []
    relevant_nodes     = []
    relevant_snapshots = []

    if words & COMMIT_KEYWORDS:
        relevant_commits   = search_commits(user_message, project_id=project_id, limit=5)
        relevant_snapshots = search_snapshots(user_message, project_id=project_id, limit=3)
        logger.info(f"Chat search: {len(relevant_commits)} commits, {len(relevant_snapshots)} snapshots")

    if words & CODE_KEYWORDS:
        relevant_nodes = search_graph_nodes(user_message, project_id=project_id, limit=5)
        logger.info(f"Chat search: {len(relevant_nodes)} code nodes")

    messages = ctx_builder.build_chat_context(
        project_id=project_id,
        user_message=user_message,
        rolling_summary=summary,
        last_exchange=last_exchange,
        recent_exchanges=recent_exchanges,
        artifact_content=artifact_content,
        artifact_path=artifact_label or artifact_path,
        relevant_commits=relevant_commits,
        relevant_nodes=relevant_nodes,
        relevant_snapshots=relevant_snapshots,
    )

    try:
        response = owui.chat(messages)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    history.append({"user": user_message, "assistant": response})
    if len(history) > 20:
        history = history[-20:]
    state["history"] = history

    def _update_summary():
        try:
            summary_messages = [{
                "role": "user",
                "content": (
                    f"Previous summary: {summary or 'None'}\n\n"
                    f"New exchange:\nUser: {user_message}\n"
                    f"Assistant: {response[:500]}\n\n"
                    f"Write an updated 2-3 sentence summary. Return only the summary text."
                )
            }]
            new_summary = owui.chat(summary_messages, model=owui.SUMMARIZER_MODEL)
            state["summary"] = new_summary
        except Exception as e:
            logger.warning(f"Summary update failed: {e}")
    threading.Thread(target=_update_summary, daemon=True).start()

    return jsonify({"response": response, "summary": summary})

# ── Graph ─────────────────────────────────────────────────────────────────

@app.route("/projects/<project_id>/graph")
@login_required
def graph_view(project_id: str):
    repo        = get_repo(project_id)
    dot_path    = str(repo.repo_path / ".residuality" / "graph.dot")
    dot_content = repo.get_graph_dot()
    svg         = render_file_graph(dot_path)
    return render_template("graph.html",
                           project_id=project_id,
                           svg=svg,
                           dot=dot_content)


@app.route("/projects/<project_id>/graph/svg")
@login_required
def graph_svg(project_id: str):
    """Return file graph SVG, optionally with external pip imports."""
    repo     = get_repo(project_id)
    dot_path = str(repo.repo_path / ".residuality" / "graph.dot")
    show_ext = request.args.get("external", "false").lower() == "true"
    svg      = render_file_graph(dot_path, show_external=show_ext)
    return jsonify({"svg": svg})


@app.route("/projects/<project_id>/graph/file/<path:filepath>")
@login_required
def graph_file_detail(project_id: str, filepath: str):
    """Return file detail SVG showing functions, classes, imports."""
    repo     = get_repo(project_id)
    dot_path = str(repo.repo_path / ".residuality" / "graph.dot")
    svg      = render_file_detail(dot_path, filepath)
    return jsonify({"svg": svg})


@app.route("/projects/<project_id>/graph/node/<path:node_id>")
@login_required
def graph_node(project_id: str, node_id: str):
    repo     = get_repo(project_id)
    dot_path = str(repo.repo_path / ".residuality" / "graph.dot")
    graph    = load_graph(dot_path)
    if not graph:
        abort(404, "Graph not found")
    node      = get_node_by_id(graph, node_id)
    neighbors = get_neighbors(graph, node_id)
    content   = None
    if node and node.get("file") and node.get("line_start"):
        try:
            file_lines = repo.read(node["file"]).splitlines()
            content = "\n".join(
                file_lines[node["line_start"] - 1 : node["line_end"]]
            )
        except Exception:
            pass
    return render_template("graph_node.html",
                           project_id=project_id,
                           node=node,
                           neighbors=neighbors,
                           content=content)


@app.route("/projects/<project_id>/graph/build_all", methods=["POST"])
@login_required
def build_graph_all(project_id: str):
    repo      = get_repo(project_id)
    errors    = []
    processed = []

    gitignore_path = repo.repo_path / ".gitignore"
    if not gitignore_path.exists():
        gitignore_path.write_text(
            "# Residuality default .gitignore\n"
            "__pycache__/\n*.pyc\n*.pyo\n*.pyd\n"
            "build/\ndist/\n*.egg-info/\n.eggs/\n"
            "*.so\n*.o\n*.a\n*.out\n*.log\nexport/\n.DS_Store\n"
        )

    skip_dirs  = {'.git', '.residuality', '__pycache__', 'node_modules', 'export'}
    skip_exts  = {'.pyc', '.pyo', '.pyd', '.so', '.o', '.a', '.out', '.log',
                  '.bin', '.onnx', '.pkl', '.pt', '.pth', '.h5', '.model'}
    size_limit = 500 * 1024

    for f in repo.repo_path.rglob("*"):
        if f.is_dir():
            continue
        rel_parts = f.parts[len(repo.repo_path.parts):]
        if any(p in skip_dirs or p.startswith('.') for p in rel_parts):
            continue
        if f.suffix in skip_exts:
            continue
        try:
            size = f.stat().st_size
        except Exception as e:
            errors.append(f"stat {f}: {e}")
            continue
        if size > size_limit:
            continue

        rel = str(f.relative_to(repo.repo_path))
        logger.info(f"Processing: {rel}")
        try:
            repo._update_graph(rel)
            processed.append(rel)
        except Exception as e:
            error_msg = f"{rel}: {type(e).__name__}: {e}"
            logger.error(error_msg, exc_info=True)
            errors.append(error_msg)

    try:
        repo.repo.git.add(".residuality/graph.dot")
        if gitignore_path.exists():
            repo.repo.git.add(".gitignore")
        if repo.repo.is_dirty(index=True):
            repo.repo.index.commit(
                f"Built graph from {len(processed)} files"
                + (f" ({len(errors)} errors)" if errors else "")
            )
    except Exception as e:
        errors.append(f"commit: {type(e).__name__}: {e}")

    return jsonify({
        "status":    "ok" if not errors else "partial",
        "processed": len(processed),
        "files":     processed,
        "errors":    errors,
    })


@app.route("/projects/<project_id>/gitignore", methods=["GET"])
@login_required
def get_gitignore(project_id: str):
    repo = get_repo(project_id)
    gitignore_path = repo.repo_path / ".gitignore"
    if not gitignore_path.exists():
        return jsonify({"entries": []})
    entries = [
        line for line in gitignore_path.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]
    return jsonify({"entries": entries})


@app.route("/projects/<project_id>/gitignore/add", methods=["POST"])
@login_required
def add_gitignore(project_id: str):
    repo    = get_repo(project_id)
    pattern = request.form.get("pattern", "").strip()
    if not pattern:
        return jsonify({"error": "No pattern provided"}), 400
    gitignore_path = repo.repo_path / ".gitignore"
    existing = gitignore_path.read_text() if gitignore_path.exists() else ""
    if pattern not in existing.splitlines():
        with open(gitignore_path, "a") as f:
            f.write(f"\n{pattern}")
    return jsonify({"status": "ok", "pattern": pattern})


@app.route("/projects/<project_id>/gitignore/remove", methods=["POST"])
@login_required
def remove_gitignore(project_id: str):
    repo    = get_repo(project_id)
    pattern = request.form.get("pattern", "").strip()
    if not pattern:
        return jsonify({"error": "No pattern provided"}), 400
    gitignore_path = repo.repo_path / ".gitignore"
    if gitignore_path.exists():
        lines = [
            l for l in gitignore_path.read_text().splitlines()
            if l.strip() != pattern
        ]
        gitignore_path.write_text("\n".join(lines) + "\n")
    return jsonify({"status": "ok"})


@app.route("/projects/<project_id>/git_status")
@login_required
def git_status(project_id: str):
    repo = get_repo(project_id)
    try:
        files = []
        try:
            status_output = repo.repo.git.status("--porcelain", "-uall")
        except Exception:
            status_output = repo.repo.git.status("--porcelain")

        for line in status_output.splitlines():
            if not line.strip():
                continue
            xy   = line[:2]
            path = line[3:].strip()
            status = xy.strip() or "M"
            if " -> " in path:
                path = path.split(" -> ")[-1]
            full_path = repo.repo_path / path
            try:
                size = full_path.stat().st_size if full_path.exists() else 0
            except Exception:
                size = 0
            files.append({"path": path, "status": status, "size": size})

        if not files:
            try:
                for path in repo.repo.untracked_files:
                    full_path = repo.repo_path / path
                    try:
                        size = full_path.stat().st_size if full_path.exists() else 0
                    except Exception:
                        size = 0
                    files.append({"path": path, "status": "??", "size": size})
            except Exception as e:
                logger.warning(f"untracked_files error: {e}")

        return jsonify({"files": files})
    except Exception as e:
        logger.error(f"git_status error: {e}", exc_info=True)
        return jsonify({"files": [], "error": str(e)}), 500


@app.route("/projects/<project_id>/graph/regenerate_and_commit", methods=["POST"])
@login_required
def regenerate_and_commit(project_id: str):
    repo    = get_repo(project_id)
    message = request.form.get("message", "Update files").strip() or "Update files"
    files   = request.form.getlist("files")

    if not files:
        return jsonify({"status": "error", "error": "No files selected"}), 400

    errors = []
    try:
        repo.repo.index.add(files)
    except Exception as e:
        errors.append(f"stage: {e}")

    for filepath in files:
        try:
            repo._update_graph(filepath)
        except Exception as e:
            errors.append(f"graph {filepath}: {e}")

    try:
        repo.repo.index.add([".residuality/graph.dot"])
    except Exception as e:
        errors.append(f"stage graph.dot: {e}")

    try:
        commit = repo.repo.index.commit(message)
        index_commit_async(repo, commit.hexsha, project_id, "", message)
        return jsonify({"status": "ok", "hash": commit.hexsha[:8], "errors": errors})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e), "errors": errors}), 500


@app.route("/projects/<project_id>/suggest_commit_message", methods=["POST"])
@login_required
def suggest_commit_message(project_id: str):
    repo = get_repo(project_id)
    try:
        repo.repo.git.add(A=True)
        try:
            diff = repo.repo.git.diff("--cached", "HEAD")
        except Exception:
            diff = repo.repo.git.diff_index("--cached", "4b825dc642cb6eb9a060e54bf8d69288fbee4904")
        if not diff.strip():
            status = repo.repo.git.status("--short")
            diff = status or "General update"
        message = owui.generate_commit_message(diff)
        return jsonify({"message": message})
    except Exception as e:
        logger.warning(f"suggest_commit_message error: {e}")
        return jsonify({"message": "Update files", "error": str(e)})


@app.route("/projects/<project_id>/commit_all", methods=["POST"])
@login_required
def commit_all(project_id: str):
    repo    = get_repo(project_id)
    message = request.form.get("message", "Update files").strip() or "Update files"
    try:
        repo.repo.git.add(A=True)
        try:
            has_changes = bool(repo.repo.git.diff("--cached", "HEAD").strip())
        except Exception:
            has_changes = True
        if has_changes:
            commit = repo.repo.index.commit(message)
            return jsonify({"status": "ok", "hash": commit.hexsha[:8]})
        else:
            return jsonify({"status": "clean", "hash": None})
    except Exception as e:
        logger.error(f"commit_all failed: {e}")
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/projects/<project_id>/graph/regenerate", methods=["POST"])
@login_required
def regenerate_graph(project_id: str):
    repo      = get_repo(project_id)
    errors    = []
    all_files = []
    for ext in ("*.py", "*.md"):
        for f in repo.repo_path.rglob(ext):
            parts = f.parts
            if any(p.startswith('.') or p == 'export' for p in parts):
                continue
            all_files.append(str(f.relative_to(repo.repo_path)))

    for filepath in all_files:
        try:
            repo._update_graph(filepath)
        except Exception as e:
            errors.append(f"{filepath}: {e}")

    try:
        repo.repo.git.add(".residuality/graph.dot")
        if repo.repo.is_dirty(index=True):
            repo.repo.index.commit("Regenerated graph.dot")
    except Exception as e:
        errors.append(f"commit: {e}")

    if errors:
        return jsonify({"status": "partial", "errors": errors})
    return jsonify({"status": "ok"})


@app.route("/projects/<project_id>/graph/search", methods=["POST"])
@login_required
def graph_search(project_id: str):
    query   = request.form.get("query", "").strip()
    results = search_graph_nodes(query, project_id=project_id) if query else []
    return jsonify(results)


@app.route("/projects/<project_id>/graph/edit", methods=["POST"])
@login_required
def graph_edit(project_id: str):
    repo        = get_repo(project_id)
    node_id     = request.form.get("node_id", "").strip()
    instruction = request.form.get("instruction", "").strip()

    dot_path = str(repo.repo_path / ".residuality" / "graph.dot")
    graph    = load_graph(dot_path)
    if not graph:
        return jsonify({"error": "Graph not found"}), 404

    node = get_node_by_id(graph, node_id)
    if not node:
        return jsonify({"error": f"Node '{node_id}' not found"}), 404

    try:
        file_lines   = repo.read(node["file"]).splitlines()
        node_content = "\n".join(file_lines[node["line_start"] - 1 : node["line_end"]])
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    state   = _conversation_state.get(project_id, {})
    summary = state.get("summary")

    messages = ctx_builder.build_edit_context(
        node_content=node_content,
        node_id=node_id,
        instruction=instruction,
        rolling_summary=summary,
        file_path=node["file"],
        line_start=node["line_start"],
        line_end=node["line_end"],
    )

    try:
        new_content = owui.chat(messages)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    edit = {"line_start": node["line_start"], "line_end": node["line_end"], "new_content": new_content}
    commit_msg = f"Edit {node_id}: {instruction[:80]}"
    try:
        commit_hash = repo.apply_edits(node["file"], [edit], commit_msg)
        index_commit_async(repo, commit_hash, project_id, node["file"], commit_msg)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({"commit_hash": commit_hash[:8], "new_content": new_content})

# ── Merge ─────────────────────────────────────────────────────────────────

@app.route("/projects/<project_id>/merge", methods=["GET", "POST"])
@login_required
def merge(project_id: str):
    repo = get_repo(project_id)
    if request.method == "POST":
        commit_a    = request.form["commit_a"].strip()
        commit_b    = request.form["commit_b"].strip()
        instruction = request.form.get("instruction", "Reconcile these two versions").strip()

        files_a   = repo.list_files(commit_a)
        files_b   = repo.list_files(commit_b)
        all_files = list(set(files_a + files_b))
        reconciled = {}

        for f in all_files:
            try:
                content_a = repo.read(f, commit_a)
            except Exception:
                content_a = ""
            try:
                content_b = repo.read(f, commit_b)
            except Exception:
                content_b = ""
            if content_a == content_b:
                reconciled[f] = content_a
                continue
            diff     = repo.diff(commit_a, commit_b)
            messages = ctx_builder.build_merge_context(
                content_a=content_a, content_b=content_b,
                diff=diff, instruction=instruction,
            )
            try:
                reconciled[f] = owui.chat(messages)
            except Exception as e:
                reconciled[f] = content_a
                logger.warning(f"Merge reconciliation failed for {f}: {e}")

        merge_message = f"Merge {commit_a[:8]} + {commit_b[:8]}: {instruction[:80]}"
        try:
            commit_hash = repo.merge_commits(commit_a, commit_b, reconciled, merge_message)
            index_commit_async(repo, commit_hash, project_id, "", merge_message)
            flash(f"Merged -> {commit_hash[:8]}", "success")
        except Exception as e:
            flash(str(e), "error")
        return redirect(url_for("dag", project_id=project_id))

    dag_nodes = repo.get_dag()
    return render_template("merge.html", project_id=project_id, dag_nodes=dag_nodes)

# ── Export ────────────────────────────────────────────────────────────────

@app.route("/projects/<project_id>/export", methods=["GET", "POST"])
@login_required
def export(project_id: str):
    repo = get_repo(project_id)
    if request.method == "POST":
        export_dir = repo.repo_path / "export"
        exported   = export_prose(str(repo.repo_path), str(export_dir))
        zip_path   = repo.repo_path / f"{project_id}_export.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            for f in exported:
                zf.write(f, Path(f).name)
        return send_file(str(zip_path), as_attachment=True,
                         download_name=f"{project_id}_export.zip")
    return render_template("export.html", project_id=project_id)

# ── Settings ──────────────────────────────────────────────────────────────

@app.route("/projects/<project_id>/settings", methods=["GET", "POST"])
@login_required
def settings(project_id: str):
    repo = get_repo(project_id)
    if request.method == "POST":
        action = request.form.get("action")
        if action == "add_remote":
            url  = request.form.get("remote_url", "").strip()
            name = request.form.get("remote_name", "origin").strip()
            if url:
                try:
                    repo.add_remote(url, name)
                    flash(f"Remote '{name}' added", "success")
                except Exception as e:
                    flash(str(e), "error")
        elif action == "push":
            remote = request.form.get("remote_name", "origin").strip()
            branch = request.form.get("branch", "main").strip()
            try:
                repo.push(remote, branch)
                flash(f"Pushed to {remote}/{branch}", "success")
            except Exception as e:
                flash(str(e), "error")
        elif action == "pull":
            remote = request.form.get("remote_name", "origin").strip()
            branch = request.form.get("branch", "main").strip()
            try:
                repo.pull(remote, branch)
                flash(f"Pulled from {remote}/{branch}", "success")
            except Exception as e:
                flash(str(e), "error")
        elif action == "remove_remote":
            name = request.form.get("remote_name", "origin").strip()
            try:
                repo.remove_remote(name)
                flash(f"Remote '{name}' removed", "success")
            except Exception as e:
                flash(str(e), "error")
        return redirect(url_for("settings", project_id=project_id))

    remotes = repo.get_remotes()
    return render_template("settings.html", project_id=project_id, remotes=remotes)

@app.route("/projects/<project_id>/graph/tree")
@login_required
def graph_tree(project_id: str):
    """
    Return the full project tree as JSON for the React graph explorer.
    Includes: directory structure, file nodes, internal structure, import edges.
    """
    repo     = get_repo(project_id)
    dot_path = str(repo.repo_path / ".residuality" / "graph.dot")
    graph    = load_graph(dot_path)

    # Build directory tree from filesystem
    def build_tree(path: Path, rel: str = "") -> dict:
        node = {
            "name":     path.name,
            "path":     rel or path.name,
            "type":     "directory" if path.is_dir() else "file",
            "children": [],
        }
        if path.is_dir():
            skip = {'.git', '.residuality', '__pycache__', 'node_modules', 'export'}
            for child in sorted(path.iterdir()):
                if child.name in skip or child.name.startswith('.'):
                    continue
                rel_child = str(child.relative_to(repo.repo_path))
                node["children"].append(build_tree(child, rel_child))
        return node

    tree = build_tree(repo.repo_path)

    # Build node map from graph.dot — internal structure per file
    file_nodes = {}   # filepath -> list of internal nodes
    import_edges = [] # {from, to, label}

    if graph:
        for node in graph.get_nodes():
            attrs    = node.get_attributes()
            nid      = node.get_name().strip('"')
            ntype    = attrs.get("type",       "").strip('"')
            nfile    = attrs.get("file",       "").strip('"')
            sig      = attrs.get("signature",  "").strip('"')
            label_v  = attrs.get("label",      "").strip('"')
            ls       = attrs.get("line_start", "").strip('"')
            le       = attrs.get("line_end",   "").strip('"')

            if ntype in ("file", "chapter"):
                continue  # file-level nodes handled by tree

            if nfile:
                if nfile not in file_nodes:
                    file_nodes[nfile] = []
                file_nodes[nfile].append({
                    "id":         nid,
                    "type":       ntype,
                    "label":      sig or label_v or nid.split("::")[-1],
                    "line_start": int(ls) if ls else 0,
                    "line_end":   int(le) if le else 0,
                })

        for edge in graph.get_edges():
            lbl = edge.get_attributes().get("label", "").strip('"')
            if lbl == "imports":
                import_edges.append({
                    "from":  edge.get_source().strip('"'),
                    "to":    edge.get_destination().strip('"'),
                    "label": lbl,
                })

    return jsonify({
        "tree":         tree,
        "file_nodes":   file_nodes,
        "import_edges": import_edges,
    })


@app.route("/projects/<project_id>/graph/node_content/<path:node_id>")
@login_required
def graph_node_content(project_id: str, node_id: str):
    """Return the source code for a specific node (function/class/file)."""
    repo     = get_repo(project_id)
    dot_path = str(repo.repo_path / ".residuality" / "graph.dot")
    graph    = load_graph(dot_path)
    if not graph:
        return jsonify({"error": "Graph not found"}), 404

    node = get_node_by_id(graph, node_id)

    # If it's a file node, return full file
    if not node:
        try:
            content = repo.read(node_id)
            return jsonify({"content": content, "type": "file"})
        except Exception as e:
            return jsonify({"error": str(e)}), 404

    # Return just the node's lines
    try:
        file_lines = repo.read(node["file"]).splitlines()
        content    = "\n".join(
            file_lines[node["line_start"] - 1 : node["line_end"]]
        )
        return jsonify({
            "content":    content,
            "type":       node["type"],
            "file":       node["file"],
            "line_start": node["line_start"],
            "line_end":   node["line_end"],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/projects/<project_id>/graph/file_content/<path:filepath>")
@login_required
def graph_file_content(project_id: str, filepath: str):
    """Return full source of a file."""
    repo = get_repo(project_id)
    try:
        content = repo.read(filepath)
        return jsonify({"content": content, "filepath": filepath})
    except Exception as e:
        return jsonify({"error": str(e)}), 404


@app.route("/projects/<project_id>/graph/explorer")
@login_required
def graph_explorer(project_id: str):
    """Serve the React graph explorer page."""
    repo = get_repo(project_id)
    return render_template("graph_explorer.html", project_id=project_id)

    
# ── API endpoints ─────────────────────────────────────────────────────────

@app.route("/api/projects")
@login_required
def api_projects():
    return jsonify(list_projects())


@app.route("/api/projects/<project_id>/dag")
@login_required
def api_dag(project_id: str):
    return jsonify(get_repo(project_id).get_dag())


@app.route("/api/health")
def api_health():
    return jsonify({"status": "ok", "owui": owui.health_check()})

# ── Startup ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ensure_collections()
    ensure_snapshot_collection()
    port = int(os.getenv("RESIDUALITY_PORT", 5010))
    logger.info(f"Residuality starting on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)

@app.route("/api/models")
@login_required
def api_models():
    """Proxy OWUI model list."""
    try:
        r = requests.get(
            f"{owui.base_url}/api/models",
            headers=owui.headers,
            timeout=10,
        )
        data = r.json()
        models = [
            {"id": m["id"], "name": m.get("name", m["id"])}
            for m in data.get("data", [])
            if not m.get("arena")  # skip arena model
        ]
        return jsonify({
            "models":          models,
            "current_chat":    owui.DEFAULT_MODEL,
            "current_planner": owui.PLANNER_MODEL,
            "current_summarizer": owui.SUMMARIZER_MODEL,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/models/select", methods=["POST"])
@login_required  
def api_models_select():
    """Update active model selections — persisted to config file."""
    import configparser
    
    chat_model      = request.form.get("chat_model",      "").strip()
    planner_model   = request.form.get("planner_model",   "").strip()
    summarizer_model = request.form.get("summarizer_model", "").strip()

    # Write to a config file that persists across restarts
    config_path = Path(REPOS_PATH).parent / "residuality_models.cfg"
    cfg = configparser.ConfigParser()
    cfg["models"] = {}
    if chat_model:       cfg["models"]["chat"]       = chat_model
    if planner_model:    cfg["models"]["planner"]     = planner_model
    if summarizer_model: cfg["models"]["summarizer"]  = summarizer_model
    with open(config_path, "w") as f:
        cfg.write(f)

    # Update in-memory client
    if chat_model:       owui.DEFAULT_MODEL    = chat_model
    if planner_model:    owui.PLANNER_MODEL    = planner_model
    if summarizer_model: owui.SUMMARIZER_MODEL = summarizer_model

    return jsonify({"status": "ok"})
