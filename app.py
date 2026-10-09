"""
app.py — Residuality Flask Application
AI-assisted artifact versioning with git + Qdrant + Open WebUI.
"""

import os
import re
import json
import logging
import zipfile
import threading
from pathlib import Path
from functools import wraps
from datetime import datetime
from urllib.parse import urlsplit

from flask import (
    Flask, render_template, request, redirect,
    url_for, jsonify, session, send_file, flash, abort
)
from dotenv import load_dotenv
from werkzeug.middleware.proxy_fix import ProxyFix

load_dotenv()

from artifact  import ArtifactRepo
from indexer   import ensure_collections, index_commit, search_commits, search_graph_nodes
from snapshot  import (ensure_collection as ensure_snapshot_collection,
                       detect_project_type, generate_snapshot,
                       store_snapshot, search_snapshots, latest_snapshot,
                       format_snapshot_for_context)
from context   import ContextBuilder
from owui_client import (
    OWUIClient, load_config, save_config, config_path, apply_model_config,
    resolve_model, resolve_max_edit_chars, list_models, strip_think, strip_code_fence,
    plan_edit_chunks, DEFAULT_EDIT_PROMPT, CONFIG_FILENAME, CONTEXT_TOKENS,
    reload_embedding, embed_config,
)
from graph import (render_file_graph, render_file_detail, render_directory_graph,
                    load_graph, get_node_by_id, get_neighbors, graph_node_ranges,
                    export_prose)

# ── Logging ───────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s"
)
logger = logging.getLogger("residuality")

# ── App setup ─────────────────────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "change-me")

# ── Behind a reverse proxy ────────────────────────────────────────────────
#
# Plain WSGI knows only what the socket says: Flask sees unencrypted HTTP to
# 127.0.0.1:5010 and has no idea a browser arrived over HTTPS at a public name.
# Everything generated from the request then carries that internal view —
# absolute URLs saying http://, and a session cookie that cannot be marked
# Secure.
#
# ProxyFix takes the `X-Forwarded-Proto` / `-Host` / `-For` headers the proxy
# sets and makes them the request's view of itself. The hop counts are the
# number of proxies in front (1 here), and they are *counts* rather than
# booleans on purpose: a caller can pre-seed those headers, and the count is
# what decides how many values to peel off, so getting it wrong is how a
# client forges its own scheme or address. Set TRUST_PROXY=false to turn this
# off when the app is reached with no proxy in front at all.
if os.getenv("TRUST_PROXY", "true").lower() in ("1", "true", "yes"):
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1,
                            x_port=1)

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # A Secure cookie is only ever sent over HTTPS. Off by default so the
    # direct http://host:5010 URL can still sign in during local work; turn it
    # on for a TLS-only deployment, which is what the public name is.
    SESSION_COOKIE_SECURE=os.getenv("SESSION_COOKIE_SECURE", "false").lower()
                            in ("1", "true", "yes"),
)

REPOS_PATH   = os.getenv("REPOS_PATH", "/repos")
RES_USERNAME = os.getenv("RESIDUALITY_USERNAME", "admin")
RES_PASSWORD = os.getenv("RESIDUALITY_PASSWORD", "changeme")

# The per-edit send limit lives with the rest of the model settings now
# (owui_client), derived from the configured context window rather than pinned
# to a constant, so it can be changed from the settings panel without a
# redeploy. Read it through resolve_max_edit_chars().

ctx_builder = ContextBuilder()
owui        = OWUIClient()


def reload_model_config() -> dict:
    """Re-read settings from disk and push them onto the live client.

    `model_cfg` is module state and the routes read it through `resolve_model`,
    so saving has to refresh it — otherwise a running process keeps serving
    whatever was on disk when it started.
    """
    global model_cfg
    model_cfg = load_config()
    apply_model_config(owui, model_cfg)
    # The embedding settings are read by the embed() block at the bottom of this
    # module rather than by the client above (the indexer and snapshot store call
    # it directly), so it has to be told too — otherwise a saved embed model
    # would not take effect until a restart.
    reload_embedding()
    return model_cfg


# Settings from the config file (.env underneath) applied at import, not in
# `__main__`, so they also take effect when the app is served by something else.
model_cfg = reload_model_config()

# ── Auth ──────────────────────────────────────────────────────────────────

def _relative_url() -> str:
    """The current request as a path on this site, never an absolute URL.

    `request.url` is always absolute, and that is what made this 403 behind the
    proxy: `?next=https://host/` is the exact shape of an open-redirect
    payload, so a proxy or WAF rule matching that shape answers it itself and
    the app is never reached. A path is also simply the more correct value — it
    survives the public name changing, and it cannot point off-site at all.
    """
    qs = request.query_string.decode("utf-8", "replace")
    path = request.script_root + request.path
    return f"{path}?{qs}" if qs else path


def _safe_next(target):
    """A post-login destination that stays on this site, or None.

    Only site-relative paths are allowed. `//evil.example` is an absolute URL
    wearing a path's clothes, so it is refused along with anything carrying a
    scheme or a netloc, and a backslash is refused too because browsers
    normalise it to `/` — which would turn `\\evil.example` back into that
    scheme-relative form after this check had passed. The login form still
    carries an attacker-supplied `next`, and an unchecked one makes the login
    page an open redirect.
    """
    if not target:
        return None
    if any(c in target for c in (chr(92), "\t", "\n", "\r")):  # chr(92) = \
        return None
    if target.startswith("//"):
        return None
    parts = urlsplit(target)
    if parts.scheme or parts.netloc:
        return None
    return target if target.startswith("/") else None


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login", next=_relative_url()))
        return f(*args, **kwargs)
    return decorated


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        if (request.form["username"] == RES_USERNAME and
                request.form["password"] == RES_PASSWORD):
            session["logged_in"] = True
            return redirect(_safe_next(request.args.get("next"))
                            or url_for("projects"))
        error = "Invalid credentials"
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/account")
@login_required
def account():
    """Who is signed in, and the way out.

    Reports the account rather than editing it: the credentials come from
    RESIDUALITY_USERNAME / RESIDUALITY_PASSWORD in the environment, so a
    password change here would mean writing a second source of truth for a
    secret that .env is meant to own.
    """
    return render_template("account.html", username=RES_USERNAME)

# ── Helpers ───────────────────────────────────────────────────────────────

def list_projects() -> list:
    """List all initialized projects in the repositories directory.

    Scans REPOS_PATH for subdirectories that contain a .git folder,
    treating each as a project. Returns metadata including whether the
    project has been processed by residuality (indicated by graph.dot).
    """
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


def _tracked_files(repo: ArtifactRepo) -> list:
    """Every tracked, non-gitignored file in the working tree.

    Deliberately not graph.dot: the directory view has to show Dockerfile,
    LICENSE, .env.example, requirements.txt and templates/*.html, none of which
    any parser handles. Files appear here from their first commit onward.
    """
    try:
        out = repo.repo.git.ls_files()
    except Exception as e:
        logger.warning(f"ls_files failed, falling back to HEAD tree: {e}")
        try:
            return repo.list_files()
        except Exception:
            return []
    return sorted(line.strip() for line in out.splitlines() if line.strip())


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
                summarizer_model = resolve_model(model_cfg, "default"),
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


# ── Global settings ───────────────────────────────────────────────────────

def settings_context() -> dict:
    """Template context for the global settings page.

    The model list is deliberately *not* fetched here. Saving has to work even
    when OWUI is unreachable, and a slow endpoint would otherwise stall the
    page — the panel fills its dropdowns from /api/models once the page is up,
    and shows the saved value as the current selection either way.
    """
    cfg = load_config()
    return {
        "model_cfg":           cfg,
        "resolved_chat":       resolve_model(cfg, "chat"),
        "resolved_edit":       resolve_model(cfg, "edit"),
        "api_key_set":         bool(cfg.get("owui_api_key")),
        "config_file":         str(config_path()),
        "edit_prompt_default": DEFAULT_EDIT_PROMPT,
        "context_tokens_default": CONTEXT_TOKENS,
        "resolved_max_edit_chars": resolve_max_edit_chars(cfg),
        "embed":                  embed_config(),
    }


@app.route("/settings")
@login_required
def settings_page():
    """The program-wide settings page: endpoint, key, models and prompts.

    Global rather than per-project on purpose — every project on this install
    talks to one endpoint with one key and one edit prompt, so a per-project
    copy of this form could only ever disagree with itself.
    """
    return render_template("settings.html", **settings_context())


@app.route("/settings/models", methods=["POST"])
@login_required
def settings_models():
    """Save the global model / connection / prompt settings.

    Blank means two different things, deliberately: for the model slots, the
    URL and the prompt it clears the override so the .env value applies again;
    for the API key it means "leave the stored key alone", because the field is
    rendered masked and is never echoed back to the browser.
    """
    cfg = load_config()

    for field in ("default", "chat", "edit", "planner", "summarizer",
                  "edit_prompt", "owui_url", "embed_url", "embed_model",
                  "embed_path"):
        if field in request.form:
            cfg[field] = request.form.get(field, "").strip()

    # Numeric settings are kept as text so blank can still mean "fall back to
    # .env"; only a non-number is refused, because a silently-ignored typo here
    # reads as "the setting does nothing".
    for field, label in (("context_tokens", "Context window"),
                         ("max_edit_chars", "Max characters per edit")):
        if field in request.form:
            raw = request.form.get(field, "").strip()
            if raw and not raw.isdigit():
                flash(f"{label} must be a whole number, or blank to use .env",
                      "error")
                return redirect(url_for("settings_page"))
            cfg[field] = raw

    if request.form.get("owui_api_key", "").strip():
        cfg["owui_api_key"] = request.form["owui_api_key"].strip()

    try:
        save_config(cfg)
    except Exception as e:
        flash(f"Could not write {CONFIG_FILENAME}: {e}", "error")
        return redirect(url_for("settings_page"))

    reload_model_config()
    flash("Settings saved", "success")
    return redirect(url_for("settings_page"))


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
            return redirect(url_for("graph_view", project_id=project_id))
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
def project_home(project_id: str):
    """The bare project URL: straight to Project Control.

    This path used to render the commit DAG. The graph is what this app is
    for, so the front door now opens on it — and every old link and redirect
    that aimed at the DAG lands somewhere that exists.
    """
    return redirect(url_for("graph_view", project_id=project_id))


@app.route("/projects/<project_id>/history")
@login_required
def history(project_id: str):
    """The running history: every commit, newest first, with search.

    A table rather than the node graph the old DAG page drew. The graph spent
    a CDN dependency and a canvas on showing parentage that a list with
    parents, branches and merge markers already conveys, and the list can be
    searched and acted on.
    """
    repo    = get_repo(project_id)
    commits = repo.get_commits(include_working=True)
    return render_template("history.html",
                           project_id=project_id,
                           commits=commits)


@app.route("/projects/<project_id>/history/search", methods=["POST"])
@login_required
def history_search(project_id: str):
    """Semantic search over commit history.

    Literal filtering happens in the browser over the list already on screen;
    this is for what a filter cannot serve — remembering *when* something
    happened, without knowing which words were in the message.
    """
    query = request.form.get("query", "").strip()
    if not query:
        return jsonify({"results": []})
    try:
        return jsonify({"results": search_commits(query, project_id=project_id)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


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
    # `working` is accepted as either side so the working tree can be compared
    # against any commit (see ArtifactRepo.diff).
    diff_text = repo.diff(commit_a, commit_b)
    return render_template("diff.html",
                           project_id=project_id,
                           commit_a=commit_a if commit_a.lower() == "working" else commit_a[:8],
                           commit_b=commit_b if commit_b.lower() == "working" else commit_b[:8],
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
    return redirect(url_for("history", project_id=project_id))


@app.route("/projects/<project_id>/checkout/<commit_hash>", methods=["POST"])
@login_required
def checkout(project_id: str, commit_hash: str):
    repo = get_repo(project_id)
    try:
        repo.checkout(commit_hash)
        flash(f"Checked out {commit_hash[:8]}", "success")
    except Exception as e:
        flash(str(e), "error")
    return redirect(url_for("history", project_id=project_id))

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

# ── Plan ──────────────────────────────────────────────────────────────────
#
# The Plan tab is this project's thinking space, and it is what the old Chat
# tab became. It holds a conversation with the *planner* model and, on demand,
# an assessment of where the work stands.
#
# Every message is sent with the same fresh context — recent git log, the
# project README and the graph — plus the history of the conversation the user
# is in. Conversations are stored per project under .residuality/chats/ (one
# JSON file each) so they survive a restart, and a new conversation starts with
# the injected context and nothing from any other conversation.

PLAN_LOG_COMMITS   = 10  # how far back the injected git log reaches
PLAN_HISTORY_LIMIT = 20  # exchanges of a conversation sent to the model


def _chats_dir(repo: ArtifactRepo) -> Path:
    d = repo.repo_path / ".residuality" / "chats"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _chat_id_ok(chat_id: str) -> bool:
    """Conversation ids are file names; keep them to safe characters."""
    return bool(re.fullmatch(r"[A-Za-z0-9._-]{1,64}", chat_id))


def _load_chat(repo: ArtifactRepo, chat_id: str) -> dict:
    path = _chats_dir(repo) / f"{chat_id}.json"
    if path.exists():
        try:
            data = json.loads(path.read_text())
            if isinstance(data, dict) and isinstance(data.get("history"), list):
                return data
        except Exception as e:
            logger.warning(f"Could not read chat {chat_id}: {e}")
    return {"id": chat_id, "title": "New conversation", "history": []}


def _save_chat(repo: ArtifactRepo, chat: dict) -> None:
    path = _chats_dir(repo) / f"{chat['id']}.json"
    tmp  = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(chat, indent=2))
    tmp.rename(path)


def _new_chat_id(repo: ArtifactRepo) -> str:
    existing = {p.stem for p in _chats_dir(repo).glob("*.json")}
    n = 1
    while f"chat-{n}" in existing:
        n += 1
    return f"chat-{n}"


def _chat_context_parts(repo: ArtifactRepo) -> list:
    """The context every chat message is sent with, read fresh each time.

    Recent git log, the README and an outline of the graph. Each part degrades
    to a note rather than failing the message when the file is missing.

    The graph is sent as a summary rather than as graph.dot itself: the raw
    file is ~48K tokens on this repo, which is most of a conversation's budget
    spent before the first question. The summary is read from
    .residuality/graph_summary.txt — the on-disk twin written by the last
    build — rather than re-rendered here, so context and disk can never
    disagree. See graph.render_graph_summary.
    """
    parts = []

    try:
        log_lines = [
            f"- [{c['hash']}] {c['message']}"
            for c in repo.log(limit=PLAN_LOG_COMMITS)
        ]
        parts.append(f"## Recent Git Log\n" + "\n".join(log_lines or ["(no commits yet)"]))
    except Exception as e:
        logger.warning(f"Chat context: git log failed: {e}")

    try:
        readme = repo.read("README.md")
        parts.append(f"## README.md\n{readme}")
    except Exception:
        parts.append("## README.md\n(not present in this project)")

    try:
        summary = repo.graph_summary_text()
        if summary:
            parts.append(f"## Project Structure\n{summary}")
        else:
            parts.append("## Project Structure\n(graph not built yet)")
    except Exception as e:
        logger.warning(f"Chat context: graph summary failed: {e}")
        parts.append(f"## Project Structure\n(unavailable: {e})")

    return parts


@app.route("/projects/<project_id>/plan")
@login_required
def plan(project_id: str):
    """The Plan page: this project's conversations, beside its assessment."""
    repo = get_repo(project_id)
    chat_id = request.args.get("chat", "").strip()
    if chat_id and not _chat_id_ok(chat_id):
        chat_id = ""

    def _chat_entry(p: Path) -> dict:
        try:
            data = json.loads(p.read_text())
            history = data.get("history", [])
            return {
                "id":    data.get("id", p.stem),
                "title": data.get("title") or (history[0]["user"][:60]
                                               if history else "New conversation"),
                "turns": len(history),
                "mtime": p.stat().st_mtime,
            }
        except Exception:
            return {"id": p.stem, "title": p.stem, "turns": 0,
                    "mtime": p.stat().st_mtime}

    chat_paths = sorted(_chats_dir(repo).glob("*.json"),
                        key=lambda p: p.stat().st_mtime, reverse=True)
    conversations = [_chat_entry(p) for p in chat_paths]

    if chat_id:
        active = _load_chat(repo, chat_id)
    elif chat_paths:
        # No explicit conversation: reopen the most recent one, so a plain
        # page refresh does not pile up empty conversations in the sidebar.
        active = _load_chat(repo, conversations[0]["id"])
    else:
        active = {"id": _new_chat_id(repo), "title": "New conversation",
                  "history": []}
        _save_chat(repo, active)
        conversations = [_chat_entry(_chats_dir(repo) / f"{active['id']}.json")]

    return render_template("plan.html",
                           project_id=project_id,
                           conversations=conversations,
                           active=active,
                           planner_model=resolve_model(model_cfg, "planner"))


@app.route("/projects/<project_id>/plan/new", methods=["POST"])
@login_required
def plan_new(project_id: str):
    repo = get_repo(project_id)
    chat = {"id": _new_chat_id(repo), "title": "New conversation", "history": []}
    _save_chat(repo, chat)
    return redirect(url_for("plan", project_id=project_id, chat=chat["id"]))


@app.route("/projects/<project_id>/plan/delete", methods=["POST"])
@login_required
def plan_delete(project_id: str):
    repo = get_repo(project_id)
    chat_id = request.form.get("chat", "").strip()
    if chat_id and _chat_id_ok(chat_id):
        path = _chats_dir(repo) / f"{chat_id}.json"
        if path.exists():
            path.unlink()
    return redirect(url_for("plan", project_id=project_id))


@app.route("/projects/<project_id>/plan/chat", methods=["POST"])
@login_required
def plan_send(project_id: str):
    """One turn of a Plan conversation; the reply comes back as JSON.

    Nothing is written to the project: the answer goes into the page, and any
    change to a file still has to be made through the edit path. This endpoint
    owns only the conversation JSON and the model call.
    """
    repo         = get_repo(project_id)
    user_message = request.form.get("message", "").strip()
    chat_id      = request.form.get("chat", "").strip()

    if not user_message:
        return jsonify({"error": "Empty message"}), 400
    if not chat_id or not _chat_id_ok(chat_id):
        return jsonify({"error": "Missing or invalid conversation id"}), 400

    chat    = _load_chat(repo, chat_id)
    history = chat["history"]

    # First exchange names the conversation.
    if not chat.get("title") or chat["title"] == "New conversation":
        chat["title"] = user_message[:60]

    # Fresh context on every message; the conversation's own history on top.
    messages = [{"role": "system",
                 "content": "\n\n".join(_chat_context_parts(repo))}]
    for exchange in history[-PLAN_HISTORY_LIMIT:]:
        messages.append({"role": "user",      "content": exchange["user"]})
        messages.append({"role": "assistant", "content": exchange["assistant"]})
    messages.append({"role": "user", "content": user_message})

    try:
        response = owui.chat(messages, model=resolve_model(model_cfg, "planner"))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    history.append({"user": user_message, "assistant": response})
    chat["history"] = history
    _save_chat(repo, chat)

    return jsonify({"response": response, "title": chat["title"]})


@app.route("/projects/<project_id>/plan/assess", methods=["POST"])
@login_required
def plan_assess(project_id: str):
    """Ask the planner model where this project stands and what to do next.

    Three inputs, assembled here rather than inside the client because this is
    where the project's files are known: the latest snapshot (what the last
    commits settled), the graph summary (what the code looks like now) and the
    recent log. Each one degrades to None on its own — a project with nothing
    indexed yet still gets an assessment from its README and history, and the
    client says which parts were missing rather than failing the request.
    """
    repo     = get_repo(project_id)

    snapshot = None
    try:
        latest = latest_snapshot(project_id)
        if latest:
            snapshot = format_snapshot_for_context(latest)
    except Exception as e:
        logger.warning(f"Plan: snapshot lookup failed for {project_id}: {e}")

    graph_summary = None
    try:
        graph_summary = repo.graph_summary_text()
    except Exception as e:
        logger.warning(f"Plan: graph summary failed for {project_id}: {e}")

    try:
        # The commit key is normalised to the shape generate_plan formats: the
        # git log calls it `hash`, a commit record calls it `commit_hash`, and
        # the prompt only needs one of them.
        recent = [{"commit_hash": c["hash"], "message": c["message"]}
                  for c in repo.log(limit=PLAN_LOG_COMMITS)]
    except Exception as e:
        logger.warning(f"Plan: git log failed for {project_id}: {e}")
        recent = []

    try:
        plan = owui.generate_plan(
            project_id=project_id,
            snapshot=snapshot,
            graph_summary=graph_summary,
            recent_commits=recent,
            model=resolve_model(model_cfg, "planner"),
        )
    except Exception as e:
        logger.error(f"Plan assessment failed for {project_id}: {e}", exc_info=True)
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 502

    plan["context"] = {
        "snapshot":      bool(snapshot),
        "graph_summary": bool(graph_summary),
        "commits":       len(recent),
    }
    return jsonify(plan)

# ── Graph ─────────────────────────────────────────────────────────────────

@app.route("/projects/<project_id>/graph", methods=["GET", "POST"])
@login_required
def graph_view(project_id: str):
    """Project Control: the graph, the commit flow, and this repo's remote.

    The git remote panel used to live on a per-project settings page. It lives
    here now: the program-wide Settings page took that name, and a remote is a
    property of *this* repository rather than of the install.
    """
    repo = get_repo(project_id)

    if request.method == "POST":
        action = request.form.get("action")
        try:
            if action == "add_remote":
                url  = request.form.get("remote_url", "").strip()
                name = request.form.get("remote_name", "origin").strip() or "origin"
                if url:
                    repo.add_remote(url, name)
                    flash(f"Remote '{name}' added", "success")
            elif action == "remove_remote":
                name = request.form.get("remote_name", "origin").strip()
                repo.remove_remote(name)
                flash(f"Remote '{name}' removed", "success")
            elif action == "push":
                remote = request.form.get("remote_name", "origin").strip()
                branch = request.form.get("branch", "main").strip() or "main"
                repo.push(remote, branch)
                flash(f"Pushed to {remote}/{branch}", "success")
            elif action == "pull":
                remote = request.form.get("remote_name", "origin").strip()
                branch = request.form.get("branch", "main").strip() or "main"
                repo.pull(remote, branch)
                flash(f"Pulled from {remote}/{branch}", "success")
        except Exception as e:
            flash(str(e), "error")
        return redirect(url_for("graph_view", project_id=project_id))

    dot_path    = str(repo.repo_path / ".residuality" / "graph.dot")
    dot_content = repo.get_graph_dot()
    # Root of the drill-down view; `graph/svg` (flat file graph) is still
    # available for anyone who wants the old level-1 canvas.
    svg         = render_directory_graph(dot_path, _tracked_files(repo), "")
    return render_template("graph.html",
                           project_id=project_id,
                           svg=svg,
                           dot=dot_content,
                           remotes=repo.get_remotes())


@app.route("/projects/<project_id>/graph/svg")
@login_required
def graph_svg(project_id: str):
    """Return file graph SVG, optionally with external pip imports."""
    repo     = get_repo(project_id)
    dot_path = str(repo.repo_path / ".residuality" / "graph.dot")
    show_ext = request.args.get("external", "false").lower() == "true"
    svg      = render_file_graph(dot_path, show_external=show_ext)
    return jsonify({"svg": svg})


@app.route("/projects/<project_id>/graph/dir")
@login_required
def graph_dir(project_id: str):
    """Directory drill-down view for one path (blank = repo root).

    The directory's name is the label on its box of files and the child
    directories are the column at the far right of the canvas. Import edges are
    retargeted to the deepest visible node on the target's path.

    `children=false` drops that column, for the panels the strip has already
    slid past: their sub-folder labels are the panels sitting to their right.
    """
    repo     = get_repo(project_id)
    dot_path = str(repo.repo_path / ".residuality" / "graph.dot")
    path     = request.args.get("path", "").strip("/")
    show_ext = request.args.get("external", "false").lower() == "true"
    show_kid = request.args.get("children", "true").lower() == "true"
    svg      = render_directory_graph(dot_path, _tracked_files(repo), path,
                                      show_external=show_ext,
                                      show_children=show_kid)
    return jsonify({"svg": svg, "path": path})


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
    walked    = set()

    gitignore_path = repo.repo_path / ".gitignore"
    if not gitignore_path.exists():
        gitignore_path.write_text(
            "# Residuality default .gitignore\n"
            "__pycache__/\n*.pyc\n*.pyo\n*.pyd\n"
            "build/\ndist/\n*.egg-info/\n.eggs/\n"
            "*.so\n*.o\n*.a\n*.out\n*.log\nexport/\n"
            ".residuality/\n"
            ".DS_Store\n"
        )

    size_limit = 500 * 1024

    def _is_ignored(rel_path: str) -> bool:
        """Ask git whether a path is ignored — respects the real .gitignore."""
        try:
            out = repo.repo.git.check_ignore("--quiet", rel_path)
            return out.strip() != ""
        except Exception:
            return False

    for f in sorted(repo.repo_path.rglob("*")):
        if f.is_dir():
            continue
        rel = str(f.relative_to(repo.repo_path))
        if _is_ignored(rel):
            continue
        try:
            size = f.stat().st_size
        except Exception as e:
            errors.append(f"stat {f}: {e}")
            continue
        if size > size_limit:
            continue

        walked.add(rel)
        logger.info(f"Processing: {rel}")
        try:
            repo._update_graph(rel)
            processed.append(rel)
        except Exception as e:
            error_msg = f"{rel}: {type(e).__name__}: {e}"
            logger.error(error_msg, exc_info=True)
            errors.append(error_msg)

    try:
        repo.prune_graph_sections(walked)
    except Exception as e:
        errors.append(f"prune: {type(e).__name__}: {e}")

    try:
        if gitignore_path.exists() and not _is_ignored(".gitignore"):
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

    def _is_ignored(rel_path: str) -> bool:
        try:
            out = repo.repo.git.check_ignore("--quiet", rel_path)
            return out.strip() != ""
        except Exception:
            return False

    for ext in ("*.py", "*.md", "*.html", "*.htm"):
        for f in sorted(repo.repo_path.rglob(ext)):
            rel = str(f.relative_to(repo.repo_path))
            if _is_ignored(rel):
                continue
            all_files.append(rel)

    for filepath in all_files:
        try:
            repo._update_graph(filepath)
        except Exception as e:
            errors.append(f"{filepath}: {e}")

    try:
        repo.prune_graph_sections(set(all_files))
    except Exception as e:
        errors.append(f"prune: {e}")

    try:
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

    messages = ctx_builder.build_edit_context(
        node_content=node_content,
        node_id=node_id,
        instruction=instruction,
        rolling_summary=None,
        file_path=node["file"],
        line_start=node["line_start"],
        line_end=node["line_end"],
        system_prompt=model_cfg.get("edit_prompt"),
    )

    try:
        new_content = owui.chat(messages, model=owui.EDIT_MODEL)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    # Cleaned up the same way the modal's AI edit is, and for a stronger reason:
    # this path commits as soon as the model answers, so a fenced reply would put
    # literal backticks into the file *and* into history, past the point where a
    # diff can undo it.
    new_content, _ = strip_think(new_content)
    new_content, _ = strip_code_fence(new_content)

    edit = {"line_start": node["line_start"], "line_end": node["line_end"], "new_content": new_content}
    commit_msg = f"Edit {node_id}: {instruction[:80]}"
    try:
        commit_hash = repo.apply_edits(node["file"], [edit], commit_msg)
        index_commit_async(repo, commit_hash, project_id, node["file"], commit_msg)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({"commit_hash": commit_hash[:8], "new_content": new_content})


@app.route("/projects/<project_id>/graph/save_lines", methods=["POST"])
@login_required
def save_lines(project_id: str):
    """Replace one line range of the working file with the editor's lines.

    This is the "sed" step, done in-process. Shelling out to `sed -i` would
    mean escaping the replacement text for `&`, `/`, backslash and newline (and
    a multi-line `c\\` replacement) — all to produce bytes identical to what a
    slice assignment already produces.

    Deliberately does **not** commit. The file and the graph.dot section
    rebuilt from it are left dirty in the working tree, so the edit arrives in
    Regenerate & Commit for review like any other working-tree change.

    `node_id` is optional and only used to hand the caller back the node's line
    range *after* the edit: adding or removing lines shifts the span, and a
    stale range would make the next save target the wrong lines.
    """
    repo     = get_repo(project_id)
    filepath = request.form.get("file", "").strip().strip("/")
    node_id  = request.form.get("node_id", "").strip() or None

    if not filepath:
        return jsonify({"error": "No file given"}), 400

    try:
        line_start = int(request.form.get("line_start", "0"))
        line_end   = int(request.form.get("line_end", "0"))
    except ValueError:
        return jsonify({"error": "line_start/line_end must be integers"}), 400
    if line_start < 1 or line_end < line_start:
        return jsonify({"error": f"Bad line range {line_start}-{line_end}"}), 400

    full_path = repo.repo_path / filepath
    if not full_path.is_file():
        return jsonify({"error": f"{filepath} is not a file in the working tree"}), 404

    try:
        original = full_path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    lines = original.splitlines()

    # Bounds check. `apply_edits` trusts line_start/line_end, and a graph built
    # before the last edit to this file hands out a range that no longer
    # exists -- slicing past the end would silently drop the file's tail.
    if line_start > len(lines):
        return jsonify({"error": (
            f"Range starts at line {line_start} but {filepath} has "
            f"{len(lines)} lines - the graph is stale, rebuild it"
        )}), 409
    clamped = line_end > len(lines)
    eff_end = min(line_end, len(lines))

    new_lines = request.form.get("content", "").splitlines()
    updated   = lines[:line_start - 1] + new_lines + lines[eff_end:]

    # A trailing newline *terminates* the last line rather than starting an
    # empty one, so don't invent one for a file that had none, and don't drop
    # the one that was there.
    full_path.write_text("\n".join(updated) + ("\n" if original.endswith("\n") else ""),
                         encoding="utf-8")

    # Re-run Build Graph for this one file: rewrite only its section of
    # graph.dot, leaving every other file's block untouched. Files no parser
    # handles (Dockerfile, LICENSE, requirements.txt) have no section to
    # rebuild -- the content is saved either way.
    errors = []
    parsed = bool(repo._lang_key(filepath)) or filepath.lower().endswith(".md")
    if parsed:
        try:
            repo._update_graph(filepath)
        except Exception as e:
            logger.error(f"graph rebuild failed for {filepath}: {e}", exc_info=True)
            errors.append(f"graph rebuild: {type(e).__name__}: {e}")

    # Hand back the node's new span, read straight off the rebuilt graph.
    new_start = line_start
    new_end   = line_start + len(new_lines) - 1
    if node_id and parsed and not errors:
        graph = load_graph(str(repo.repo_path / ".residuality" / "graph.dot"))
        node  = get_node_by_id(graph, node_id) if graph else None
        if node and node.get("file") == filepath and node.get("line_start"):
            new_start, new_end = node["line_start"], node["line_end"]

    return jsonify({
        "status":     "ok",
        "file":       filepath,
        "line_start": new_start,
        "line_end":   new_end,
        "lines":      len(new_lines),
        "graph":      parsed,
        "clamped":    clamped,
        "errors":     errors,
    })


@app.route("/projects/<project_id>/graph/ai_edit", methods=["POST"])
@login_required
def graph_ai_edit(project_id: str):
    """Ask a model for a replacement for one line range, returned as text.

    Deliberately stops at text. Nothing is written, nothing is committed and
    graph.dot is not rebuilt: the reply lands in the editor's box, where the
    user sees it as a diff against the original and either saves it through
    `save_lines` or cancels it away. So this endpoint needs none of the
    bounds-clamping or graph-rebuild half of `save_lines` — it touches no file.

    The model is checked against the endpoint's own list rather than taken on
    trust. That list is whatever the configured API key can see, so the
    allowlist is a property of the key rather than a second thing to maintain.

    A range larger than the model's budget is split on the graph's node
    boundaries and sent as one call per chunk, then stitched back into a single
    replacement. That is the index doing what it exists for: a small-context
    model can still rewrite a whole file, one node at a time, without the range
    ever being cut down to a prefix it never saw the end of.
    """
    repo        = get_repo(project_id)
    filepath    = request.form.get("file", "").strip().strip("/")
    node_id     = request.form.get("node_id", "").strip() or None
    instruction = request.form.get("instruction", "").strip()
    model       = (request.form.get("model", "").strip()
                   or resolve_model(model_cfg, "edit"))

    if not filepath:
        return jsonify({"error": "No file given"}), 400
    if not instruction:
        return jsonify({"error": "Describe what should change first"}), 400

    try:
        line_start = int(request.form.get("line_start", "0"))
        line_end   = int(request.form.get("line_end", "0"))
    except ValueError:
        return jsonify({"error": "line_start/line_end must be integers"}), 400
    if line_start < 1 or line_end < line_start:
        return jsonify({"error": f"Bad line range {line_start}-{line_end}"}), 400

    full_path = repo.repo_path / filepath
    if not full_path.is_file():
        return jsonify({"error": f"{filepath} is not a file in the working tree"}), 404

    lines = full_path.read_text(encoding="utf-8", errors="replace").splitlines()
    if line_start > len(lines):
        return jsonify({"error": (
            f"Range starts at line {line_start} but {filepath} has "
            f"{len(lines)} lines - the graph is stale, rebuild it"
        )}), 409
    line_end = min(line_end, len(lines))

    # Split the range on the graph's own node boundaries and send one call per
    # chunk. A node begins and ends at a statement, so a cut there keeps whole
    # functions; the old head-clamp cut on an arbitrary line and spliced a
    # prefix answer back over the whole range, which is how a long range lost
    # its tail. Chunking is the index doing what it exists for -- a small model
    # edits a big file node by node.
    dot_path    = str(repo.repo_path / ".residuality" / "graph.dot")
    graph       = load_graph(dot_path)
    node_ranges = graph_node_ranges(graph, filepath)
    limit       = resolve_max_edit_chars(model_cfg)
    chunks      = plan_edit_chunks(lines, line_start, line_end, limit, node_ranges)

    known, list_error = list_models(base_url=owui.base_url, api_key=owui.api_key)
    if known:
        if model not in {m["id"] for m in known}:
            return jsonify({
                "error":  f"Model '{model}' is not offered by {owui.base_url} "
                          f"with the configured API key",
                "models": [m["id"] for m in known],
            }), 400
    else:
        # Nothing to validate against, so accept only a model this install is
        # already configured to use rather than any id the browser names.
        allowed = {v for v in (model_cfg.get("default"), model_cfg.get("chat"),
                               resolve_model(model_cfg, "edit")) if v}
        if model not in allowed:
            return jsonify({"error": (
                f"Cannot verify model '{model}': the model list is "
                f"unavailable ({list_error or 'empty'})"
            )}), 503

    many     = len(chunks) > 1
    replies  = []
    empty    = []
    think_stripped  = False
    fences_stripped = False

    for index, (chunk_start, chunk_end) in enumerate(chunks, start=1):
        messages = ctx_builder.build_edit_context(
            node_content="\n".join(lines[chunk_start - 1:chunk_end]),
            node_id=node_id or filepath,
            instruction=instruction,
            rolling_summary=None,
            file_path=filepath,
            line_start=chunk_start,
            line_end=chunk_end,
            chunk=(index, len(chunks), line_start, line_end) if many else None,
            system_prompt=model_cfg.get("edit_prompt"),
        )
        try:
            reply = owui.chat(messages, model=model)
        except Exception as e:
            # Any chunk failing fails the whole edit: a stitched answer with a
            # hole in it is the silent damage this path exists to avoid. The
            # chunk is named so a retry is not blind.
            logger.error(
                f"ai_edit chunk {index}/{len(chunks)} "
                f"({chunk_start}-{chunk_end}) failed: {e}", exc_info=True,
            )
            return jsonify({"error": (
                f"chunk {index} of {len(chunks)} "
                f"(lines {chunk_start}-{chunk_end}) failed: "
                f"{type(e).__name__}: {e}"
            )}), 502

        text, think_stripped_chunk  = strip_think(reply)
        text, fences_stripped_chunk = strip_code_fence(text)
        think_stripped  = think_stripped or think_stripped_chunk
        fences_stripped = fences_stripped or fences_stripped_chunk

        if text.strip():
            replies.append(text)
        else:
            # An empty answer would splice a hole where those lines were, so the
            # chunk is kept verbatim and reported. Discarding model output is not
            # this endpoint's call to make.
            empty.append({"line_start": chunk_start, "line_end": chunk_end})
            replies.append("\n".join(lines[chunk_start - 1:chunk_end]))

    if len(empty) == len(chunks):
        return jsonify({"error": "the model returned nothing usable"}), 502

    return jsonify({
        "content":         "\n".join(replies),
        "model":           model,
        "think_stripped":  think_stripped,
        "fences_stripped": fences_stripped,
        "chunk_count":     len(chunks),
        "empty_chunks":    empty,
        "line_start":      line_start,
        "line_end":        line_end,
    })

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
        return redirect(url_for("history", project_id=project_id))

    commits = repo.get_commits()
    return render_template("merge.html", project_id=project_id, commits=commits)

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

    # File and chapter nodes carry no `file=` attribute — their id *is* the
    # path. Slicing with node["file"] read "" and resolved to the repo root,
    # so every file node answered 500 "Is a directory" and a file's whole
    # content was unreachable from the graph.
    is_file_node = bool(node) and (node["type"] in ("file", "chapter")
                                   or not node["file"])

    # Return just the node's lines
    if node and not is_file_node:
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

    # Whole file: a file/chapter node, or an id that is a plain path.
    try:
        content = repo.read(node_id)
        return jsonify({
            "content":    content,
            "type":       "file",
            "file":       node_id,
            "line_start": 1,
            "line_end":   len(content.splitlines()),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 404


@app.route("/projects/<project_id>/graph/nodes/<path:filepath>")
@login_required
def graph_file_nodes(project_id: str, filepath: str):
    """Return the interior nodes (functions/classes/sections) of a file."""
    repo     = get_repo(project_id)
    dot_path = str(repo.repo_path / ".residuality" / "graph.dot")
    graph    = load_graph(dot_path)
    if not graph:
        return jsonify({"error": "Graph not found"}), 404

    nodes = []
    for node in graph.get_nodes():
        attrs = node.get_attributes()
        nid   = node.get_name().strip('"')
        nfile = attrs.get("file", "").strip('"')
        if nfile != filepath or nid == filepath:
            continue
        def _i(key):
            try:    return int(attrs.get(key, ""))
            except: return 0
        nodes.append({
            "id":         nid,
            "type":       attrs.get("type", "").strip('"'),
            "signature":  attrs.get("signature", "").strip('"'),
            "label":      attrs.get("label", "").strip('"'),
            "line_start": _i("line_start"),
            "line_end":   _i("line_end"),
        })
    # Preserve parse order
    return jsonify({"file": filepath, "nodes": nodes})


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


# ── Image display & replacement ───────────────────────────────────────────
#
# The node modal reads files through repo.read(), which forces everything
# through UTF-8 — fine for code and prose, but it turns a PNG into garbage
# text. Images need their raw bytes served back so the browser can render
# them, plus a way to swap in a replacement without touching the line-based
# edit path (which is meaningless for binary data).

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}
IMAGE_MIME = {
    ".png":  "image/png",
    ".jpg":  "image/jpeg", ".jpeg": "image/jpeg",
    ".gif":  "image/gif",
    ".webp": "image/webp",
    ".bmp":  "image/bmp",
    ".svg":  "image/svg+xml",
}
IMAGE_SIZE_CAP = 15 * 1024 * 1024   # 15 MB — bigger than any reasonable asset


def _is_image_path(filepath: str) -> bool:
    return Path(filepath).suffix.lower() in IMAGE_EXTENSIONS


@app.route("/projects/<project_id>/graph/image/<path:filepath>")
@login_required
def graph_image(project_id: str, filepath: str):
    """Serve a tracked image file's raw bytes for inline display.

    The node modal points an <img> at this URL when the clicked file has an
    image extension. Returns the file with its detected MIME type so the
    browser renders it; refuses non-image paths and anything over the cap.
    """
    if not _is_image_path(filepath):
        return jsonify({"error": f"{filepath} is not an image"}), 400

    repo = get_repo(project_id)
    abs_path = repo.repo_path / filepath

    # Resolve and confirm the path stays inside the repo root.
    try:
        abs_path.resolve().relative_to(repo.repo_path.resolve())
    except ValueError:
        return jsonify({"error": "path escapes the repository"}), 400

    if not abs_path.is_file():
        return jsonify({"error": f"{filepath} not found"}), 404

    size = abs_path.stat().st_size
    if size > IMAGE_SIZE_CAP:
        return jsonify({"error": f"file is {size // 1024 // 1024} MB, over the {IMAGE_SIZE_CAP // 1024 // 1024} MB cap"}), 413

    mime = IMAGE_MIME.get(Path(filepath).suffix.lower(), "application/octet-stream")
    return send_file(str(abs_path), mimetype=mime, as_attachment=False)


@app.route("/projects/<project_id>/graph/image/replace", methods=["POST"])
@login_required
def graph_image_replace(project_id: str):
    """Replace a tracked image file with an uploaded one.

    Multipart form: `file` (the new image) + `target` (repo-relative path of
    the existing image to overwrite). Writes the bytes to the working tree
    and leaves the file dirty for Regenerate & Commit — consistent with the
    text-edit flow, which also does not commit on save.
    """
    target = request.form.get("target", "").strip()
    upload = request.files.get("file")

    if not target or not upload or not upload.filename:
        return jsonify({"error": "missing 'target' or 'file' field"}), 400

    if not _is_image_path(target):
        return jsonify({"error": f"{target} is not an image path"}), 400

    # Validate the uploaded file is actually an image by extension.
    up_ext = Path(upload.filename).suffix.lower()
    if up_ext not in IMAGE_EXTENSIONS:
        return jsonify({"error": f"uploaded file must be an image ({', '.join(sorted(IMAGE_EXTENSIONS))})"}), 400

    data = upload.read()
    if len(data) > IMAGE_SIZE_CAP:
        return jsonify({"error": f"upload is {len(data) // 1024 // 1024} MB, over the {IMAGE_SIZE_CAP // 1024 // 1024} MB cap"}), 413

    repo = get_repo(project_id)
    abs_path = repo.repo_path / target

    # Guard against path traversal.
    try:
        abs_path.resolve().relative_to(repo.repo_path.resolve())
    except ValueError:
        return jsonify({"error": "target path escapes the repository"}), 400

    # The target must already exist in the repo (we are replacing, not adding).
    if not abs_path.is_file():
        return jsonify({"error": f"{target} does not exist in the repository"}), 404

    # Write the new bytes. Binary-safe: no encoding, no newline translation.
    abs_path.write_bytes(data)

    # Re-run the graph update for this file. For images this is a no-op (no
    # parser handles .png etc.), but keeping the call uniform means the
    # summary and graph.dot stay in sync if a parser is ever added.
    try:
        repo._update_graph(target)
    except Exception:
        pass   # graph rebuild is best-effort here; the image itself is saved

    return jsonify({
        "status":  "ok",
        "file":    target,
        "size":    len(data),
        "message": f"replaced {target} ({len(data) // 1024} KB) — uncommitted, commit from Regenerate & Commit",
    })


# ── Snapshots ─────────────────────────────────────────────────────────────

# The last reason a manual snapshot failed, per project. The generate button
# runs in a background thread and answers immediately, so the only channel back
# to the UI is this status route — without it, a failure collapses into the
# generic "may be down" line with no way to tell an embedding outage from a
# model that answered with a question instead of a snapshot.
_snapshot_failures = {}


@app.route("/projects/<project_id>/snapshot/generate", methods=["POST"])
@login_required
def generate_snapshot_now(project_id: str):
    """Generate and store an episodic snapshot on demand.

    The commit pipeline (`index_commit_async`) already snapshots after every
    commit, but a project that has not committed since being indexed has no
    baseline at all — the Plan assessment then reads as *not yet* rather than
    describing where things stand. This button is the manual trigger: it takes
    the current HEAD (or the newest commit if the tree is dirty), asks the
    default model for a state-of-affairs snapshot, and stores it in Qdrant.

    It runs in a background thread and answers immediately, because the model
    call and the embedding request can each take many seconds — and either of
    them may be down. A dead OWUI or embed endpoint must not hang the click:
    the thread logs the failure and the status line reports it.
    """
    repo = get_repo(project_id)
    _snapshot_failures.pop(project_id, None)   # a fresh attempt clears the old reason

    def _generate():
        try:
            head   = repo.repo.heads[repo._current_branch()].commit
            commit_hash    = head.hexsha
            commit_message = head.message.strip().splitlines()[0] if head.message else "(no message)"
            project_type   = detect_project_type(repo.repo_path)
            snapshot       = generate_snapshot(
                project_id       = project_id,
                commit_hash      = commit_hash,
                commit_message   = commit_message,
                summary          = None,
                project_type     = project_type,
                owui_client      = owui,
                summarizer_model = resolve_model(model_cfg, "default"),
            )
            if not snapshot:
                _snapshot_failures[project_id] = (
                    f"the default model ({resolve_model(model_cfg, 'default')}) returned "
                    f"no usable snapshot — it may have asked a clarifying question or "
                    f"refused the JSON format. Check the server log for its reply."
                )
                logger.warning(f"Snapshot generation returned nothing for {project_id}")
                return
            stored = store_snapshot(
                project_id     = project_id,
                commit_hash    = commit_hash,
                commit_message = commit_message,
                snapshot       = snapshot,
                turn           = 0,
                timestamp      = head.committed_date,
            )
            if stored is True:
                _snapshot_failures.pop(project_id, None)
                logger.info(f"Manual snapshot stored for {project_id} @ {commit_hash[:8]}: "
                            f"{snapshot.get('state', '')[:80]}")
            else:
                _snapshot_failures[project_id] = stored
                logger.warning(f"Manual snapshot not stored for {project_id}: {stored}")
        except Exception as e:
            _snapshot_failures[project_id] = str(e)
            logger.warning(f"Manual snapshot failed for {project_id}: {e}")

    threading.Thread(target=_generate, daemon=True).start()
    return jsonify({"status": "started",
                    "message": "snapshot generation started — check the status line"})


@app.route("/projects/<project_id>/snapshot/status")
@login_required
def snapshot_status(project_id: str):
    """The latest stored snapshot for this project, or none.

    The generate button polls this while it works: `latest_snapshot` scrolls
    the stored payloads by timestamp, so a freshly generated snapshot shows up
    here the moment it lands in Qdrant.
    """
    snap = latest_snapshot(project_id)
    if not snap:
        return jsonify({"has_snapshot": False, "snapshot": None,
                        "error": _snapshot_failures.get(project_id)})
    return jsonify({"has_snapshot": True, "snapshot": snap})


# ── API endpoints ─────────────────────────────────────────────────────────

@app.route("/api/projects")
@login_required
def api_projects():
    return jsonify(list_projects())


@app.route("/api/health")
def api_health():
    return jsonify({"status": "ok", "owui": owui.health_check()})

@app.route("/api/models")
@login_required
def api_models():
    """Proxy the endpoint's model list, with the current selections.

    This route used to sit *below* `if __name__ == "__main__": app.run(...)`,
    which never returns — so under the Docker entrypoint (`python app.py`) it
    was never registered at all, and it called `requests.get` in a module that
    never imported `requests`. The move and the single fetcher fix both.
    """
    models, error = list_models(
        base_url=owui.base_url,
        api_key=owui.api_key,
        force=request.args.get("refresh") == "1",
    )
    return jsonify({
        "models":             models,
        "error":              error,
        "endpoint":           owui.base_url,
        "current_default":    model_cfg.get("default", ""),
        "current_chat":       model_cfg.get("chat", ""),
        "current_edit":       model_cfg.get("edit", ""),
        "resolved_chat":      resolve_model(model_cfg, "chat"),
        "resolved_edit":      resolve_model(model_cfg, "edit"),
        "current_planner":    resolve_model(model_cfg, "planner"),
        "current_summarizer": resolve_model(model_cfg, "summarizer"),
    })


@app.route("/api/models/select", methods=["POST"])
@login_required  
def api_models_select():
    """Save model selections. Superseded by /settings/models.

    Kept because it predates the settings panel; both now write through the
    same `save_config`, so there is one config file and one rule rather than
    two. Note it used to write `chat_model` into the *default* slot and then
    copy every value onto the client, where `chat()` ignored it anyway.
    """
    cfg = load_config()
    for field in ("default", "chat", "edit", "planner", "summarizer"):
        if field in request.form:
            cfg[field] = request.form.get(field, "").strip()
    try:
        save_config(cfg)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    reload_model_config()
    return jsonify({"status": "ok"})


# ── Startup ───────────────────────────────────────────────────────────────
# Last in the file on purpose: `app.run()` blocks, so everything above has to
# be registered before this point. Routes used to be defined *after* it, which
# meant they silently did not exist in the deployed app.

if __name__ == "__main__":
    ensure_collections()
    ensure_snapshot_collection()
    port = int(os.getenv("RESIDUALITY_PORT", 5010))
    logger.info(f"Residuality starting on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
