# residuality

AI-assisted artifact versioning. Residuality sits on top of a git repo and gives you
a code/prose graph, vector-backed long-term memory, and surgical model-assisted edits
— so you can change one function without losing the thread of the whole project.

It is designed to work with **Open WebUI** (local models) and **Qdrant** (local vector
store). Nothing leaves your machine.

---

## What it does

| Concept | How it works |
|---|---|
| **Artifact store** | Every project is a real git repo under `/repos/<project_id>`. `read`, `write`, `branch`, `merge`, `checkout`, `diff` — all backed by GitPython. |
| **Graph** | `tree-sitter` parses `.py` files (and `<!-- rs:section -->` markers in `.md`) into a DOT graph of files, classes, functions and import edges. Stored in `.residuality/graph.dot` and committed with the code. |
| **Memory** | Each commit indexes its message and its graph nodes into Qdrant, and generates an *episodic snapshot* (state, decisions, open questions) that is also vectorised. |
| **Chat** | Chat is scoped to a project. It pulls in the rolling summary, the last few exchanges, and — keyword-triggered — relevant commits or relevant graph nodes. It edits one node at a time, not whole files. |
| **Merge** | Pick two divergent commits; the model reconciles conflicting files into a merge commit with two parents. |

---

## Architecture

```
  ┌─────────────────────┐        ┌──────────────────────┐
  │  Open WebUI  :8282  │◄───────│                      │
  │  (models)           │        │   residuality        │
  └─────────────────────┘        │   Flask app :5010    │
                                 │                      │
  ┌─────────────────────┐        │  artifact.py   git   │
  │  Qdrant      :6333  │◄───────┼── indexer.py    Qdrant│
  │  (vectors)          │        │  snapshot.py         │
  └─────────────────────┘        │  context.py          │
                                 │  owui_client.py      │
  ┌─────────────────────┐        │  graph.py   pydot    │
  │ Embed svc   :8090   │◄───────┘                      │
  │ (nomic-embed-text)  │         └──────────┬──────────┘
  └─────────────────────┘                    │
                                      tree-sitter JSON
                                            │
                                            ▼
                                 ┌─────────────────────┐
                                 │  dot_updater  (C)   │  ← rewrites graph.dot
                                 └─────────────────────┘
```

### Layout

```
/app/app.py              # routes, auth, chat, graph, merge, export
/app/artifact.py         # Git-backed artifact store + graph updates
/app/graph.py            # DOT parsing, SVG rendering, prose export
/app/indexer.py          # Qdrant upsert/search (commits + graph nodes)
/app/snapshot.py         # episodic snapshot generation + storage
/app/context.py          # context compression for model calls
/app/owui_client.py      # Open WebUI API client
/app/dot_updater.c       # surgical graph.dot section replacer (C)
/app/.residuality/extract.scm   # tree-sitter query for Python
/templates/*.html        # Jinja UI
/static/residuality.js   # graph drill-down, gitignore, chat, file lists
```

---

## Getting started

### Docker (recommended)

```bash
cp .env.example .env      # edit OWUI_URL / QDRANT_URL etc.
docker compose up -d
```

`docker-compose.yml` mounts every `.py`, `templates/` and `static/` over the image,
so you can edit the app on your host and see changes without rebuilding. State lives
in `~/sandbox/repos` (the `/repos` volume).

```bash
# Build graph for an already-cloned project
docker compose exec residuality python -c "
from artifact import ArtifactRepo
r = ArtifactRepo('my-project')
r._update_graph('src/main.py')
"
```

### Local

```bash
pip install -r requirements.txt
gcc -O2 -o dot_updater dot_updater.c cJSON.c -lm
sudo mv dot_updater /usr/local/bin/dot_updater
RESIDUALITY_PORT=5010 python app.py
```

Required env (defaults in parentheses):

| Variable | Default |
|---|---|
| `OWUI_URL` | `http://192.168.0.100:8282` |
| `OWUI_API_KEY` | *(none)* |
| `OWUI_MODEL` | `/models/diffusiongemma` (chat) |
| `SUMMARIZER_MODEL` | `/models/diffusiongemma` |
| `PLANNER_MODEL` | `Qwen3.5-9B-Q4_0.gguf` |
| `QDRANT_URL` | `http://192.168.0.100:6333` |
| `EMBED_URL` | `http://192.168.0.100:8090` |
| `REPOS_PATH` | `/repos` |
| `RESIDUALITY_PORT` | `5010` |
| `RESIDUALITY_USERNAME` / `RESIDUALITY_PASSWORD` | `admin` / `changeme` |

**Change the defaults before exposing this to a network.** There is no rate limiting
and the default credentials are committed in `docker-compose.yml`.

---

## Usage

1. **Create a project** — either a fresh repo or link an existing one (`/projects/new`).
   Residuality writes `.residuality/extract.scm`, an empty `graph.dot`, and a default
   `.gitignore`, then commits them as `Init project: <id>`.
2. **Build the graph** — ⚙ *Build Graph* runs tree-sitter over every `.py`/`.md`
   (skipping `.git`, `.residuality`, `export`, and files over 500 KB) and commits the
   result.
3. **Drill down** — click a file in the SVG to see its classes and functions; click a
   node to view its exact source span and its neighbours (`contains`, `calls`, `imports`,
   `precedes`).
4. **Edit a node** — pick a function or section, give an instruction, and the model
   returns *only* the replacement text for that line range. Applied bottom-up so ranges
   don't shift, then committed.
5. **Chat** — mention something historical (`when did`, `broke`, `changed`) to pull in
   relevant commits and snapshots; mention structure (`function`, `class`, `where is`)
   to pull in graph nodes. The current node's content is injected as ground truth.
6. **Merge** — select two commits and let the model reconcile divergent files into a
   two-parent merge commit.

### Prose marker syntax

Markdown files are treated as chapters. Sections are declared with HTML comments and
become `type="section"` nodes with `precedes` edges:

```markdown
<!-- rs:section id="s3" label="The cellar door" -->
The door had never been locked. That was the part she couldn't forgive.
<!-- /rs:section -->
```

`graph.export_prose` strips those markers when exporting clean `.md` out of `export/`.

---

## Design notes

- **Graph updates are section-replacement, not full rebuild.** Each file owns a
  `// --- file: <path> ---` … `// --- end: <path> ---` block. Rewriting one file's
  block leaves every other project file untouched. The write is atomic (`write` to
  `<file>.tmp`, then `rename()`).
- **Node IDs are hierarchical:** `<file>::<Class>::<method>`. A top-level function is
  `<file>::<fn>`. This doubles as the Qdrant payload key (`uuid5` over `node-<id>`).
- **Two project types, two snapshot shapes.** `snapshot.detect_project_type` picks
  `fiction` (reader knows / open threads / tone / last hook) versus `code`
  (current task / key decisions / what's working / what's broken). The same search
  endpoint returns both; `format_snapshot_for_context` renders the right fields.
- **Memory is stateless between requests.** `_conversation_state` holds the rolling
  summary and the last 20 exchanges in-memory; everything else (commits, nodes,
  snapshots) comes back from Qdrant on every message. Restart the server and you lose
  the working summary, not the history.

### Known limitations

- There are **two graph-update implementations** that diverge: `artifact.py` walks the
  tree-sitter AST directly, while `dot_updater.c` consumes `tree-sitter query` JSON.
  Only one should own parsing. Both stop at function bodies, so nested functions are
  invisible, and the C path's class-parent heuristic (`start_row <= current_class_end`)
  mis-attributes methods across nested classes and bare functions.
- `chat_send` reads the **working tree** (`repo.read(path)`) rather than the commit a
  node belongs to, so content can be stale when browsing history.
- `merge` recomputes `repo.diff(a, b)` once **per file** instead of once per merge.
- `indexer.py` and `snapshot.py` each carry their own `_embed`, `_ensure_collection`
  and upsert/search helpers. Identical code, different names.
- `apply_edits` trusts `line_start`/`line_end` without bounds checks.

---

## License

Apache 2.0 — see `LICENSE`.
