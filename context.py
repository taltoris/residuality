"""
context.py — Context Compression Pipeline
Builds compressed context for model calls.
Models never see full conversation history — only rolling summary + last exchange + current artifact.
"""

import os
import logging
from typing import Optional
from indexer import search_commits

logger = logging.getLogger(__name__)


class ContextBuilder:
    """
    Builds the messages array sent to the model for each turn.
    Implements the rolling summary + last exchange + current artifact pattern.
    """

    def build_chat_context(
        self,
        project_id: str,
        user_message: str,
        rolling_summary: Optional[str],
        last_exchange: Optional[dict],
        artifact_content: Optional[str] = None,
        artifact_path: Optional[str] = None,
        relevant_commits: Optional[list] = None,
    ) -> list:
        """
        Build the messages list to send to Open WebUI.

        Returns a list of {role, content} dicts.
        Structure:
          [system: context + artifact]
          [assistant: last response]  (if last_exchange present)
          [user: current message]
        """
        system_parts = []

        # Rolling summary
        if rolling_summary:
            system_parts.append(
                f"## Conversation Summary\n{rolling_summary}"
            )

        # Relevant historical commits (from Qdrant search)
        if relevant_commits:
            commit_lines = "\n".join([
                f"- [{c['commit_hash'][:8]}] {c['message']} (branch: {c['branch']})"
                for c in relevant_commits[:5]
            ])
            system_parts.append(
                f"## Relevant History\n{commit_lines}"
            )

        # Current artifact
        if artifact_content and artifact_path:
            system_parts.append(
                f"## Current Artifact: {artifact_path}\n```\n{artifact_content}\n```"
            )

        messages = []

        if system_parts:
            messages.append({
                "role": "system",
                "content": "\n\n".join(system_parts)
            })

        # Last exchange (compressed — just the previous turn)
        if last_exchange:
            if last_exchange.get("user"):
                messages.append({
                    "role": "user",
                    "content": last_exchange["user"]
                })
            if last_exchange.get("assistant"):
                messages.append({
                    "role": "assistant",
                    "content": last_exchange["assistant"]
                })

        # Current user message
        messages.append({
            "role": "user",
            "content": user_message
        })

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
        """
        Build context for a surgical node edit.
        Model sees only the target function/section + instruction.
        """
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
            "role": "system",
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
        """
        Build context for a model-assisted merge reconciliation.
        """
        system_parts = [
            "You are reconciling two divergent versions of an artifact. "
            "Produce a single coherent version that harmoniously combines the best of both."
        ]

        if rolling_summary:
            system_parts.append(f"## Project Context\n{rolling_summary}")

        messages = [{
            "role": "system",
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
                text_only = [
                    item for item in content
                    if item.get("type") != "image_url"
                ]
                text_parts = [
                    item.get("text", "") for item in text_only
                    if item.get("type") == "text"
                ]
                cleaned.append({**msg, "content": " ".join(text_parts)})
            else:
                cleaned.append(msg)
        return cleaned
