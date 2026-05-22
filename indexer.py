"""
indexer.py — Qdrant indexer for Residuality
Indexes commit messages and graph node signatures for semantic search.
"""

import os
import uuid
import logging
import requests
from typing import Optional

logger = logging.getLogger(__name__)

QDRANT_URL              = os.getenv("QDRANT_URL",               "http://192.168.0.100:6333")
EMBED_URL               = os.getenv("EMBED_URL",                "http://192.168.0.100:8090")
COMMITS_COLLECTION      = os.getenv("QDRANT_COMMITS_COLLECTION", "residuality_commits")
GRAPH_COLLECTION        = os.getenv("QDRANT_GRAPH_COLLECTION",   "residuality_graph")
VECTOR_DIM              = 768


def _embed(text: str) -> Optional[list]:
    """Embed text via lcpp-embed (nomic-embed-text)."""
    try:
        r = requests.post(
            f"{EMBED_URL}/v1/embeddings",
            json={"input": text[:2048], "model": "nomic-embed-text"},
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        return r.json()["data"][0]["embedding"]
    except Exception as e:
        logger.warning(f"Embedding failed: {e}")
        return None


def _qdrant_put(collection: str, points: list) -> bool:
    """Upsert points into a Qdrant collection."""
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
                   filters: dict = None, limit: int = 10) -> list:
    """Semantic search in a Qdrant collection."""
    payload = {
        "vector": {"name": "dense", "vector": vector},
        "limit": limit,
        "with_payload": True,
    }
    if filters:
        payload["filter"] = filters
    try:
        r = requests.post(
            f"{QDRANT_URL}/collections/{collection}/points/search",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        return r.json().get("result", [])
    except Exception as e:
        logger.warning(f"Qdrant search failed: {e}")
        return []


def ensure_collections() -> None:
    """Create Qdrant collections if they don't exist."""
    schema = {
        "vectors": {
            "dense": {
                "size": VECTOR_DIM,
                "distance": "Cosine",
            }
        },
        "sparse_vectors": {},
    }
    for collection in [COMMITS_COLLECTION, GRAPH_COLLECTION]:
        try:
            r = requests.get(f"{QDRANT_URL}/collections/{collection}", timeout=10)
            if r.status_code == 404:
                requests.put(
                    f"{QDRANT_URL}/collections/{collection}",
                    json=schema,
                    headers={"Content-Type": "application/json"},
                    timeout=15,
                )
                logger.info(f"Created Qdrant collection: {collection}")
        except Exception as e:
            logger.warning(f"Collection check failed for {collection}: {e}")


def index_commit(commit_data: dict) -> bool:
    """
    Index a commit message in Qdrant for semantic search.
    commit_data keys: commit_hash, project_id, branch, parent_hashes,
                      artifact_path, message, timestamp, turn,
                      model_used, is_merge
    """
    message = commit_data.get("message", "")
    if not message:
        return False

    vector = _embed(message)
    if not vector:
        return False

    point_id = str(uuid.uuid5(
        uuid.NAMESPACE_DNS,
        f"commit-{commit_data['commit_hash']}"
    ))

    return _qdrant_put(COMMITS_COLLECTION, [{
        "id":      point_id,
        "vector":  {"dense": vector},
        "payload": {
            "commit_hash":   commit_data.get("commit_hash", ""),
            "project_id":    commit_data.get("project_id", ""),
            "branch":        commit_data.get("branch", "main"),
            "parent_hashes": commit_data.get("parent_hashes", []),
            "artifact_path": commit_data.get("artifact_path", ""),
            "message":       message,
            "timestamp":     commit_data.get("timestamp", 0),
            "turn":          commit_data.get("turn", 0),
            "model_used":    commit_data.get("model_used", ""),
            "is_merge":      commit_data.get("is_merge", False),
        }
    }])


def index_graph_node(node_data: dict) -> bool:
    """
    Index a code/prose graph node in Qdrant for semantic search.
    node_data keys: id, type, file, project_id, line_start, line_end,
                    signature (code) or label (prose), docstring
    """
    text = node_data.get("signature") or node_data.get("label") or node_data.get("id")
    if node_data.get("docstring"):
        text = f"{text}\n{node_data['docstring']}"
    if not text:
        return False

    vector = _embed(text)
    if not vector:
        return False

    point_id = str(uuid.uuid5(
        uuid.NAMESPACE_DNS,
        f"node-{node_data['project_id']}-{node_data['id']}"
    ))

    return _qdrant_put(GRAPH_COLLECTION, [{
        "id":      point_id,
        "vector":  {"dense": vector},
        "payload": {
            "node_id":    node_data.get("id", ""),
            "type":       node_data.get("type", ""),
            "file":       node_data.get("file", ""),
            "project_id": node_data.get("project_id", ""),
            "line_start": node_data.get("line_start", 0),
            "line_end":   node_data.get("line_end",   0),
            "signature":  node_data.get("signature",  ""),
            "label":      node_data.get("label",      ""),
            "docstring":  node_data.get("docstring",  ""),
        }
    }])


def search_commits(query: str, project_id: Optional[str] = None,
                   limit: int = 10) -> list:
    """Semantic search over commit messages."""
    vector = _embed(query)
    if not vector:
        return []

    filters = None
    if project_id:
        filters = {"must": [{"key": "project_id", "match": {"value": project_id}}]}

    results = _qdrant_search(COMMITS_COLLECTION, vector, filters, limit)
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


def search_graph_nodes(query: str, project_id: Optional[str] = None,
                       limit: int = 10) -> list:
    """Semantic search over code/prose graph nodes."""
    vector = _embed(query)
    if not vector:
        return []

    filters = None
    if project_id:
        filters = {"must": [{"key": "project_id", "match": {"value": project_id}}]}

    results = _qdrant_search(GRAPH_COLLECTION, vector, filters, limit)
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
