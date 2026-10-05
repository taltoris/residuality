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
| **Plan** | The project's thinking space, and what the old Chat tab became. It holds a conversation on the *planner* model — every message sent with fresh context: the recent git log, `README.md` and the graph summary (`.residuality/graph_summary.txt`), plus the history of the conversation you are in — and, on demand, an **assessment**: that same context put to the model as a question about where things stand and what to do next. |
| **History** | Every commit on every branch, newest first, with parents, branches and merge markers. A literal filter narrows the list as you type; *Search by meaning* uses the vector index for when you remember the shape of a change but not its wording. From a row you can view a commit's files, branch from it, or check it out; tick two rows to diff them. |
| **Merge** | Pick two divergent commits; the model reconciles conflicting files into a merge commit with two parents. |

### Navigation

The bar is split by scope, not by importance.

- **Program-wide** (right of the gap, always rendered): **Settings** — the model slots, the
  endpoint, the API key, the edit prompt and the context window — and **Account**, which
  reports the signed-in user and holds **Log out**. These mean the same thing on every page,
  including the project list, where there is no project at all.
- **Project-scoped** (left of the gap, rendered only inside a project): **Plan**, **Project
  Control**, **History**, **Search**, **Merge**, **Export**. They are absent from the project
  list because they have nothing to point at there.

The git remote moved the other way: a remote belongs to one repository, so its panel lives on
that project's Project Control page rather than under the program-wide Settings link.

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
/app/app.py              # routes, auth, plan, graph, history, merge, export
/app/artifact.py         # Git-backed artifact store + graph updates
/app/graph.py            # DOT parsing, SVG rendering, prose export
/app/indexer.py          # Qdrant upsert/search (commits + graph nodes)
/app/snapshot.py         # episodic snapshot generation + storage
/app/context.py          # context compression for model calls
/app/owui_client.py      # Open WebUI API client
/app/.residuality/extract-python.scm   # tree-sitter query for Python
<repo>/.residuality/graph.dot          # the graph, one section per file
<repo>/.residuality/graph_summary.txt  # its prompt-sized outline (on-disk twin)
/templates/*.html        # Jinja UI
/static/residuality.js   # graph drill-down, gitignore, plan, file lists
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

### Behind a reverse proxy (Nginx Proxy Manager)

The container serves plain HTTP on 5010; the proxy terminates TLS and forwards to it.

**Proxy host**

| Field | Value |
|---|---|
| Domain Names | `res.mywebsite.com` |
| Scheme | `http` |
| Forward Hostname / IP | the Docker host's LAN address (the port is published), or the container name if NPM shares its Docker network |
| Forward Port | `5010` |
| Access List | *Public* — a set Access List answers the whole host with 403 |
| Block Common Exploits | **off** if the login redirect 403s — see below |

Do not point Forward Hostname at `localhost` from inside the NPM container: that is
NPM's own loopback, not the host's. Use the LAN IP, or attach both containers to one
network and use `residuality`.

**SSL** — attach a certificate and turn on Force SSL. **Advanced** — NPM already sets
`Host`, `X-Real-IP` and `X-Forwarded-For`; add the scheme so Flask can tell it is on
HTTPS:

```nginx
proxy_set_header X-Forwarded-Proto $scheme;
proxy_set_header X-Forwarded-Host  $host;
client_max_body_size 512m;   # export zips and large commits
proxy_buffering off;         # a Plan assessment can take a while
```

**Why the app needed changing too.** Under plain WSGI Flask sees only the socket:
HTTP, `127.0.0.1:5010`. It built the post-login redirect from `request.url`, which is
always absolute, so an unauthenticated hit produced
`/login?next=https://res.mywebsite.com/`. That is the exact shape of an open-redirect
payload, and a proxy or WAF rule matching that shape (NPM's *Block Common Exploits* is
the usual one) answers **403** itself, before Flask is ever reached. Hence:

- **`ProxyFix`** — the app now believes `X-Forwarded-Proto` / `-Host`, so it knows it
  is on HTTPS at the public name. Disable with `TRUST_PROXY=false` if nothing proxies.
- **`next` is a bare path** — the redirect is `/login?next=/`, which is both correct
  (it survives the host changing) and not something a WAF objects to. The value is
  validated on the way back in, so the login page is no longer an open redirect.
- **`SESSION_COOKIE_HTTPONLY` / `SAMESITE`** are set, and `SESSION_COOKIE_SECURE` is
  available for a TLS-only deployment — off by default so the direct port-5010 URL can
  still sign in.

To confirm which layer is answering 403, compare the two:

```bash
curl -sI "https://res.mywebsite.com/login?next=/"                     # expect 200
curl -sI "https://res.mywebsite.com/login?next=https://res.mywebsite.com/"   # 403 = a rule in front
curl -sI "http://<docker-host>:5010/login?next=https://res.mywebsite.com/"   # 200 = the app is fine
```

If the first is 200 and the second 403, the rule is keyed on the URL shape and the fix
is on the proxy (Block Common Exploits off, or a narrower custom rule).

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
| `TRUST_PROXY` | `true` — believe `X-Forwarded-*` from the proxy |
| `SESSION_COOKIE_SECURE` | `false` — set `true` for a TLS-only deployment |
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
   Residuality writes `.residuality/extract-python.scm`, an empty `graph.dot` and
   `graph_summary.txt`, and a default `.gitignore`, then commits them as `Init project: <id>`.
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
5. **Plan** — the tab is this project's thinking space. The conversation runs on the
   planner model; every message carries the recent git log (last 10 commits), the
   project's `README.md` and its graph summary — `.residuality/graph_summary.txt`,
   files, imports and symbol names, not the raw DOT — plus the history of the
   conversation you are in. Conversations
   are stored under `.residuality/chats/` (one JSON file each, surviving restarts) and
   listed in the sidebar; **+ New conversation** starts fresh with the injected context
   and none of the history from any other conversation. **Assess project state** puts that
   same context to the model as a question — latest snapshot, graph outline, recent log —
   and answers with an assessment, a next task, the files it expects to touch, risks and
   open questions. It also reports which of those three inputs it actually had, so a
   project with nothing indexed yet reads as *not yet* rather than as an empty project.
7. **Merge** — select two commits and let the model reconcile divergent files into a
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
graph.dot is rewritten (and the graph summary is re-rendered to match). It
deliberately does **not** commit: the file, graph.dot and the summary are left
dirty for Regenerate & Commit, which is also where the message comes from.
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

The settings page (`/settings`) covers: the shared **default** model, separate **chat** and
**edit** overrides, the endpoint URL, the API key, the prompt and system prompt used for
edits, the model's **context window**, and how much of a range one edit may send. Precedence is per-call model → slot override → default → environment, and an
empty override means *follow the default* rather than *unset*.

Settings are saved to `residuality_models.cfg`, beside the projects and inside the
`/repos` mount so they survive a container recreate, and they layer *over* the
environment: an install that never opens the page runs entirely off `.env`, and
clearing a field hands it back. The file is `0600` because it can hold an API key, and a
blank key field means *keep the stored one* — the value is never sent back to the
browser. The model list is filled in by JavaScript after the page renders, so a slow or
dead endpoint delays a dropdown rather than the page itself, and an unreachable one leaves
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
| `<div id="plan-messages">` | `type="section"`, id `<file>::<tag>#<id>`, spanning start tag to end tag |
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
- **Plan conversations are files, not memory.** Each conversation is a JSON file
  under `.residuality/chats/`, so history survives a restart. The injected context
  (git log, `README.md`, the graph outline) is re-read on every message, so it is
  always current; a conversation carries only its own history, never another's.
- **Plan carries a graph *summary*, not graph.dot.** `render_graph_summary`
  re-renders the parsed graph as files, imports and symbol names. The raw DOT is
  192 KB here — ~48K tokens on *every* message, most of a conversation's budget
  gone before the first question — and 52% of it was two vendored React bundles.
  Three properties make the outline small: `contains` edges are pure restatement
  (a node id already carries its own path, so 702 of the 781 edges say nothing the
  ids do not), `imports` edges are already module-level names rather than node ids,
  and the only containment worth keeping is that a method belongs to a class.
  Line numbers are deliberately left out: the outline answers "what exists", and
  the exact span is looked up from the graph when an edit is actually made.
- **The summary is a file, and context reads it.** The outline is written to
  `.residuality/graph_summary.txt` — atomically, like `graph.dot` — every time a
  graph update runs, so it is the on-disk twin of the graph. Chat and Plan read
  that file instead of re-rendering `graph.dot` per message: what the model sees
  is exactly what the last build produced, and the two can never drift apart.
  A project whose graph predates the file gets it backfilled on first open
  (`_ensure_residuality` re-renders from the existing `graph.dot`), so there is
  no migration step. One known wrinkle: within a *batch* operation (Build Graph
  over many files) the file is written once, mid-batch, and can trail the final
  `graph.dot` until the next single-file update re-renders it; single-file
  operations always leave it exact.
- **The commit list is a table, and the drawn DAG is gone.** `/projects/<id>` used to render
  a `vis-network` graph of commit parentage. The list carries the same information — parents,
  branches, merge markers — in a form that can be filtered, searched and acted on, without a
  CDN dependency, and `/projects/<id>` now redirects to Project Control so every old link and
  every post-action redirect lands somewhere real.
- **The Plan assessment exists because the button did not.** Project Control had a Plan button
  posting to `/projects/<id>/plan`, and no such route was ever written: it POSTed into a 404,
  got an HTML error page back, and died on `JSON.parse` of the first character. The client
  method (`OWUIClient.generate_plan`) and the frontend stub both existed; the route between
  them is what was missing. The assessment lives on the Plan page now.
- **Latest-snapshot lookup is a scroll, not a search.** `snapshot.latest_snapshot` sorts the
  stored payloads on their timestamp. Asking the vector index for "what happened last" would
  be asking a relevance question a recency question, and could answer with a very *relevant*
  old snapshot. Both it and the semantic search map payloads through one function
  (`_snapshot_from_payload`), so a field cannot exist on one path and be missing on the other.

- **The app is proxy-aware, and its login redirect is a path.** Two separate bugs
  met on the same page. WSGI has no idea it is behind TLS, so the app generated
  absolute `http://` URLs (`ProxyFix` fixes that); and `next=request.url` put a full
  `https://` URL inside a query parameter, which is the open-redirect shape a WAF
  blocks with a 403 before the app runs. A relative path fixes both the security
  hole and the 403, and it survives the public name changing.
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
  limit *are* indexed, and they don't fit a line-addressed graph.
  `static/vendor/react-dom.production.min.js` averages 493 chars per line, and five
  separate functions (`mb`, `Ab`, `bj`, `dj`, `ej`) all sit on line 14 — so 61% of
  its 346 nodes record the same single line, two of them cannot be told apart by
  address, and a line-granular edit of any one would replace the other four with it.
  The fix is a second address: `node.start_byte`/`node.end_byte` are already read
  (they build each node's signature), so a char range could be recorded alongside
  the line range for nodes whose lines do not distinguish them. Whether an edit
  should then be *allowed* on a minified bundle is a separate question — it is the
  one file in the tree nobody hand-edits.
- Separately, minified bundles have no place in a prompt at all, whatever the graph
  records for them — they are dependencies, not code to navigate. `render_graph_summary`
  detects them by path convention or by symbol density (React DOM scores 1.30 symbols
  per line against 0.08 for the densest hand-written file here) and lists the file by
  name only, with no symbols: 370 mangled single-letter names would be the one thing
  that put the bundles back into the context.
- `graph_edit` (`/projects/<id>/graph/node/<node_id>`, the `graph_node.html` page) is the
  older path: it asks a model, then **commits immediately** with a generated message.
  The modal's Edit/AI flow deliberately does not use it, so the two behave differently
  for the same node — one lands in Regenerate & Commit, the other is already in history.

---

## License

Apache 2.0 — see `LICENSE`.
