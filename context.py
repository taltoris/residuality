"""
context.py — Context Compression Pipeline
Builds compressed context for model calls.

Memory model:
  - Long term:      rolling summary + Qdrant episodic snapshots
  - Associative:    Qdrant commit/node search results
  - Working memory: last N full exchanges (immediate context)
  - Ground truth:   relevant code/prose node content
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)


class ContextBuilder:

    def build_chat_context(
        self,
        project_id: str,
        user_message: str,
        rolling_summary: Optional[str],
        last_exchange: Optional[dict],
        artifact_content: Optional[str] = None,
        artifact_path: Optional[str] = None,
        relevant_commits: Optional[list] = None,
        relevant_nodes: Optional[list] = None,
        relevant_snapshots: Optional[list] = None,
        recent_exchanges: Optional[list] = None,
        max_recent_exchanges: int = 3,
    ) -> list:
        """
        Build a stateless compressed context for each Open WebUI request.

        Memory model:
          - Long term:      rolling summary + Qdrant semantic search
          - Working memory: last N full exchanges (immediate context, full fidelity)
          - Ground truth:   relevant code/prose node (not the whole file)
        """
        system_parts = []

        # Rolling summary — long term memory
        if rolling_summary:
            system_parts.append(f"## Conversation Summary\n{rolling_summary}")

        # Episodic snapshots — state-of-affairs at key commit moments
        if relevant_snapshots:
            from snapshot import format_snapshot_for_context
            snap_lines = "\n\n".join([
                format_snapshot_for_context(s) for s in relevant_snapshots[:3]
            ])
            system_parts.append(f"## Relevant Project Snapshots\n{snap_lines}")

        # Relevant commit history from Qdrant
        if relevant_commits:
            commit_lines = "\n".join([
                f"- [{c['commit_hash'][:8]}] {c['message']} "
                f"(branch: {c['branch']}, score: {c['score']})"
                for c in relevant_commits[:5]
            ])
            system_parts.append(f"## Relevant Commit History\n{commit_lines}")

        # Relevant code/prose nodes from Qdrant
        if relevant_nodes:
            node_lines = "\n".join([
                f"- {n['node_id']} [{n['type']}] "
                f"{n['file']}:{n['line_start']}-{n['line_end']} (score: {n['score']})"
                for n in relevant_nodes[:5]
            ])
            system_parts.append(f"## Relevant Code Locations\n{node_lines}")

        # Current node/artifact — the relevant section, not the whole file
        if artifact_content and artifact_path:
            system_parts.append(
                f"## Current Context: {artifact_path}\n```\n{artifact_content}\n```"
            )

        messages = []
        if system_parts:
            messages.append({
                "role":    "system",
                "content": "\n\n".join(system_parts)
            })

        # Recent exchanges — immediate working memory, full fidelity
        # Use recent_exchanges if provided, fall back to last_exchange for compat
        exchanges = recent_exchanges or ([last_exchange] if last_exchange else [])
        exchanges = exchanges[-max_recent_exchanges:]
        for exchange in exchanges:
            if exchange.get("user"):
                messages.append({"role": "user",      "content": exchange["user"]})
            if exchange.get("assistant"):
                messages.append({"role": "assistant", "content": exchange["assistant"]})

        # Current message
        messages.append({"role": "user", "content": user_message})
        return messages

    def build_edit_context(
        self,
        node_content: str,
        node_id: str,
        instruction: str,
        rolling_summary: Optional[str] = None,
        file_path: Optional[str] = None,
        line_start: Optional[int] = None,
        line_end: Optional[int] = None,
    ) -> list:
        """Build context for a surgical node edit."""
        system_parts = [
            "You are editing a specific section of code or prose. "
            "Return ONLY the replacement content, no explanations, no markdown fences."
        ]

        if rolling_summary:
            system_parts.append(f"## Project Context\n{rolling_summary}")

        if file_path and line_start and line_end:
            system_parts.append(
                f"## Target\nFile: {file_path}, lines {line_start}–{line_end}\nNode: {node_id}"
            )

        messages = [{
            "role":    "system",
            "content": "\n\n".join(system_parts)
        }]

        messages.append({
            "role": "user",
            "content": (
                f"Here is the current content:\n\n```\n{node_content}\n```\n\n"
                f"Instruction: {instruction}\n\n"
                f"Return only the replacement content."
            )
        })

        return messages

    def build_merge_context(
        self,
        content_a: str,
        content_b: str,
        diff: str,
        instruction: str,
        rolling_summary: Optional[str] = None,
    ) -> list:
        """Build context for a model-assisted merge reconciliation."""
        system_parts = [
            "You are reconciling two divergent versions of an artifact. "
            "Produce a single coherent version that harmoniously combines the best of both."
        ]

        if rolling_summary:
            system_parts.append(f"## Project Context\n{rolling_summary}")

        messages = [{
            "role":    "system",
            "content": "\n\n".join(system_parts)
        }]

        messages.append({
            "role": "user",
            "content": (
                f"## Version A\n```\n{content_a}\n```\n\n"
                f"## Version B\n```\n{content_b}\n```\n\n"
                f"## Diff\n```\n{diff}\n```\n\n"
                f"## Instruction\n{instruction}\n\n"
                f"Produce the reconciled version:"
            )
        })

        return messages

    def strip_images(self, messages: list) -> list:
        """Remove image_url items from message content (for text-only models)."""
        cleaned = []
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, list):
                text_parts = [
                    item.get("text", "") for item in content
                    if item.get("type") == "text"
                ]
                cleaned.append({**msg, "content": " ".join(text_parts)})
            else:
                cleaned.append(msg)
        return cleaned
