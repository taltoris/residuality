"""
owui_client.py — Open WebUI API Client
Sends chat completions through Open WebUI (which runs the semantic router).
"""

import os
import json
import logging
import requests
from typing import Optional

logger = logging.getLogger(__name__)

OWUI_URL         = os.getenv("OWUI_URL",          "http://192.168.0.100:8282")
OWUI_API_KEY     = os.getenv("OWUI_API_KEY",      "")
DEFAULT_MODEL    = os.getenv("OWUI_MODEL",         "/models/diffusiongemma")
SUMMARIZER_MODEL = os.getenv("SUMMARIZER_MODEL",   "/models/diffusiongemma")
PLANNER_MODEL    = os.getenv("PLANNER_MODEL",      "Qwen3.5-9B-Q4_0.gguf")


class OWUIClient:

    def __init__(self):
        self.base_url         = OWUI_URL.rstrip("/")
        self.api_key          = OWUI_API_KEY
        self.SUMMARIZER_MODEL = SUMMARIZER_MODEL
        self.PLANNER_MODEL    = PLANNER_MODEL
        self.headers          = {"Content-Type": "application/json"}
        if self.api_key:
            self.headers["Authorization"] = f"Bearer {self.api_key}"

    def chat(
        self,
        messages: list,
        model: str = DEFAULT_MODEL,
        stream: bool = False,
        temperature: Optional[float] = None,
    ) -> str:
        """
        Send messages to Open WebUI and return assistant response text.
        Includes chat_id to bypass Open WebUI v0.9.5 NoneType bug.
        """
        payload = {
            "model":    model,
            "messages": messages,
            "stream":   stream,
            "chat_id":  "residuality_api",  # required for OWUI v0.9.5
        }
        if temperature is not None:
            payload["temperature"] = temperature

        logger.info(f"OWUI chat: model={model} url={self.base_url} auth={'yes' if self.api_key else 'NO'}")
        logger.info(f"OWUI payload: {json.dumps(payload)[:500]}")

        try:
            r = requests.post(
                f"{self.base_url}/api/chat/completions",
                json=payload,
                headers=self.headers,
            )
            r.raise_for_status()
            raw = r.text.strip()
            if not raw:
                raise ValueError("Empty response from OWUI")

            logger.info(f"OWUI raw response: {raw[:400]}")

            # Handle streaming response (SSE format)
            if raw.startswith("data:"):
                content_parts = []
                for line in raw.splitlines():
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                        delta = chunk["choices"][0].get("delta", {})
                        if delta.get("content"):
                            content_parts.append(delta["content"])
                    except Exception:
                        continue
                return "".join(content_parts) or "(no response)"

            # Non-streaming JSON response
            data    = json.loads(raw)
            message = data["choices"][0]["message"]
            content = message.get("content")
            if not content:
                content = message.get("reasoning_content") or "(no response)"
            return content

        except requests.exceptions.HTTPError as e:
            logger.error(f"OWUI HTTP error: {e}, response: {r.text[:1000]}")
            raise
        except requests.exceptions.Timeout:
            logger.error("OWUI request timed out")
            raise
        except Exception as e:
            logger.error(f"OWUI chat failed: {e}")
            raise

    def generate_commit_message(self, diff: str) -> str:
        """Generate a concise commit message from a diff using the summarizer model."""
        if not diff.strip():
            return "Update files"

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
            result = self.chat(messages, model=self.SUMMARIZER_MODEL)
            result = result.strip().strip('"\'').split('\n')[0]
            result = result.removeprefix("commit: ").removeprefix("Commit: ")
            return result[:72] or "Update files"
        except Exception as e:
            logger.warning(f"Commit message generation failed: {e}")
            return "Update files"

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

    def health_check(self) -> bool:
        """Check if Open WebUI is reachable."""
        try:
            r = requests.get(f"{self.base_url}/health", timeout=5)
            return r.status_code == 200
        except Exception:
            return False
            
    def generate_plan(
        self,
        project_id: str,
        snapshot: Optional[str],
        graph_summary: Optional[str],
        recent_commits: Optional[list] = None,
    ) -> dict:
        """Ask the planner model to assess project state and suggest next steps."""
        import json, re
    
        context_parts = []
        if snapshot:
            context_parts.append(f"## Current Project State\n{snapshot}")
        if graph_summary:
            context_parts.append(f"## Codebase Structure\n{graph_summary}")
        if recent_commits:
            commit_lines = "\n".join([
                f"- [{c['commit_hash'][:8]}] {c['message']}"
                for c in recent_commits[:5]
            ])
            context_parts.append(f"## Recent Commits\n{commit_lines}")
    
        prompt = (
            "\n\n".join(context_parts) +
            "\n\n## Task\n"
            "Based on the project state above, suggest what to work on next.\n"
            "Return ONLY raw JSON:\n"
            "{\n"
            '  "assessment": "1-2 sentence summary of where things stand",\n'
            '  "next_task": "specific thing to work on next",\n'
            '  "approach": "how to tackle it",\n'
            '  "files_to_touch": ["file1.py", "file2.py"],\n'
            '  "risks": "anything that could go wrong",\n'
            '  "open_questions": ["question 1", "question 2"]\n'
            "}"
        )
    
        try:
            response = self.chat(
                messages=[{"role": "user", "content": prompt}],
                model=self.PLANNER_MODEL,
            )
            clean = re.sub(r'```json|```', '', response).strip()
            # Strip thinking block if present
            if '<think>' in clean:
                clean = re.sub(r'<think>.*?</think>', '', clean, flags=re.DOTALL).strip()
            return json.loads(clean)
        except Exception as e:
            logger.warning(f"Plan generation failed: {e}")
            return {
                "assessment": "Could not generate plan",
                "next_task": "Review project state manually",
                "approach": "",
                "files_to_touch": [],
                "risks": str(e),
                "open_questions": [],
            }
