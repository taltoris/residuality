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

logger = logging.getLogger(__name__)

QDRANT_URL        = os.getenv("QDRANT_URL",  "http://192.168.0.100:6333")
EMBED_URL         = os.getenv("EMBED_URL",   "http://192.168.0.100:8090")
COLLECTION_PREFIX = os.getenv("QDRANT_COLLECTION_PREFIX", "res")
VECTOR_DIM        = 768


def snapshots_collection(project_id: str) -> str:
    return f"{COLLECTION_PREFIX}_{project_id}_snapshots"


# ── Qdrant helpers ────────────────────────────────────────────────────────

def _embed(text: str) -> Optional[list]:
    """Generate a vector embedding for the given text using the Nomic model.

    Args:
        text: The input string to embed (truncated to 2048 characters).

    Returns:
        A list of floats representing the embedding, or None if an error occurs.
    """
    try:
        # Truncate input to fit within token/character limits of the model API endpoint.
        truncated_text = text[:2048]

        r = requests.post(
            f"{EMBED_URL}/v1/embeddings",
            json={"input": truncated_text, "model": "nomic-embed-text"},
            headers={"Content-Type": "application/json"},
            timeout=15,  # Set timeout to prevent hanging on network issues.
        )
        # Extracts only the first result from potentially multiple inputs in batch
        # mode (though we send a single item here).
        return r.json()["data"][0]["embedding"]
    except Exception as e:
        logger.warning(f"Embed failed for text starting with '{text[:50]}...': {e}")
        return None


def _upsert(collection: str, points: list) -> bool:
    _ensure_collection(collection)
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


def _ensure_collection(name: str) -> None:
    try:
        r = requests.get(f"{QDRANT_URL}/collections/{name}", timeout=10)
        if r.status_code == 404:
            requests.put(
                f"{QDRANT_URL}/collections/{name}",
                json={"vectors": {"dense": {"size": VECTOR_DIM, "distance": "Cosine"}}},
                headers={"Content-Type": "application/json"},
                timeout=15,
            )
            logger.info(f"Created collection: {name}")
    except Exception as e:
        logger.warning(f"Collection check failed: {e}")


def ensure_collection() -> None:
    """No-op — snapshot collections are created lazily per project."""
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

Return ONLY raw JSON:
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

Return ONLY raw JSON:
{{
  "state": "1-2 sentence description of where the story stands",
  "reader_knows": ["key facts the reader now knows"],
  "reader_doesnt_know": ["key things still hidden from reader"],
  "open_threads": ["unresolved plot threads"],
  "foreshadowed": ["things hinted at but not resolved"],
  "tone": "current emotional/tonal state of the narrative",
  "last_hook": "the hook or revelation that ended the last section"
}}"""


def generate_snapshot(
    project_id: str,
    commit_hash: str,
    commit_message: str,
    summary: Optional[str],
    project_type: str,
    owui_client,
    summarizer_model: str,
) -> Optional[dict]:
    """Generate a snapshot using the summarizer model."""
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
        data["project_type"]   = project_type
        data["commit_hash"]    = commit_hash
        data["commit_message"] = commit_message
        data["project_id"]     = project_id
        return data
    except Exception as e:
        logger.warning(f"Snapshot generation failed: {e}")
        return None


def store_snapshot(
    project_id: str,
    commit_hash: str,
    commit_message: str,
    snapshot: dict,
    turn: int,
    timestamp: int,
) -> bool:
    """Store a snapshot in Qdrant."""
    # Embed the state description as the semantic anchor
    state_text = snapshot.get("state", commit_message)
    vector = _embed(state_text)
    if not vector:
        return False

    point_id = str(uuid.uuid5(
        uuid.NAMESPACE_DNS,
        f"snapshot-{project_id}-{commit_hash}"
    ))

    return _upsert(snapshots_collection(project_id), [{
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
    }])


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
    vector = _embed(query)
    if not vector:
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
