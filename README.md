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
| **Chat** | Chat is scoped to a project and runs on the *planner* model. Every message is sent with fresh context — the recent git log, `README.md` and `graph.dot` — plus the history of the conversation you are in. Conversations are stored per project and listed in a sidebar; a new one starts with the injected context and nothing from any other conversation. |
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

The three model variables above are *defaults*. Anything set in the homepage settings
panel is written to `residuality_models.cfg` and takes precedence over the environment —
see [Model settings](#model-settings).

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
4. **Edit a node** — pick a function or section and either type the new lines yourself
   or press **AI** and describe the change, letting a model draft the replacement.
   Either way the result is a draft in the box, shown as a diff against the original;
   Save splices the range and leaves it dirty for Regenerate & Commit.
5. **Chat** — the tab is a planning conversation on the planner model. Every message
   carries the recent git log (last 10 commits), the project's `README.md` and its
   `graph.dot`, plus the history of the conversation you are in. Conversations are
   stored under `.residuality/chats/` (one JSON file each, surviving restarts) and
   listed in the sidebar; **+ New conversation** starts fresh with the injected
   context and none of the history from any other conversation.
6. **Merge** — select two commits and let the model reconcile divergent files into a
   two-parent merge commit.

### Hand-editing a node, or a whole file

The graph's node pop-up reads a node's exact line span, so the same pane edits it.
Opening a file lands on the **whole file** — the node list is on the left to narrow
into, not a gate in front of it — and a pill in the header names the file and the
range on screen (`file.py (whole file)`, then `file.py :: section` once one is
picked). **Edit** turns the source view into a text box holding *that line range
and nothing else*. The box is the final draft: `show changes` renders it as a
git-style diff against the original (additions green, deletions red and struck
through, a changed line as a deletion with its replacement beneath it, every
untouched line left in place), and unchecking it drops the rendering back to the
plain final draft. It is on by default and available only in edit mode, and the
diff collapses on save — once the file has taken the edit there is nothing left to
compare against. While editing, the range being edited sits on the left of the
footer and Save/Cancel on the right.

**Save** splices the box's lines over that range in the working file and re-runs
Build Graph for that one file, so only its `// --- file: ... ---` block of
graph.dot is rewritten. It deliberately does **not** commit: the file and graph.dot
are left dirty for Regenerate & Commit, which is also where the message comes from.
A file no parser handles (Dockerfile, LICENSE, requirements.txt) still saves, and
reports `graph unchanged`. **Cancel** reverts the node to its original state, and
navigating out of the pane with unsaved text asks first.

The "sed" step is a Python slice assignment rather than `sed -i`: the replacement
text is arbitrary, so escaping `&`, `/`, backslash and newline (and a multi-line
`c\` range) buys nothing a slice does not already do. A range that no longer
exists — a graph built before the last edit to the file — is refused rather than
silently truncating the file, and an over-long range end is clamped and reported.

### Asking a model to rewrite a node

**AI**, in the node pop-up, opens a small panel: which model, and what should change.
Submitting sends *the lines on screen* — the same range **Edit** would open — and drops
the reply into that editor box. From there it is an ordinary edit: the diff comes up
already on, Save splices it, Cancel drops it. The endpoint writes nothing and commits
nothing, so the diff *is* the review step; there is no second write path to get wrong.

A range too long for one call is **split on the graph's node boundaries** and sent as one
call per chunk, then stitched back into a single replacement. This is the index doing what
it exists for: a node begins and ends at a statement, so a cut there keeps whole functions,
where an arbitrary line number cuts one in half — and a small-context model can still rewrite
a whole file, one node at a time, without ever being handed a range it cannot hold. The
status line names how many calls it took. A gap between nodes that is too big to send is
cut on blank lines, and a lone node larger than the budget is the only case cut on lines,
because there is nothing else left to cut on. Every reply covers exactly the lines that
were on screen, so there is no partial answer and no tail to splice back underneath it.

How long is *too long* is a setting, and it is sized against the thing that actually
constrains it: the model's **context window**, in tokens (`OWUI_CONTEXT_TOKENS`,
262144 by default). One edit may send about **half** the window, converted to
characters — because the replacement comes back roughly as long as the range it
replaces, and the model has to hold both at once. At a 262144-token window that is
roughly 524288 characters.

It used to be a hardcoded 6000, which is small in absolute terms but was also measured
against nothing: a 219-line file was cut at line 170 and the tail was never sent at all,
so an ordinary "add comments to this file" silently left the last 49 lines alone. That is
what the split replaced — a long range is now several node-sized calls, which makes the
budget a per-call ceiling rather than a cap on how much can be edited. Against a
262144-token window, 6000 characters is well under 1% of what the model can hold — and
even a 32000-character limit would be only about 3%.

**Max characters per edit** takes a number to pin the budget outright, `0` for no limit,
or blank to derive it from the window again. An explicit number replaces the derived one
rather than being clamped against it, and a value that cannot be used falls back rather
than failing the request.

The reply is cleaned up in exactly two ways, because there are exactly two shapes
where what the model said is unambiguous:

- **A `…` reasoning trace is removed.** A reasoning model emits one whatever the
  prompt says. An *unclosed* trace is the awkward case — there is no way to tell where
  the reasoning stops and the answer starts — so only the tag is dropped there.
- **A markdown fence around the whole reply is unwrapped.** The reported case was a
  full-file rewrite coming back as ```` ```python …file… ``` ````, which would otherwise
  paste backticks through the entire file.

Both keep the text when they cannot tell, on the same principle: discarding model
output behind an approve-before-write box is worse than showing some noise. So a fence
with prose *around* it is deliberately left alone — for a prose target the answer can
legitimately be a sentence with a code block inside it (`Use this:\n```sh\nmake\n```
),
and nothing in the text distinguishes that from a fenced-off answer. Guessing would drop
the sentence. The diff is where that gets decided, and both flags come back with the
reply so the status line can say which cleanup happened.

Everything past that is the prompt's job, which is why it is a setting. An empty reply
is refused outright rather than opening an empty box over a live range, where Save would
quietly delete it. With several chunks, a chunk that comes back empty is kept verbatim and
named in the status line instead, and only an all-empty reply is refused — the same
principle, applied per piece of the range.

The older node page has its own edit path (`graph_edit`) which asks a model and then
**commits**. It gets the same two cleanups and the same configured prompt, because a
fenced reply there would reach history, past the point a diff can undo.

### Model settings

The homepage settings panel covers: the shared **default** model, separate **chat** and
**edit** overrides, the endpoint URL, the API key, the prompt and system prompt used for
edits, the model's **context window**, and how much of a range one edit may send. Precedence is per-call model → slot override → default → environment, and an
empty override means *follow the default* rather than *unset*.

Settings are saved to `residuality_models.cfg`, beside the projects and inside the
`/repos` mount so they survive a container recreate, and they layer *over* the
environment: an install that never opens the panel runs entirely off `.env`, and
clearing a field hands it back. The file is `0600` because it can hold an API key, and a
blank key field means *keep the stored one* — the value is never sent back to the
browser. The model list is filled in by JavaScript after the page renders, so a slow or
dead endpoint delays a dropdown rather than the homepage, and an unreachable one leaves
the saved value selected in the dropdown. The slots are `<select>`s, and each one is
rendered with its saved value already in it, so a failed fetch leaves a control that
still posts what was stored rather than an empty one that would post nothing.

**The allowlist is the API key.** `/api/models` returns whatever the configured key can
see, cached for 30s, and an edit naming a model the key cannot reach is refused before
anything is sent. When the list cannot be fetched at all, only the already-configured
models are accepted — an arbitrary id from the browser is not, and the picker keeps its
last good list rather than emptying.

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
- **Chat conversations are files, not memory.** Each conversation is a JSON file
  under `.residuality/chats/`, so history survives a restart. The injected context
  (git log, `README.md`, `graph.dot`) is re-read on every message, so it is always
  current; a conversation carries only its own history, never another's.
- **Settings layer over the environment, never replace it.** `load_config` starts from
  `.env` and applies the config file on top, so a key deleted from the file falls back
  to its environment value instead of becoming empty. The alternative — writing the
  merged result and treating the file as authoritative — makes `.env` silently stop
  mattering the first time someone opens the panel.
- **`/api/models` is fetched in one place.** It used to be duplicated, once inside
  `OWUIClient` and once inline in a route, with the inline copy calling `requests.get`
  in a module that never imported `requests`.
- **Routes belong above the startup block.** `if __name__ == "__main__": app.run(...)`
  never returns, so anything defined after it is never registered under the Docker
  entrypoint (`python app.py`). `/api/models` and `/api/models/select` sat below it and
  therefore did not exist in the deployed app at all.
- **`chat()` takes its model as an argument, not a default.** `def chat(...,
  model=DEFAULT_MODEL)` binds at import, so setting `client.DEFAULT_MODEL` changed the
  attribute and nothing about what was sent — which is why the old model-select endpoint
  appeared to do nothing.

### Known limitations

- `merge` recomputes `repo.diff(a, b)` once **per file** instead of once per merge.
- `indexer.py` and `snapshot.py` each carry their own `_embed`, `_ensure_collection`
  and upsert/search helpers. Identical code, different names.
- `apply_edits` trusts `line_start`/`line_end` without bounds checks.
- *Build Graph* skips files over 500 KB, so a minified vendor bundle (a 2.4 MB
  `babel.min.js`) never gets a section — the directory view still lists it, since
  that comes from `git ls-files` rather than `graph.dot`. Bundles just under the
  limit are indexed, and their minified single-letter names then collide into
  duplicate node ids.
- `graph_edit` (`/projects/<id>/graph/node/<node_id>`, the `graph_node.html` page) is the
  older path: it asks a model, then **commits immediately** with a generated message.
  The modal's Edit/AI flow deliberately does not use it, so the two behave differently
  for the same node — one lands in Regenerate & Commit, the other is already in history.

---

## License

Apache 2.0 — see `LICENSE`.
