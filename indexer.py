"""
indexer.py — Qdrant indexer for Residuality
Per-project collections: res_{project_id}_commits, res_{project_id}_graph
"""

import os
import uuid
import logging
import requests
from typing import Optional

logger = logging.getLogger(__name__)

QDRANT_URL        = os.getenv("QDRANT_URL",  "http://192.168.0.100:6333")
EMBED_URL         = os.getenv("EMBED_URL",   "http://192.168.0.100:8090")
COLLECTION_PREFIX = os.getenv("QDRANT_COLLECTION_PREFIX", "res")
VECTOR_DIM        = 768


def commits_collection(project_id: str) -> str:
    return f"{COLLECTION_PREFIX}_{project_id}_commits"

def graph_collection(project_id: str) -> str:
    return f"{COLLECTION_PREFIX}_{project_id}_graph"


def _embed(text: str) -> Optional[list]:
    try:
        r = requests.post(
            f"{EMBED_URL}/v1/embeddings",
            json={"input": text[:2048], "model": "nomic-embed-text"},
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        return r.json()["data"][0]["embedding"]
    except Exception as e:
        logger.warning(f"Embed failed: {e}")
        return None


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
        logger.warning(f"Collection check failed for {name}: {e}")


def _qdrant_put(collection: str, points: list) -> bool:
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


def _qdrant_search(collection: str, vector: list,
                   limit: int = 10) -> list:
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


def ensure_collections() -> None:
    """No-op — collections are created lazily per project."""
    pass


def index_commit(commit_data: dict) -> bool:
    """Index a commit message in the project's commits collection."""
    project_id = commit_data.get("project_id", "default")
    message    = commit_data.get("message", "")
    if not message:
        return False

    vector = _embed(message)
    if not vector:
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
    }])


def index_graph_node(node_data: dict) -> bool:
    """Index a graph node in the project's graph collection."""
    project_id = node_data.get("project_id", "default")
    text = (node_data.get("signature") or node_data.get("label")
            or node_data.get("id", ""))
    if node_data.get("docstring"):
        text = f"{text}\n{node_data['docstring']}"
    if not text:
        return False

    vector = _embed(text)
    if not vector:
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
    }])


def search_commits(query: str, project_id: str = "default",
                   limit: int = 10) -> list:
    """Semantic search over a project's commit messages."""
    vector = _embed(query)
    if not vector:
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
    """Semantic search over a project's code/prose graph nodes."""
    vector = _embed(query)
    if not vector:
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
