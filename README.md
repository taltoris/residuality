Residuality: A Semantic Artifact Versioning System

"What remains when everything transient is stripped away — that is the truth of the system."



Name & Concept

Residuality — what remains after stress, excavation, and time. The residue that persists when everything transient has been stripped away.

Like an archaeological dig, Residuality lets you:





Excavate back through artifact history to any decision point



Branch timelines and explore alternate paths



Semantically search through layers to find "the version where X worked"



Merge divergent timelines with model-assisted reconciliation

The artifacts are the dig site. Git is the stratigraphy. Qdrant is the semantic index that lets you find the right layer without digging through everything manually.



What Problem Does This Solve?

The Context Pollution Problem

Every LLM conversation degrades over time. Studies show a 39% average performance drop in multi-turn conversations as models become confused by accumulated context, make premature assumptions, and fail to course-correct.

Current solutions are bad:





Continue in a polluted context → model gets confused



Start fresh → lose all accumulated work



Copy-paste between chats → manual, error-prone, loses structure

The Lost Decision Problem

When you're building something complex across many sessions — a codebase, a novel, a strategic plan — you lose track of why things are the way they are. You can't easily answer:





"Find the version before we added OAuth"



"What was the story before we introduced the plot twist?"



"When did this algorithm work correctly?"

The Single-Model Bottleneck Problem

Complex tasks have phases that require different capabilities:





Planning → needs deep reasoning (Qwen-27B-Think)



Implementing → needs fast code generation (Qwen-35B-Instruct)



Summarizing → needs efficient compression (Bonsai)



Routing → needs vision + judgment (MiniCPM-V)

Forcing all phases through one model wastes resources and degrades quality.



Architecture Overview

┌─────────────────────────────────────────────────────────┐
│                    Residuality Flask App                      │
│  ┌──────────────┐  ┌──────────────┐  ┌───────────────┐ │
│  │  DAG Viewer  │  │  Diff View   │  │  Chat Panel   │ │
│  │  (artifact   │  │  (commit A   │  │  (context-    │ │
│  │   tree)      │  │   vs B)      │  │   aware)      │ │
│  └──────────────┘  └──────────────┘  └───────────────┘ │
└─────────────────────────────────────────────────────────┘
         │                    │                  │
         ▼                    ▼                  ▼
┌─────────────┐      ┌─────────────┐    ┌──────────────┐
│  Git Repos  │      │   Qdrant    │    │  Open WebUI  │
│  /repos/    │◄────►│  (semantic  │    │  (semantic   │
│  (artifact  │      │   index of  │    │   router +   │
│   versions) │      │   commits)  │    │   models)    │
└─────────────┘      └─────────────┘    └──────────────┘


External Dependencies (Already Running)





Qdrant at 192.168.0.100:6333 — semantic search



Open WebUI — model routing, tool execution



MiniCPM-V — routing decisions



Bonsai-8B — summarization, tool calls



Qwen models — heavy reasoning and code generation

New Components (To Build)





Residuality Flask App — UI + artifact management API



Git repos — one per project, stored on server filesystem



Residuality Qdrant collection — separate from existing Semantic collection



The Core Data Model

A Commit Node

{
  "commit_hash": "a3f9b2c1",
  "parent_hashes": ["7d4e1a09"],
  "branch": "main",
  "project_id": "my-novel",
  "artifact_path": "chapter3.md",
  "message": "Introduced villain reveal. Motivation: user wanted darker tone. Status: chapter coherent, chapter 4 needs update.",
  "timestamp": 1748000000,
  "turn": 7,
  "model_used": "qwen36-27b-think",
  "is_merge": false
}


A merge commit has two parents:

{
  "commit_hash": "f1c2d3e4",
  "parent_hashes": ["a3f9b2c1", "b9e7f6a2"],
  "is_merge": true,
  "message": "Reconciled villain-reveal branch with hero-wins main. Rewrote chapter 4 to acknowledge villain's past."
}


The Artifact Tree (DAG)

commit_0 ── commit_1 ── commit_2 ── commit_3a (main)
                              │              │
                              │         commit_4a ── commit_MERGE
                              │                           │
                        commit_3b ── commit_4b ───────────┘
                        (branch: plot-twist)


Every node knows its parent(s). Branches are just named pointers to leaf nodes. The tree can be reconstructed from parent_hash relationships stored in Qdrant.



Context Compression Pipeline

This is how Residuality prevents context pollution. Instead of every model seeing the full raw conversation, each model sees a compressed, relevant slice.

The Flow

Turn 0:
  user: "Hi"
  → route to Bonsai (trivial)
  → Bonsai: "Hello! How can I help?"
  → store: summary_0 = "User greeted, assistant offered help"

Turn 1:
  user: "Look at this picture <kitten.png>"
  → route to MiniCPM (vision)
  → MiniCPM sees: [summary_0] + [current message + image]
  → MiniCPM: "That's a cute kitten!"
  → store: summary_1 = "User shared kitten image, assistant described it"

Turn 2:
  user: "cool"
  → route to Bonsai (trivial, text only — image stripped)
  → Bonsai sees: [summary_1] + ["cool"]  ← no image, no raw history
  → store: summary_2 = "Brief positive acknowledgment"

Turn 3:
  user: "Let's architect a distributed cache system"
  → route to Qwen-27B-Think (complex reasoning)
  → Qwen sees: [summary_2] + [last exchange] + [current message]
  → Qwen: "<architectural plan>"
  → git commit: "Initial cache architecture. Redis + consistent hashing."
  → Qdrant indexes commit message
  → store: summary_3 = "User requested cache architecture, assistant proposed Redis with consistent hashing"


Key Rules





Images are stripped when routing to text-only models, but noted in the summary ("user shared an image")



Only the rolling summary + last 1-2 raw exchanges are passed to the target model — never the full history



Artifacts (code, prose, plans) live in git — they are NOT part of the conversation context. The model reads the current file, modifies it, and commits.



The summary lives in Qdrant — retrieved at the start of each turn



The Artifact Lifecycle

Creating an Artifact

user: "write a sorting function"
→ Residuality creates: /repos/my-project/sort.py (empty)
→ routes to Qwen-35B-Instruct
→ model writes the function
→ Residuality auto-commits: "Initial quicksort implementation"
→ Qdrant indexes commit message


Modifying an Artifact

user: "handle edge cases"
→ Residuality reads: current sort.py content
→ routes to Qwen-35B-Think
→ model sees: [summary] + [sort.py contents] + [instruction]
→ model rewrites sort.py
→ Residuality commits: "Added null/empty/single-element handling. All edge cases passing."
→ Qdrant indexes


Branching from a Commit

user: "let's try a completely different approach from before the edge case work"
→ Residuality: create_branch("merge-sort-approach", from_commit="initial_hash")
→ routes to Qwen-35B-Think
→ model starts fresh from the initial implementation
→ develops along new branch


Merging Two Commits

user: "the merge-sort branch is better, but I want the edge case handling from main"
→ Residuality: merge_commits(commit_a="merge-sort-tip", commit_b="main-tip")
→ routes to Qwen-27B-Think (complex reconciliation task)
→ model sees: [version A content] + [version B content] + [diff] + [instruction]
→ model produces reconciled version
→ Residuality commits merge: two parent hashes recorded
→ Qdrant indexes merge commit message


Searching History

user: "find the version where the auth system was working"
→ Residuality: search_history("auth system working")
→ Qdrant semantic search over commit messages
→ returns ranked list of relevant commits with metadata
→ UI shows: "commit a3f9 — 'OAuth integration complete, all tests passing' (3 days ago)"
→ user: "restore that one"
→ Residuality: git checkout a3f9




Components To Build

1. Residuality Flask Application

File structure:

residuality/
├── app.py                 # Flask app, routes
├── artifact.py            # ArtifactRepo class (gitpython)
├── indexer.py             # Qdrant indexing of commits
├── context.py             # Context compression pipeline
├── owui_client.py         # Open WebUI API client
├── templates/
│   ├── base.html
│   ├── projects.html      # Project list
│   ├── dag.html           # DAG visualization
│   ├── diff.html          # Commit diff view
│   ├── search.html        # Semantic search UI
│   ├── chat.html          # Chat panel
│   └── settings.html      # Project settings (optional remote)
├── static/
│   └── dag.js             # DAG visualization (vis.js)
├── dot_updater.c          # C binary source
├── cJSON.c / cJSON.h      # JSON parser for dot_updater
├── Dockerfile
└── requirements.txt


Flask routes:

GET  /                              → project list
GET  /projects/new                  → create project form
POST /projects/new                  → init new git repo
GET  /projects/<id>                 → DAG view
GET  /projects/<id>/commit/<h>      → view artifact at commit
GET  /projects/<id>/diff/<a>/<b>    → diff two commits
POST /projects/<id>/branch          → create branch from commit
POST /projects/<id>/merge           → merge two commits
GET  /projects/<id>/search          → semantic search UI
POST /projects/<id>/search          → execute search, return results
GET  /projects/<id>/chat            → chat panel
POST /projects/<id>/chat            → send message, get routed response
POST /projects/<id>/checkout/<h>    → restore artifact to commit
GET  /projects/<id>/settings        → project settings (remote URL etc.)
POST /projects/<id>/settings        → save settings
POST /projects/<id>/remote/add      → link GitHub/GitLab remote (optional)
POST /projects/<id>/remote/push     → push to remote (optional)
POST /projects/<id>/remote/pull     → pull from remote (optional)




2. ArtifactRepo (gitpython wrapper)

Methods to implement:

class ArtifactRepo:
    def __init__(self, project_id: str)
    
    def init(self, project_id: str) -> str
        # Create new git repo at /repos/{project_id}
        # Return project_id
    
    def write(self, path: str, content: str, 
              message: str, branch: str = None,
              model_used: str = None, turn: int = None) -> str
        # Write content to file
        # git add + git commit
        # Return commit hash
    
    def read(self, path: str, commit: str = None) -> str
        # Read file at HEAD or specific commit
    
    def branch(self, name: str, from_commit: str, reason: str = "") -> str
        # git checkout -b {name} {from_commit}
        # Return branch name
    
    def merge(self, commit_a: str, commit_b: str, 
              content: str, message: str) -> str
        # Create merge commit with two parents
        # Write reconciled content
        # Return merge commit hash
    
    def get_dag(self) -> List[dict]
        # Return all commits as list of nodes
        # Each node: {hash, parents, message, branch, timestamp}
    
    def diff(self, commit_a: str, commit_b: str) -> str
        # Return unified diff between two commits
    
    def checkout(self, commit: str) -> None
        # Restore working tree to commit state
    
    def log(self) -> List[dict]
        # Return commit history with metadata

    # --- Optional remote operations ---

    def add_remote(self, url: str, name: str = "origin") -> None
        # git remote add {name} {url}
        # Supports HTTPS (token auth) and SSH remotes
        # Stored in project settings, never required

    def push(self, remote: str = "origin", branch: str = "main") -> None
        # git push {remote} {branch}
        # Uses GITHUB_TOKEN env var for HTTPS, mounted SSH key for SSH
        # Raises if no remote configured

    def pull(self, remote: str = "origin", branch: str = "main") -> None
        # git pull {remote} {branch}
        # Merges remote changes into local repo

    def remove_remote(self, name: str = "origin") -> None
        # git remote remove {name}




3. Qdrant Indexer

Collection: residuality_commits (separate from existing Semantic collection)

Schema:

{
    "commit_hash": str,        # keyword index
    "project_id": str,         # keyword index  
    "branch": str,             # keyword index
    "parent_hashes": List[str],# stored, not indexed
    "artifact_path": str,      # keyword index
    "message": str,            # text, embedded as dense vector
    "timestamp": int,          # integer index (range queries)
    "turn": int,               # integer index
    "model_used": str,         # keyword index
    "is_merge": bool,          # bool index
}


Methods:

class ResidualityIndexer:
    def index_commit(self, commit_data: dict) -> None
        # Embed commit message via lcpp-embed
        # Upsert to Qdrant with all metadata
    
    def search(self, query: str, project_id: str = None,
               branch: str = None, limit: int = 10) -> List[dict]
        # Embed query
        # Qdrant search with optional filters
        # Return ranked commit list
    
    def get_commit(self, commit_hash: str) -> dict
        # Retrieve single commit by hash
    
    def get_project_commits(self, project_id: str) -> List[dict]
        # All commits for a project (for DAG reconstruction)




4. Context Compression Pipeline

This is the core innovation — what gets sent to each model.

class ResidualityContext:
    def build_context(self, 
                      project_id: str,
                      chat_id: str,
                      user_message: str,
                      artifact_path: str = None,
                      has_images: bool = False) -> dict
        """
        Build the compressed context to send to the target model.
        Returns: {
            "messages": [...],     # what to send to the model
            "summary": str,        # current rolling summary
            "artifact": str,       # current artifact content (if any)
        }
        """
        # 1. Fetch rolling summary from Qdrant (existing Semantic collection)
        # 2. Fetch current artifact from git (if project active)
        # 3. Build messages array:
        #    [system: summary + artifact] + [last_exchange] + [current_message]
        # 4. Strip images if routing to text-only model
    
    def update_summary(self, chat_id: str, 
                       user_message: str,
                       assistant_message: str,
                       turn: int) -> str
        """
        Generate and store updated rolling summary.
        Uses Bonsai for speed.
        """




5. Open WebUI Client

class OWUIClient:
    def __init__(self, base_url: str, api_key: str)
    
    async def chat(self, messages: List[dict],
                   model: str = "semantic-router") -> str
        # POST to /api/chat/completions
        # Semantic router picks the actual model
        # Return assistant message content
    
    async def get_models(self) -> List[dict]
        # GET /api/models
        # Return available model list




6. DAG Visualization

Use vis.js Network (simpler than d3 for graphs, pure JS, no build step needed for Flask).

// dag.js
function renderDAG(commits) {
    // commits: [{hash, parents, message, branch, timestamp}]
    
    const nodes = commits.map(c => ({
        id: c.hash,
        label: c.hash.slice(0, 7),
        title: c.message,  // tooltip
        color: branchColor(c.branch),
        shape: c.is_merge ? "diamond" : "dot"
    }));
    
    const edges = [];
    commits.forEach(c => {
        c.parents.forEach(p => {
            edges.push({ from: p, to: c.hash });
        });
    });
    
    const network = new vis.Network(container, { nodes, edges }, options);
    
    network.on("click", function(params) {
        if (params.nodes.length > 0) {
            const hash = params.nodes[0];
            // Show commit detail panel
            // Offer: view artifact, branch from here, diff with another
        }
    });
}




7. Docker Container

FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Git config
RUN git config --global user.email "residuality@local" && \
    git config --global user.name "Residuality"

VOLUME /repos

CMD ["python", "app.py"]


# docker-compose addition
residuality:
  build: ./residuality
  container_name: residuality
  ports:
    - "5010:5000"
  volumes:
    - ./repos:/repos
    - ~/.ssh:/root/.ssh:ro          # optional — for SSH remotes (GitHub/GitLab)
  environment:
    - OWUI_URL=http://192.168.0.100:8080
    - OWUI_API_KEY=${OWUI_API_KEY}
    - QDRANT_URL=http://192.168.0.100:6333
    - EMBED_URL=http://192.168.0.100:8090
    - REPOS_PATH=/repos
    - GITHUB_TOKEN=${GITHUB_TOKEN}  # optional — for HTTPS remotes
  restart: unless-stopped


Neither ~/.ssh nor GITHUB_TOKEN are required. If absent, remote push/pull routes return a clear error. The local workflow is fully functional without them.

requirements.txt:
flask>=3.0
gitpython>=3.1
qdrant-client>=1.9
aiohttp>=3.9
requests>=2.31
graphviz>=0.20
pydot>=3.0


C binary (dot_updater) and tree-sitter CLI are installed at Docker build time — no Python dependencies for parsing.



What This Enables

For Code





Exploration without fear — branch from any working commit, experiment freely, merge what works



Decision archaeology — "find when the tests were passing" returns a specific commit instantly



Multi-model code review — Qwen-Think plans the architecture, Qwen-Instruct writes the code, Bonsai summarizes what changed



Automatic context compression — model always sees current file + summary, never a 200-turn conversation

For Writing





Timeline branching — "what if the protagonist had made a different choice in chapter 3?" → branch from that commit



Merge narratives — develop two alternate endings, merge the best elements from each with model assistance



Continuity checking — semantic search finds all commits where a character was mentioned, ensuring consistency across rewrites

For Planning





Strategy trees — branch from key decision points, explore multiple strategic paths simultaneously



Decision audit trail — every commit message records not just what changed but why



Rollback with context — restore any previous plan state with full understanding of why it was abandoned



Implementation Order (Suggested for Your Local Models)

Phase 1 — Core infrastructure (give this to Qwen-35B-Instruct)





artifact.py — ArtifactRepo with gitpython



indexer.py — Qdrant indexer for commits



Basic Flask routes for project CRUD

Phase 2 — Context pipeline (give this to Qwen-27B-Think) 4. context.py — compression pipeline 5. owui_client.py — Open WebUI integration 6. Chat panel that uses compressed context

Phase 3 — UI (give this to Qwen-35B-Instruct) 7. DAG visualization with vis.js 8. Diff viewer 9. Semantic search UI

Phase 4 — Advanced (give this to Qwen-27B-Think) 10. Model-assisted merge reconciliation 11. Cross-project semantic search 12. Branch strategy suggestions

Phase 5 — Code Structure Graph (give this to Qwen-35B-Instruct + Qwen-27B-Think) 13. dot_updater.c + cJSON.c — C binary for surgical dot file updates 14. extract.scm — tree-sitter query for Python classes, functions, imports 15. graph.py — thin Python wrapper shelling out to tree-sitter CLI + dot_updater 16. Code structure visualization (Graphviz SVG served by Flask) 17. Surgical multi-edit workflow with bottom-up line ordering 18. Cross-file traversal via pydot for context assembly



Code Structure Graph (Phase 5)

This capability turns Residuality into a precision editing tool. Instead of reading whole files into context, it reads individual functions — the smallest meaningful unit of code.

The Core Insight

Python imports are explicit edge declarations. The import table fully resolves all inter-file dependencies without type inference:

# file_a.py
from file_b import SessionStore    # edge: file_a → file_b.SessionStore
from utils import hash_password    # edge: file_a → utils.hash_password
import database                    # edge: file_a → database (module-level)


Every call inside a function body can be resolved against the import table unambiguously. If verify_token calls SessionStore.create and SessionStore was imported from file_b, the edge is file_a.AuthManager.verify_token → file_b.SessionStore.create. No type inference needed.

The Graph Structure

file_a.py ──imports──► file_b.py
    │                      │
    ▼                      ▼
AuthManager            SessionStore
    │                      │
    ├── __init__           ├── create ◄── called_by ── AuthManager.verify_token
    ├── login              └── invalidate
    └── verify_token ──calls──► SessionStore.create


Node types: File (module), Class, Function / Method

Edge types:





imports — file imports class or function from another file



contains — file contains class, class contains method



calls — function calls another function



called_by — inverse of calls, for traversal

Storage: git + dot file + Qdrant

The graph is stored as a .dot file versioned alongside the code — no database needed for structure. Qdrant handles semantic search over node content only.

/repos/my-project/
├── src/
│   ├── auth.py
│   └── database.py
└── .residuality/
    └── graph.dot        ← versioned alongside the code, updated every commit


Every commit that changes code also updates graph.dot atomically. The graph history is perfectly synchronized with the code history — checkout any commit and get the exact graph that reflected the codebase at that moment. Graph diffs are human-readable in git diff.

git + dot file — graph structure, history, and diffing:

digraph residuality {
    "file_a.py" [type="file"];
    "file_b.py" [type="file"];
    "AuthManager" [type="class", file="file_a.py"];
    "AuthManager.verify_token" [type="function", file="file_a.py",
                                 line_start=47, line_end=89,
                                 signature="verify_token(self, token)"];
    "SessionStore" [type="class", file="file_b.py"];
    "SessionStore.create" [type="function", file="file_b.py",
                            line_start=92, line_end=110];

    "file_a.py" -> "AuthManager" [label="contains"];
    "AuthManager" -> "AuthManager.verify_token" [label="contains"];
    "file_a.py" -> "file_b.py" [label="imports"];
    "AuthManager.verify_token" -> "SessionStore.create" [label="calls"];
}


Traversal in Python via pydot — no SQL needed:

import pydot

graphs = pydot.graph_from_dot_file(".residuality/graph.dot")
graph = graphs[0]

# find all functions that call verify_token
callers = [
    edge.get_source()
    for edge in graph.get_edges()
    if edge.get_destination() == "AuthManager.verify_token"
    and edge.get("label") == '"calls"'
]

# find line range for a node
node = graph.get_node("AuthManager.verify_token")[0]
line_start = int(node.get("line_start"))
line_end = int(node.get("line_end"))


Qdrant (residuality_graph collection) — semantic search over node signatures and docstrings only:

search("validates authentication token")
→ AuthManager.verify_token (score: 0.94)
→ SessionStore.check_session (score: 0.81)


What you gain over SQLite:





No additional dependency (pydot is lightweight)



Graph history is free — git tracks it alongside the code



Human-readable diffs: git diff .residuality/graph.dot shows exactly which edges changed



Single source of truth — no sync issues between a database and git



Graphviz renders the dot file directly

Updated requirements.txt:

flask>=3.0
gitpython>=3.1
qdrant-client>=1.9
aiohttp>=3.9
requests>=2.31
graphviz>=0.20
pydot>=3.0


The graph.py Module

graph.py is a thin Python wrapper — it shells out to tree-sitter CLI and the C dot_updater binary. No Python parsing at all.

import subprocess
from pathlib import Path

QUERY_PATH = ".residuality/extract.scm"
DOT_UPDATER = ".residuality/bin/dot_updater"

class CodeGraphBuilder:

    def __init__(self, repo_path: str):
        self.repo_path = Path(repo_path)
        self.dot_path  = self.repo_path / ".residuality" / "graph.dot"

    def update_file(self, filepath: str):
        """
        Reparse a single file with tree-sitter and surgically
        update its section in graph.dot. Called after every commit
        that touches this file.
        """
        result = subprocess.run(
            ["tree-sitter", "query", QUERY_PATH, filepath, "--json"],
            capture_output=True, text=True, cwd=self.repo_path
        )
        subprocess.run(
            [DOT_UPDATER, str(self.dot_path), filepath],
            input=result.stdout, text=True, cwd=self.repo_path
        )

    def apply_edits(self, filepath: str, edits: list[dict]):
        """
        Apply multiple function edits to a file bottom-up,
        then reparse once.

        edits: [
            {"line_start": 234, "line_end": 251, "new_content": "..."},
            {"line_start": 117, "line_end": 126, "new_content": "..."},
            {"line_start": 45,  "line_end": 67,  "new_content": "..."},
        ]
        """
        # Sort descending — edit from bottom of file upward
        # so earlier line numbers are unaffected by each edit
        edits_sorted = sorted(edits, key=lambda e: e["line_start"], reverse=True)

        lines = Path(filepath).read_text().splitlines()

        for edit in edits_sorted:
            new_lines = edit["new_content"].splitlines()
            lines = (
                lines[:edit["line_start"] - 1] +
                new_lines +
                lines[edit["line_end"]:]
            )

        Path(filepath).write_text("\n".join(lines))

        # One tree-sitter run covers all edits
        self.update_file(filepath)


The tree-sitter Query

.residuality/extract.scm — queries classes, functions, and imports:

; Capture class definitions with name and range
(class_definition
  name: (identifier) @class.name) @class.def

; Capture function/method definitions with name and range
(function_definition
  name: (identifier) @function.name) @function.def

; Capture imports
(import_from_statement
  module_name: (dotted_name) @import.module
  name: (dotted_name) @import.name)

(import_statement
  name: (dotted_name) @import.module)


tree-sitter outputs JSON with node names, start_point (row, col), and end_point for every match. Rows are 0-indexed; dot_updater converts to 1-indexed line numbers.

The dot_updater C Binary

dot_updater reads tree-sitter JSON from stdin and surgically replaces the section of graph.dot belonging to the changed file. Every other file's nodes and edges are untouched.

/* dot_updater.c
 *
 * Usage: tree-sitter query extract.scm <filepath> --json | dot_updater <graph.dot> <filepath>
 *
 * Algorithm:
 *   1. Read graph.dot into memory line by line
 *   2. Find the section bounded by:
 *        // --- file: <filepath> ---
 *        // --- end: <filepath> ---
 *      If not found, append a new section.
 *   3. Read tree-sitter JSON from stdin
 *   4. Parse JSON → extract nodes (classes, functions) and import edges
 *   5. Generate fresh dot lines for this file's section:
 *        - file node with line_start=1, line_end=<total lines>
 *        - class nodes with absolute line_start, line_end, signature
 *        - function nodes with absolute line_start, line_end, signature
 *        - contains edges (file→class, class→method)
 *        - import edges (file→module)
 *        - calls edges resolved against import table
 *   6. Replace old section with new section
 *   7. Atomic write: write to graph.dot.tmp, then rename() to graph.dot
 *
 * Dependencies: cJSON (single-header JSON parser, included in repo)
 * Build: gcc -O2 -o dot_updater dot_updater.c cJSON.c
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "cJSON.h"

#define MAX_FILE_SIZE  (4 * 1024 * 1024)  /* 4MB */
#define MAX_LINE       4096
#define SECTION_FMT    "    // --- file: %s ---\n"
#define SECTION_END    "    // --- end: %s ---\n"

typedef struct {
    char  id[512];
    char  type[32];       /* "file", "class", "function" */
    char  file[512];
    int   line_start;
    int   line_end;
    char  signature[512];
} DotNode;

typedef struct {
    char  from[512];
    char  to[512];
    char  label[64];      /* "contains", "imports", "calls" */
} DotEdge;

/* ... full implementation ... */

int main(int argc, char *argv[]) {
    if (argc < 3) {
        fprintf(stderr, "Usage: dot_updater <graph.dot> <filepath>\n");
        return 1;
    }

    char *dot_path = argv[1];
    char *filepath = argv[2];

    /* 1. Read existing dot file */
    char *dot_contents = read_file(dot_path);

    /* 2. Read tree-sitter JSON from stdin */
    char *ts_json = read_stdin();
    cJSON *root   = cJSON_Parse(ts_json);

    /* 3. Parse JSON into nodes and edges */
    DotNode nodes[1024]; int node_count = 0;
    DotEdge edges[4096]; int edge_count = 0;
    parse_treesitter_output(root, filepath, nodes, &node_count,
                                             edges, &edge_count);

    /* 4. Generate new section */
    char new_section[MAX_FILE_SIZE];
    generate_dot_section(filepath, nodes, node_count,
                                   edges, edge_count, new_section);

    /* 5. Replace section in dot_contents */
    char updated[MAX_FILE_SIZE * 2];
    replace_file_section(dot_contents, filepath, new_section, updated);

    /* 6. Atomic write */
    char tmp_path[1024];
    snprintf(tmp_path, sizeof(tmp_path), "%s.tmp", dot_path);
    write_file(tmp_path, updated);
    rename(tmp_path, dot_path);  /* atomic on Linux */

    cJSON_Delete(root);
    free(dot_contents);
    free(ts_json);
    return 0;
}


The dot File Format (Annotated)

digraph residuality {

    // --- file: src/auth.py ---
    "src/auth.py" [type="file", line_start=1, line_end=240];

    "src/auth.py::AuthManager" [type="class", file="src/auth.py",
                                 line_start=50, line_end=180,
                                 signature="class AuthManager"];

    "src/auth.py::AuthManager::__init__" [type="function", file="src/auth.py",
                                           line_start=52, line_end=61,
                                           signature="__init__(self, secret)"];

    "src/auth.py::AuthManager::verify_token" [type="function", file="src/auth.py",
                                               line_start=117, line_end=126,
                                               signature="verify_token(self, token)"];

    "src/auth.py" -> "src/auth.py::AuthManager" [label="contains"];
    "src/auth.py::AuthManager" -> "src/auth.py::AuthManager::__init__" [label="contains"];
    "src/auth.py::AuthManager" -> "src/auth.py::AuthManager::verify_token" [label="contains"];
    "src/auth.py" -> "src/database.py" [label="imports"];
    "src/auth.py::AuthManager::verify_token" -> "src/database.py::SessionStore::create" [label="calls"];
    // --- end: src/auth.py ---

    // --- file: src/database.py ---
    "src/database.py" [type="file", line_start=1, line_end=310];

    "src/database.py::SessionStore" [type="class", file="src/database.py",
                                      line_start=20, line_end=150,
                                      signature="class SessionStore"];

    "src/database.py::SessionStore::create" [type="function", file="src/database.py",
                                              line_start=92, line_end=110,
                                              signature="create(self, payload)"];

    "src/database.py" -> "src/database.py::SessionStore" [label="contains"];
    "src/database.py::SessionStore" -> "src/database.py::SessionStore::create" [label="contains"];
    // --- end: src/database.py ---

}


Node IDs use :: as separator to avoid ambiguity with file paths containing dots or slashes.

The Multi-Edit Workflow (Complete)

Bug report: auth.py has 3 functions that need fixing

1. Model identifies edits needed:
   - verify_token  lines 117-126  (fix null check)
   - login         lines 63-89    (fix session handling)
   - __init__      lines 52-61    (fix secret validation)

2. Sort descending by line_start:
   [verify_token:117, login:63, __init__:52]

3. Apply edits bottom-up:
   a. Replace lines 117-126 with new verify_token
      → lines 52-116 are untouched, line numbers valid
   b. Replace lines 63-89 with new login
      → lines 52-62 are untouched, line numbers valid
   c. Replace lines 52-61 with new __init__

4. One tree-sitter reparse of auth.py
   → fresh absolute line numbers for all nodes

5. dot_updater replaces // --- file: auth.py --- section
   → graph.dot updated atomically

6. git add auth.py .residuality/graph.dot
   git commit "Fixed null check, session handling, secret validation in auth.py"

7. Qdrant async: index updated node signatures


Context sent to the model for each function: ~10-40 lines. Never the whole file.

Docker Build for dot_updater

The C binary is compiled during the Docker image build — no runtime dependencies:

FROM python:3.11-slim AS base

# Install tree-sitter CLI and build tools
RUN apt-get update && apt-get install -y \
    gcc make curl git \
    && curl -L https://github.com/tree-sitter/tree-sitter/releases/latest/download/tree-sitter-linux-x64.gz \
    | gunzip > /usr/local/bin/tree-sitter \
    && chmod +x /usr/local/bin/tree-sitter

WORKDIR /app

# Build dot_updater C binary
COPY dot_updater.c cJSON.c cJSON.h ./
RUN gcc -O2 -o dot_updater dot_updater.c cJSON.c \
    && mv dot_updater /usr/local/bin/dot_updater

# Install Python dependencies
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Git config for commits
RUN git config --global user.email "residuality@local" \
    && git config --global user.name "Residuality"

VOLUME /repos
CMD ["python", "app.py"]




Key Design Decisions

Why git instead of a custom version store? Git already solves the hard problems: efficient diffing, merge tracking, parent relationships, branch management. Gitpython gives clean Python access. No need to reinvent.

Why Qdrant for commit messages instead of just git log? Git log is keyword search only. Qdrant enables semantic search: "find when the caching was working" finds commits even if they don't contain the word "working" or "cache" explicitly.

Why Flask instead of React? Lower complexity, easier to maintain, runs anywhere Python runs, no build step, your models can generate Jinja2 templates more reliably than React components.

Why keep Open WebUI? The semantic router, tool calling, model connections, and auth are all already built and working. Residuality calls Open WebUI as a backend service — it doesn't replace it.

Why a separate Qdrant collection? The existing Semantic collection stores conversation context (summaries, ToC, messages). The residuality_commits collection stores artifact version history. The residuality_graph collection stores semantic embeddings of function signatures and docstrings. Each has a different schema and different search patterns — keeping them separate avoids query pollution.

Why tree-sitter CLI instead of Python's ast module? tree-sitter is a compiled Rust binary — it parses files in microseconds, handles syntax errors gracefully, and is language-agnostic. Adding JavaScript, Rust, or Go support later means only swapping the grammar and query file. Python's ast module is Python-only and fails on broken code.

Why a C binary for dot_updater instead of Python? The dot file update is pure string manipulation and file I/O — exactly what C is fastest at. The binary compiles to a single executable with no runtime dependencies, ships inside the Docker image, and runs in microseconds. The logic is simple enough that C is not burdensome. Python stays for everything that benefits from Python: Flask routing, Qdrant client, gitpython, async tasks.

Why sort edits descending by line number before applying? Editing a file from the bottom up means each edit doesn't shift the line numbers of subsequent edits. Three edits at lines 234, 117, and 45 can be applied independently in that order — each one sees the line numbers it was given without any offset correction needed. One tree-sitter reparse at the end recomputes all absolute positions correctly.

Why store the code structure graph as a dot file in git rather than a database? The dot file is versioned alongside the code for free — checkout any commit and get the exact graph that reflected the codebase at that moment. Diffs are human-readable. Graphviz renders it directly. No sync issues between a separate database and git. Traversal via pydot is fast enough for any realistic codebase. Qdrant handles the one thing dot files can't do: semantic search over node content.

Why is git remote support optional rather than required? Residuality's core value is local AI-assisted artifact management — git is an internal record-keeping mechanism, not a collaboration tool. Most workflows never need a remote. But for projects that are also real codebases, optionally linking to GitHub or GitLab costs nothing and lets Residuality coexist with existing development workflows. SSH keys and tokens are mounted/injected at the Docker level — Residuality itself never stores credentials.



The Name

Residuality works on multiple levels:





Geological layers → artifact versions through time



Archaeological excavation → semantic search through history



Stratigraphy → the science of reading history from layers



"Residuality" implies what survives — the stable state a system returns to

Alternative names considered:





Stratum (singular, more precise)



Excavate (verb, action-oriented)



Dig (simple, memorable)



Sediment (too passive)



Fossil (too dead)

Residuality wins because it works on every level: the residue in a dig site, the attractor a chaotic system returns to, the error term that reveals model fit, the stress that remains in a material after loading. It is what persists. It is the truth underneath.
