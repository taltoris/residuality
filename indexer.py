"""
indexer.py — Qdrant indexer for Residuality

Provides per-project vector collections for commit messages and code graph nodes.
Collections are named `res_{project_id}_commits` and `res_{project_id}_graph`
and are created lazily on first write.

Public API:
    - index_commit(commit_data: dict) -> bool
    - index_graph_node(node_data: dict) -> bool
    - search_commits(query: str, project_id: str, limit: int) -> list[dict]
    - search_graph_nodes(query: str, project_id: str, limit: int) -> list[dict]
"""

import os
import uuid
import logging
import requests
from typing import Optional

import owui_client

logger = logging.getLogger(__name__)

# Configuration
QDRANT_URL        = os.getenv("QDRANT_URL",  "http://192.168.0.100:6333")
COLLECTION_PREFIX = os.getenv("QDRANT_COLLECTION_PREFIX", "res")

# Note on the embeddings: the endpoint, the model and the request shape live in
# owui_client.py, shared with snapshot.py. This module used to carry its own
# `_embed` — and with it a hardcoded VECTOR_DIM, which is how a collection ends
# up created at a width the model does not produce. The width is now read off
# the vector that comes back and passed to `_ensure_collection`.


# ---------------------------------------------------------------------------
# Collection name helpers
# ---------------------------------------------------------------------------

def commits_collection(project_id: str) -> str:
    """Return the Qdrant collection name for commit vectors of a project."""
    return f"{COLLECTION_PREFIX}_{project_id}_commits"


def graph_collection(project_id: str) -> str:
    """Return the Qdrant collection name for graph node vectors of a project."""
    return f"{COLLECTION_PREFIX}_{project_id}_graph"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _ensure_collection(name: str, dim: int) -> bool:
    """
    Ensure the named Qdrant collection exists at `dim` dimensions.

    The width is a parameter rather than a constant because it is a property of
    the embedding model, not of this file. That constant is exactly how the
    installer ended up with 768-wide collections (nomic-embed-text's width) and
    then handed them 1024-wide vectors (Qwen3-Embedding-0.6B's) — a mismatch
    that rejects every write with an error nowhere near the cause.

    An existing collection of a different width is refused, not written to:
    those vectors came from another model, so they cannot be compared against
    the new ones, and they cannot be re-widened in place. Deleting the
    collection is the fix, and the log says so.

    Uses a Cosine distance metric. Returns False (having said why) rather than
    raising, so a caller's point-write fails once and cleanly.
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


def _qdrant_put(collection: str, points: list, dim: int) -> bool:
    """
    Upsert one or more points into the specified collection.

    Ensures the collection exists at `dim` dimensions before writing, so a
    width mismatch stops the write here with a message that names it rather
    than surfacing later as a failed point.
    Returns True on HTTP 200, False otherwise.
    """
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


def _qdrant_search(collection: str, vector: list,
                   limit: int = 10) -> list:
    """
    Perform a vector similarity search in the specified collection.

    Returns the raw list of result objects from Qdrant, or an empty list
    on failure.
    """
    try:
        r = requests.post(
            f"{QDRANT_URL}/collections/{collection}/points/search",
            json={"vector": {"name": "dense", "vector": vector},
                  "limit": limit, "with_payload": True},
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        return r.json().get("result", [])
    except Exception as e:
        logger.warning(f"Qdrant search failed: {e}")
        return []


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def ensure_collections() -> None:
    """
    No-op. Collections are created lazily per project on first write.
    Retained for backward compatibility.
    """
    pass


def index_commit(commit_data: dict) -> bool:
    """
    Index a commit message in the project's commits collection.

    Args:
        commit_data: Dictionary with keys:
            - project_id: Project identifier (defaults to "default").
            - message: Commit message text (required).
            - commit_hash: Unique commit identifier.
            - branch: Branch name (defaults to "main").
            - parent_hashes: List of parent commit hashes.
            - artifact_path: Path to associated artifact, if any.
            - timestamp: Unix timestamp of the commit.
            - turn: Turn number in the conversation/session.
            - model_used: Name of the model that produced the commit.
            - is_merge: Boolean indicating if this is a merge commit.

    Returns:
        True if the point was successfully upserted, False otherwise.
    """
    project_id = commit_data.get("project_id", "default")
    message    = commit_data.get("message", "")
    if not message:
        return False

    vector = owui_client.embed(message)
    if not vector:
        logger.warning(f"Commit {commit_data.get('commit_hash', '?')[:8]} not indexed: "
                       f"{owui_client.embed_last_error()}")
        return False

    point_id = str(uuid.uuid5(uuid.NAMESPACE_DNS,
                               f"commit-{commit_data['commit_hash']}"))

    return _qdrant_put(commits_collection(project_id), [{
        "id":      point_id,
        "vector":  {"dense": vector},
        "payload": {
            "commit_hash":   commit_data.get("commit_hash", ""),
            "project_id":    project_id,
            "branch":        commit_data.get("branch", "main"),
            "parent_hashes": commit_data.get("parent_hashes", []),
            "artifact_path": commit_data.get("artifact_path", ""),
            "message":       message,
            "timestamp":     commit_data.get("timestamp", 0),
            "turn":          commit_data.get("turn", 0),
            "model_used":    commit_data.get("model_used", ""),
            "is_merge":      commit_data.get("is_merge", False),
        }
    }], len(vector))


def index_graph_node(node_data: dict) -> bool:
    """
    Index a graph node in the project's Qdrant graph collection.

    Embeds the node's signature/label and docstring into a dense vector,
    then upserts the point into the project-scoped graph collection.

    Args:
        node_data: Dictionary containing node metadata. Expected keys:
            - project_id: Project identifier (defaults to "default").
            - id: Unique node identifier.
            - signature: Function/method signature (preferred for embedding).
            - label: Human-readable label (fallback for embedding).
            - docstring: Documentation string (appended to embedding text).
            - type: Node type (e.g., "function", "class").
            - file: Source file path.
            - line_start: Starting line number.
            - line_end: Ending line number.

    Returns:
        True if the point was successfully upserted, False otherwise.
    """
    project_id = node_data.get("project_id", "default")
    text = (node_data.get("signature") or node_data.get("label")
            or node_data.get("id", ""))
    if node_data.get("docstring"):
        text = f"{text}\n{node_data['docstring']}"
    if not text:
        return False

    vector = owui_client.embed(text)
    if not vector:
        logger.warning(f"Graph node {node_data.get('id', '?')} not indexed: "
                       f"{owui_client.embed_last_error()}")
        return False

    point_id = str(uuid.uuid5(uuid.NAMESPACE_DNS,
                               f"node-{project_id}-{node_data['id']}"))

    return _qdrant_put(graph_collection(project_id), [{
        "id":      point_id,
        "vector":  {"dense": vector},
        "payload": {
            "node_id":    node_data.get("id", ""),
            "type":       node_data.get("type", ""),
            "file":       node_data.get("file", ""),
            "project_id": project_id,
            "line_start": node_data.get("line_start", 0),
            "line_end":   node_data.get("line_end",   0),
            "signature":  node_data.get("signature",  ""),
            "label":      node_data.get("label",      ""),
            "docstring":  node_data.get("docstring",  ""),
        }
    }], len(vector))


def search_commits(query: str, project_id: str = "default",
                   limit: int = 10) -> list:
    """
    Semantic search over a project's commit messages.

    Args:
        query: Natural language search query.
        project_id: Project identifier to search within.
        limit: Maximum number of results to return.

    Returns:
        List of dictionaries with commit metadata and similarity score.
    """
    vector = owui_client.embed(query)
    if not vector:
        logger.warning(f"Commit search unavailable: {owui_client.embed_last_error()}")
        return []
    results = _qdrant_search(commits_collection(project_id), vector, limit)
    return [
        {
            "commit_hash":   r["payload"].get("commit_hash"),
            "message":       r["payload"].get("message"),
            "project_id":    r["payload"].get("project_id"),
            "branch":        r["payload"].get("branch"),
            "artifact_path": r["payload"].get("artifact_path"),
            "timestamp":     r["payload"].get("timestamp"),
            "model_used":    r["payload"].get("model_used"),
            "is_merge":      r["payload"].get("is_merge"),
            "score":         round(r.get("score", 0.0), 3),
        }
        for r in results
    ]


def search_graph_nodes(query: str, project_id: str = "default",
                       limit: int = 10) -> list:
    """
    Semantic search over a project's code/prose graph nodes.

    Args:
        query: Natural language search query.
        project_id: Project identifier to search within.
        limit: Maximum number of results to return.

    Returns:
        List of dictionaries with node metadata and similarity score.
    """
    vector = owui_client.embed(query)
    if not vector:
        logger.warning(f"Graph search unavailable: {owui_client.embed_last_error()}")
        return []
    results = _qdrant_search(graph_collection(project_id), vector, limit)
    return [
        {
            "node_id":    r["payload"].get("node_id"),
            "type":       r["payload"].get("type"),
            "file":       r["payload"].get("file"),
            "line_start": r["payload"].get("line_start"),
            "line_end":   r["payload"].get("line_end"),
            "signature":  r["payload"].get("signature"),
            "label":      r["payload"].get("label"),
            "score":      round(r.get("score", 0.0), 3),
        }
        for r in results
    ]
