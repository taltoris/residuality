"""
snapshot.py — Episodic Snapshot Generator
Captures state-of-affairs at commit time for long-term Qdrant memory.

Code projects:  captures current task, key decisions, open questions
Fiction projects: captures what reader knows, open threads, tone
"""

import os
import uuid
import logging
import requests
from pathlib import Path
from typing import Optional

import owui_client

logger = logging.getLogger(__name__)

QDRANT_URL        = os.getenv("QDRANT_URL",  "http://192.168.0.100:6333")
COLLECTION_PREFIX = os.getenv("QDRANT_COLLECTION_PREFIX", "res")

# The embedding call itself lives in owui_client.py, shared with indexer.py. It
# used to live here as well, along with a hardcoded VECTOR_DIM — which is how a
# 768-wide collection got handed 1024-wide vectors. The width now comes off the
# vector that comes back.


def snapshots_collection(project_id: str) -> str:
    return f"{COLLECTION_PREFIX}_{project_id}_snapshots"


# ── Qdrant helpers ────────────────────────────────────────────────────────

def _upsert(collection: str, points: list, dim: int) -> bool:
    """Write points, having first ensured the collection exists at `dim`."""
    if not _ensure_collection(collection, dim):
        return False
    try:
        r = requests.put(
            f"{QDRANT_URL}/collections/{collection}/points",
            json={"points": points},
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        return r.status_code == 200
    except Exception as e:
        logger.warning(f"Qdrant upsert failed: {e}")
        return False


def _ensure_collection(name: str, dim: int) -> bool:
    """Ensure a snapshot collection exists at `dim` dimensions.

    Same rule as the indexer's copy of this function: the width is passed in
    from the vector that was just embedded, never assumed, and an existing
    collection of a different width is refused with a message that names the
    fix rather than being written to.
    """
    try:
        r = requests.get(f"{QDRANT_URL}/collections/{name}", timeout=10)
        if r.status_code == 404:
            requests.put(
                f"{QDRANT_URL}/collections/{name}",
                json={"vectors": {"dense": {"size": dim, "distance": "Cosine"}}},
                headers={"Content-Type": "application/json"},
                timeout=15,
            )
            logger.info(f"Created collection: {name} ({dim} dimensions)")
            return True
        if r.status_code != 200:
            logger.warning(f"Collection check for {name} returned {r.status_code}")
            return False

        vectors = (r.json().get("result", {}).get("config", {})
                   .get("params", {}).get("vectors", {}))
        existing = (vectors.get("dense") or {}).get("size")
        if existing and existing != dim:
            logger.error(
                f"Collection {name} is {existing}-dimensional but the embedding "
                f"model returns {dim}. Delete the collection to rebuild it at "
                f"{dim} — its vectors came from a different model and cannot be "
                f"reused."
            )
            return False
        return True
    except Exception as e:
        logger.warning(f"Collection check failed for {name}: {e}")
        return False


def ensure_collection() -> None:
    """No-op — snapshot collections are created lazily per project, on first
    write, at the width of the vector that write is actually carrying."""
    pass


# ── Project type detection ─────────────────────────────────────────────────

def detect_project_type(repo_path: Path) -> str:
    """
    Returns 'fiction' if majority of files are .md prose,
    'code' if majority are source code files.
    """
    code_exts  = {'.py', '.js', '.ts', '.cpp', '.c', '.h', '.rs', '.go', '.java'}
    prose_exts = {'.md', '.txt', '.rst'}

    code_count  = 0
    prose_count = 0

    for f in repo_path.rglob("*"):
        if f.is_file() and not any(p.startswith('.') for p in f.parts):
            if f.suffix in code_exts:
                code_count += 1
            elif f.suffix in prose_exts:
                prose_count += 1

    return 'fiction' if prose_count > code_count else 'code'


# ── Snapshot generation ────────────────────────────────────────────────────

CODE_SNAPSHOT_PROMPT = """You are analyzing a software project at a commit point.
Based on the commit message and recent context, generate a state-of-affairs snapshot.

Commit message: {commit_message}
Recent summary: {summary}

Return ONLY raw JSON — no prose, no markdown fences, and do NOT ask any
clarifying questions or request more context. Answer from the commit message
and summary alone; if information is missing, state your best inference in the
relevant field rather than asking for it.
{{
  "state": "1-2 sentence description of where the project stands right now",
  "current_task": "what is being worked on",
  "key_decisions": ["decision 1", "decision 2"],
  "open_questions": ["question 1", "question 2"],
  "working": ["what is confirmed working"],
  "broken": ["what is known broken or incomplete"]
}}"""

FICTION_SNAPSHOT_PROMPT = """You are analyzing a fiction project at a section commit point.
Based on the commit message and recent context, generate a narrative state snapshot.

Commit message: {commit_message}
Recent summary: {summary}

Return ONLY raw JSON — no prose, no markdown fences, and do NOT ask any
clarifying questions or request more context. Answer from the commit message
and summary alone; if information is missing, state your best inference in the
relevant field rather than asking for it.
{{
  "state": "1-2 sentence description of where the story stands",
  "reader_knows": ["key facts the reader now knows"],
  "reader_doesnt_know": ["key things still hidden from reader"],
  "open_threads": ["unresolved plot threads"],
  "foreshadowed": ["things hinted at but not resolved"],
  "tone": "current emotional/tonal state of the narrative",
  "last_hook": "the hook or revelation that ended the last section"
}}"""


# A reply that is not a snapshot: models that ask clarifying questions instead
# of answering (some chat-tuned models do this) return an object shaped like
# {"questions": [...]}. It parses as valid JSON but has no state to store, so it
# must be rejected rather than stored as an empty snapshot.
_QUESTION_KEYS = {"questions", "question", "clarify", "clarification", "ask_user"}


def _looks_like_question(data) -> bool:
    if isinstance(data, list):
        return any(_looks_like_question(d) for d in data)
    if not isinstance(data, dict):
        return False
    if _QUESTION_KEYS & set(data.keys()):
        return True
    return any(_looks_like_question(v) for v in data.values())


def generate_snapshot(
    project_id: str,
    commit_hash: str,
    commit_message: str,
    summary: Optional[str],
    project_type: str,
    owui_client,
    summarizer_model: str,
) -> Optional[dict]:
    """Generate a snapshot using the given model.

    Returns a dict with at least a non-empty ``state`` key, or None when the
    model is unreachable, returns unparseable text, or answers with a
    clarifying question instead of a snapshot. Callers treat None as "no
    snapshot yet", not as a crash.
    """
    import json, re

    prompt_template = (
        FICTION_SNAPSHOT_PROMPT if project_type == 'fiction'
        else CODE_SNAPSHOT_PROMPT
    )
    prompt = prompt_template.format(
        commit_message=commit_message,
        summary=summary or "No previous summary."
    )

    try:
        response = owui_client.chat(
            messages=[{"role": "user", "content": prompt}],
            model=summarizer_model,
        )
        # Strip markdown fences
        clean = re.sub(r'```json|```', '', response).strip()
        data  = json.loads(clean)
    except Exception as e:
        logger.warning(f"Snapshot generation failed: {e}")
        return None

    if not isinstance(data, dict) or not str(data.get("state", "")).strip():
        logger.warning(
            f"Snapshot model returned no usable 'state' field "
            f"(model={summarizer_model}); got: {str(data)[:200]!r}"
        )
        return None
    if _looks_like_question(data):
        logger.warning(
            f"Snapshot model asked a clarifying question instead of answering "
            f"(model={summarizer_model}); refusing to store it. Point the default "
            f"model at one that follows the 'return ONLY raw JSON' instruction."
        )
        return None

    data["project_type"]   = project_type
    data["commit_hash"]    = commit_hash
    data["commit_message"] = commit_message
    data["project_id"]     = project_id
    return data


def store_snapshot(
    project_id: str,
    commit_hash: str,
    commit_message: str,
    snapshot: dict,
    turn: int,
    timestamp: int,
) -> bool:
    """Store a snapshot in Qdrant.

    Returns True on success, or a short reason string on failure so the caller
    can say *which* stage broke — an embedding-service outage and a Qdrant
    upsert error are different fixes, and collapsing both into False is what
    made the UI show a generic 'may be down' with no way to tell them apart.

    The failure string is the embedder's own sentence rather than a
    paraphrase. It used to read "embedding service unreachable", which sent
    two rounds of diagnosis after the wrong thing: the endpoint was up and
    answering, and what it was saying was that the request named no model.
    """
    # Embed the state description as the semantic anchor
    state_text = snapshot.get("state", commit_message)
    vector = owui_client.embed(state_text)
    if not vector:
        return f"could not vectorize the snapshot — {owui_client.embed_last_error()}"

    point_id = str(uuid.uuid5(
        uuid.NAMESPACE_DNS,
        f"snapshot-{project_id}-{commit_hash}"
    ))

    ok = _upsert(snapshots_collection(project_id), [{
        "id":     point_id,
        "vector": {"dense": vector},
        "payload": {
            "project_id":     project_id,
            "commit_hash":    commit_hash,
            "commit_message": commit_message,
            "turn":           turn,
            "timestamp":      timestamp,
            **snapshot,
        }
    }], len(vector))
    return True if ok else f"Qdrant upsert failed for collection {snapshots_collection(project_id)}"


def _snapshot_from_payload(payload: dict, score: Optional[float] = None) -> dict:
    """One stored snapshot in the shape the rest of the app expects.

    Both readers — semantic search, and the latest-snapshot lookup the Plan
    page assesses with — come through here, so a field cannot exist on one
    path and be missing on the other.
    """
    return {
        "commit_hash":    payload.get("commit_hash", ""),
        "commit_message": payload.get("commit_message", ""),
        "state":          payload.get("state", ""),
        "project_type":   payload.get("project_type", "code"),
        "timestamp":      payload.get("timestamp", 0),
        "score":          round(score, 3) if score is not None else None,
        # Code fields
        "current_task":   payload.get("current_task"),
        "key_decisions":  payload.get("key_decisions", []),
        "open_questions": payload.get("open_questions", []),
        "working":        payload.get("working", []),
        "broken":         payload.get("broken", []),
        # Fiction fields
        "reader_knows":        payload.get("reader_knows", []),
        "reader_doesnt_know":  payload.get("reader_doesnt_know", []),
        "open_threads":        payload.get("open_threads", []),
        "foreshadowed":        payload.get("foreshadowed", []),
        "tone":                payload.get("tone"),
        "last_hook":           payload.get("last_hook"),
    }


def search_snapshots(
    query: str,
    project_id: Optional[str] = None,
    limit: int = 5,
) -> list:
    """Semantic search over project snapshots."""
    vector = owui_client.embed(query)
    if not vector:
        logger.warning(f"Snapshot search unavailable: {owui_client.embed_last_error()}")
        return []

    payload = {
        "vector": {"name": "dense", "vector": vector},
        "limit":  limit,
        "with_payload": True,
    }

    try:
        r = requests.post(
            f"{QDRANT_URL}/collections/{snapshots_collection(project_id or 'default')}/points/search",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        results = r.json().get("result", [])
        return [_snapshot_from_payload(p.get("payload", {}), p.get("score"))
                for p in results]
    except Exception as e:
        logger.warning(f"Snapshot search failed: {e}")
        return []


def latest_snapshot(project_id: str) -> Optional[dict]:
    """The most recent snapshot stored for a project, or None.

    Scrolled and sorted on the stored timestamp rather than searched: "what
    happened last" is an ordering question, and a semantic query could return a
    very *relevant* old snapshot as its nearest neighbour. A project stores one
    snapshot per commit, so reading the payloads and sorting them is both
    simpler and correct.

    None is a normal answer — a project with nothing indexed yet — so callers
    treat it as "no snapshot", not as a failure.
    """
    try:
        r = requests.post(
            f"{QDRANT_URL}/collections/{snapshots_collection(project_id)}/points/scroll",
            json={"limit": 200, "with_payload": True, "with_vector": False},
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        points = r.json().get("result", {}).get("points", [])
    except Exception as e:
        logger.warning(f"Snapshot scroll failed: {e}")
        return None

    if not points:
        return None
    payloads = [p.get("payload", {}) for p in points]
    payloads.sort(key=lambda p: p.get("timestamp", 0), reverse=True)
    return _snapshot_from_payload(payloads[0])


def format_snapshot_for_context(snapshot: dict) -> str:
    """Format a snapshot for injection into the model context."""
    lines = [f"### Snapshot at commit {snapshot['commit_hash'][:8]}"]
    lines.append(f"**State:** {snapshot['state']}")

    if snapshot["project_type"] == "fiction":
        if snapshot.get("reader_knows"):
            lines.append("**Reader knows:** " + "; ".join(snapshot["reader_knows"]))
        if snapshot.get("open_threads"):
            lines.append("**Open threads:** " + "; ".join(snapshot["open_threads"]))
        if snapshot.get("last_hook"):
            lines.append(f"**Last hook:** {snapshot['last_hook']}")
        if snapshot.get("tone"):
            lines.append(f"**Tone:** {snapshot['tone']}")
    else:
        if snapshot.get("current_task"):
            lines.append(f"**Current task:** {snapshot['current_task']}")
        if snapshot.get("key_decisions"):
            lines.append("**Decisions:** " + "; ".join(snapshot["key_decisions"]))
        if snapshot.get("working"):
            lines.append("**Working:** " + "; ".join(snapshot["working"]))
        if snapshot.get("broken"):
            lines.append("**Broken:** " + "; ".join(snapshot["broken"]))

    return "\n".join(lines)
