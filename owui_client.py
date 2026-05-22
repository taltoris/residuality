"""
owui_client.py — Open WebUI API Client
Sends chat completions through Open WebUI (which runs the semantic router).
"""

import os
import logging
import requests
from typing import Optional

logger = logging.getLogger(__name__)

OWUI_URL        = os.getenv("OWUI_URL",          "http://192.168.0.100:8282")
OWUI_API_KEY    = os.getenv("OWUI_API_KEY",      "")
DEFAULT_MODEL   = os.getenv("OWUI_MODEL",        "semantic-router")
SUMMARIZER_MODEL = os.getenv("SUMMARIZER_MODEL", "bonsai")


class OWUIClient:

    def __init__(self):
        self.base_url = OWUI_URL.rstrip("/")
        self.api_key  = OWUI_API_KEY
        self.headers  = {
            "Content-Type":  "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

    def chat(
        self,
        messages: list,
        model: str = DEFAULT_MODEL,
        stream: bool = False,
        temperature: Optional[float] = None,
    ) -> str:
        """
        Send messages to Open WebUI and return assistant response text.
        The semantic router in OWUI picks the actual model.
        """
        payload = {
            "model":    model,
            "messages": messages,
            "stream":   stream,
        }
        if temperature is not None:
            payload["temperature"] = temperature

        try:
            r = requests.post(
                f"{self.base_url}/api/chat/completions",
                json=payload,
                headers=self.headers,
                timeout=120,
            )
            r.raise_for_status()
            data = r.json()
            return data["choices"][0]["message"]["content"]
        except requests.exceptions.Timeout:
            logger.error("OWUI request timed out")
            raise
        except requests.exceptions.HTTPError as e:
            logger.error(f"OWUI HTTP error: {e}, response: {r.text[:500]}")
            raise
        except Exception as e:
            logger.error(f"OWUI chat failed: {e}")
            raise

    def get_models(self) -> list:
        """Return list of available models from Open WebUI."""
        try:
            r = requests.get(
                f"{self.base_url}/api/models",
                headers=self.headers,
                timeout=15,
            )
            r.raise_for_status()
            return r.json().get("data", [])
        except Exception as e:
            logger.warning(f"Could not fetch models: {e}")
            return []

    def generate_commit_message(self, diff: str) -> str:
        """
        Generate a commit message from a diff using the summarizer model.
        Returns a concise one-line commit message.
        """
        if not diff.strip():
            return "Update files"

        # Truncate very large diffs
        diff_truncated = diff[:4000] + ("\n... (truncated)" if len(diff) > 4000 else "")

        messages = [{
            "role": "user",
            "content": (
                f"Write a concise git commit message (one line, under 72 characters) "
                f"that describes what changed in this diff. "
                f"Return ONLY the commit message, no explanation, no quotes.\n\n"
                f"Diff:\n{diff_truncated}"
            )
        }]

        try:
            result = self.chat(messages, model=SUMMARIZER_MODEL)
            # Clean up — strip quotes, newlines, prefixes like "commit: "
            result = result.strip().strip('"\'').split('\n')[0]
            result = result.removeprefix("commit: ").removeprefix("Commit: ")
            return result[:72] or "Update files"
        except Exception as e:
            logger.warning(f"Commit message generation failed: {e}")
            return "Update files"

    def health_check(self) -> bool:
        """Check if Open WebUI is reachable."""
        try:
            r = requests.get(f"{self.base_url}/health", timeout=5)
            return r.status_code == 200
        except Exception:
            return False
