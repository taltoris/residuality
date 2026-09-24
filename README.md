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
| **Graph** | `tree-sitter` parses `.py`, `.c`/`.h`, `.cpp`/`.hpp`, `.js`/`.ts`, `.rs` and `.html`/`.htm` (plus `<!-- rs:section -->` markers in `.md`) into a DOT graph of files, classes, functions, template sections and import edges. Stored in `.residuality/graph.dot` and committed with the code. |
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
  ┌─────────────────────┐        │  graph.py            │
  │ Embed svc   :8090   │◄───────┘                      │
  │ (nomic-embed-text)  │         └────────────────────┘
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
/app/.residuality/extract-python.scm   # tree-sitter query for Python
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
   Residuality writes `.residuality/extract-python.scm`, an empty `graph.dot`, and a default
   `.gitignore`, then commits them as `Init project: <id>`.
2. **Build the graph** — ⚙ *Build Graph* runs tree-sitter over every `.py`/`.md`/`.html`
   (skipping `.git`, `.residuality`, `export`, and files over 500 KB) and commits the
   result.
3. **Drill down** — the canvas opens on the repo root with the folder's name above its
   box of files and its sub-folders as the column on the right. Clicking one slides the
   strip left until that folder sits on the right of the workspace; everything you came
   through stays a horizontal scroll back to. Only the folder you are standing in keeps
   that sub-folder column — the panels already slid past drop theirs, since their labels
   *are* the panels to their right — but they keep every file, so the whole path stays
   readable end to end. Click a file in the SVG to see its classes and functions; click a
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

A section node's span covers the prose *between* the markers, not the markers
themselves — they are metadata for prompt-building, and `word_count` has always
counted only the prose. So `s3` above is one line, not three. An empty section
falls back to spanning its marker pair, since it has no prose to point at.

`graph.export_prose` strips those markers when exporting clean `.md` out of `export/`.

### HTML / Jinja templates

`.html`/`.htm` is parsed by `tree-sitter-html`, but through its own extractor
(`artifact._update_html_graph`) rather than the `_LANGUAGE_RULES` table. That grammar
exposes **no fields at all** — `element` has none, and an attribute's name and value have
to be read positionally — so there is no `name_field` for a rules entry to key off, and a
node per element would be thousands of nodes for one page. What becomes a node instead:

| In the template | In the graph |
|---|---|
| `<div id="chat-messages">` | `type="section"`, id `<file>::<tag>#<id>`, spanning start tag to end tag |
| an `id` element nested in another | `contains` edge from the enclosing section, not from the file |
| inline `<script>` / `<style>` | `type="section"`, `<file>::script#1` / `<file>::style#1`, numbered per tag |
| `<script src>`, `<link href>` | `imports` edge |
| `{% extends %}`, `{% include %}`, `{% import %}`, `{% from %}` | `imports` edge (read off the raw text — the grammar leaves Jinja inside a text node) |

`/static/app.js`-style targets resolve repo-root-relative, and
`{{ url_for('static', filename='vendor/x.js') }}` resolves through its `filename=`. A
`<script src="...">` is *not* an inline section — its `raw_text` is empty, and the `src`
is the edge. A duplicate `id` gets a `~2` suffix rather than silently overwriting the
first node's vector. A page with no `id`s and no inline blocks gets just its file node.

Two sharp edges worth keeping in mind when adding anything here:

- **Signatures are deliberately quote-free** — `div #input-area`, never
  `div id="input-area"`. `graph.render_file_detail` splices a signature straight into a
  DOT label, and the escaped `\"` written into graph.dot swallowed the rest of that
  node's attribute list the moment it was read back and re-emitted.
- **Never a signature that opens with `<` and closes with `>`.** graphviz reads any such
  label as an HTML-like label, and `<script>` is not valid HTML-label markup, so the whole
  file's view dies on a syntax error. Hence `inline script`, not `<script>`.

---

## Design notes

- **Graph updates are section-replacement, not full rebuild.** Each file owns a
  `// --- file: <path> ---` … `// --- end: <path> ---` block. Rewriting one file's
  block leaves every other project file untouched. The write is atomic (`write` to
  `<file>.tmp`, then `rename()`).
- **Node IDs are hierarchical:** `<file>::<Class>::<method>`. A top-level function is
  `<file>::<fn>`, an HTML section is `<file>::<tag>#<id>` (`templates/base.html::div#input-area`),
  a prose section is `<file>::<section-id>`. This doubles as the Qdrant payload key
  (`uuid5` over `node-<id>`).
- **Two project types, two snapshot shapes.** `snapshot.detect_project_type` picks
  `fiction` (reader knows / open threads / tone / last hook) versus `code`
  (current task / key decisions / what's working / what's broken). The same search
  endpoint returns both; `format_snapshot_for_context` renders the right fields.
- **Memory is stateless between requests.** `_conversation_state` holds the rolling
  summary and the last 20 exchanges in-memory; everything else (commits, nodes,
  snapshots) comes back from Qdrant on every message. Restart the server and you lose
  the working summary, not the history.

### Known limitations

- `chat_send` reads the **working tree** (`repo.read(path)`) rather than the commit a
  node belongs to, so content can be stale when browsing history.
- `merge` recomputes `repo.diff(a, b)` once **per file** instead of once per merge.
- `indexer.py` and `snapshot.py` each carry their own `_embed`, `_ensure_collection`
  and upsert/search helpers. Identical code, different names.
- `apply_edits` trusts `line_start`/`line_end` without bounds checks.
- *Build Graph* skips files over 500 KB, so a minified vendor bundle (a 2.4 MB
  `babel.min.js`) never gets a section — the directory view still lists it, since
  that comes from `git ls-files` rather than `graph.dot`. Bundles just under the
  limit are indexed, and their minified single-letter names then collide into
  duplicate node ids.

---

## License

Apache 2.0 — see `LICENSE`.
