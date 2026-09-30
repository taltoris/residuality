"""
owui_client.py — Open WebUI API Client
Sends chat completions through Open WebUI (which runs the semantic router).

That same endpoint is where the model list comes from, so this module also owns
*model configuration* — which model each role uses, and where the app is pointed.

The roles, in the order they matter here:

  default     the one shared model, used by anything with no override
  chat        override for the "chat" slot        ("" = follow default)
  edit        override for AI-assisted edits     ("" = follow default)
  planner     the Plan tab: conversations and assessments
  summarizer  commit messages + rolling summaries

Note on `chat`: it is retained and still saved, but nothing reads it. The Plan
tab runs on `planner`, and `chat()` takes its model per call (falling back to
`default`), so `CHAT_MODEL` is assigned and never used. It is kept so an
existing config file does not silently lose a value on upgrade; retiring it is
a config migration, not an edit.
"""

import os
import io
import re
import json
import time
import logging
import configparser
from pathlib import Path
import requests
from typing import Optional

logger = logging.getLogger(__name__)

OWUI_URL         = os.getenv("OWUI_URL",          "http://192.168.0.100:8282")
OWUI_API_KEY     = os.getenv("OWUI_API_KEY",      "")
DEFAULT_MODEL    = os.getenv("OWUI_MODEL",         "/models/diffusiongemma")
SUMMARIZER_MODEL = os.getenv("SUMMARIZER_MODEL",   "/models/diffusiongemma")
PLANNER_MODEL    = os.getenv("PLANNER_MODEL",      "Qwen3.5-9B-Q4_0.gguf")

CONFIG_FILENAME  = "residuality_models.cfg"

# The model's context window, in tokens. This is the constraint an edit budget
# stands in for, so it is the thing worth configuring -- 262144 is what the
# routed model reports.
CONTEXT_TOKENS   = int(os.getenv("OWUI_CONTEXT_TOKENS", "262144"))

# Characters per token, roughly. A real tokenizer runs about 3.5 for code and 4
# for prose; this only has to be close, because it feeds a budget that is
# already half the window.
CHARS_PER_TOKEN  = 4

# A replacement comes back about as long as the range it replaces, and the model
# has to hold both at once -- so only half the window can be input, with the
# other half left for the reply (plus the system prompt and the instruction).
REPLY_SHARE      = 0.5


def chars_for_context(context_tokens: int) -> int:
    """A per-edit character budget for a model with this context window."""
    return int(context_tokens * CHARS_PER_TOKEN * REPLY_SHARE)


# What an install gets with nothing set, and the fallback when a setting is
# unusable. MAX_EDIT_NODE_CHARS still overrides it, and 0 still means no limit.
MAX_EDIT_CHARS   = chars_for_context(CONTEXT_TOKENS)

# The system prompt an AI-assisted edit starts from. It is a *setting* because
# how a given model wants to be asked for bare replacement text varies, but the
# default is the one that was always sent, so an unconfigured install behaves
# exactly as it did before there was a settings panel.
DEFAULT_EDIT_PROMPT = (
    "You are editing a specific section of code or prose. "
    "Return ONLY the replacement content, no explanations, no markdown fences."
)


# ── Config file ───────────────────────────────────────────────────────────

def config_path() -> Path:
    """Where settings saved in the UI live.

    Under REPOS_PATH, which is the one bind mount that outlives the container.
    The previous code wrote to Path(REPOS_PATH).parent — "/" inside the
    container, outside every mount — so a saved setting died with the
    container before it was ever read back.
    """
    return Path(os.getenv("REPOS_PATH", "/repos")) / CONFIG_FILENAME


def load_config() -> dict:
    """Current settings: env first, then anything the config file overrides.

    The file is layered *over* the environment rather than replacing it, so an
    install that never opens the settings page runs entirely off .env, and a
    key removed from the file falls back to its .env value instead of becoming
    empty.
    """
    cfg = {
        "default":      DEFAULT_MODEL,
        "chat":         "",          # "" = follow default
        "edit":         "",          # "" = follow default
        "planner":      PLANNER_MODEL,
        "summarizer":   SUMMARIZER_MODEL,
        "edit_prompt":  DEFAULT_EDIT_PROMPT,
        "context_tokens": str(CONTEXT_TOKENS),
        "max_edit_chars": os.getenv("MAX_EDIT_NODE_CHARS", ""),   # "" = from window
        "owui_url":     OWUI_URL,
        "owui_api_key": OWUI_API_KEY,
    }

    path = config_path()
    if not path.is_file():
        return cfg

    parser = configparser.ConfigParser()
    try:
        parser.read(path, encoding="utf-8")
    except Exception as e:
        logger.warning(f"Could not read {path}: {e} — using environment defaults")
        return cfg

    if not parser.has_section("models"):
        return cfg

    for key in cfg:
        value = parser["models"].get(key, "").strip()
        if value:
            cfg[key] = value
        elif key in ("chat", "edit"):
            # An empty override is meaningful, not missing: it means "follow
            # the default". Keep it empty rather than restoring the env value.
            cfg[key] = ""
    return cfg


def save_config(cfg: dict) -> Path:
    """Write settings to the config file. One writer for the whole app."""
    parser = configparser.ConfigParser()
    parser["models"] = {k: str(v) for k, v in cfg.items()}

    body = io.StringIO()
    parser.write(body)

    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# Written by the Residuality settings panel.\n"
        "# These values override the matching .env / environment variables;\n"
        "# blank means 'fall back to .env'.\n"
        + body.getvalue(),
        encoding="utf-8",
    )
    # This file can hold an API key.
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def resolve_model(cfg: dict, slot: str) -> str:
    """The model a role actually uses, after fallback to the shared default."""
    return cfg.get(slot) or cfg.get("default") or DEFAULT_MODEL


def resolve_max_edit_chars(cfg: dict) -> int:
    """Characters of a range to send in one edit. 0 (or less) means no limit.

    An explicit max_edit_chars wins; otherwise the budget is derived from the
    model's context window, which is what it actually stands in for. Bad values
    fall back rather than raising: this is read on the request path, and a typo
    in a settings box should not turn every edit into a 500.
    """
    raw = str(cfg.get("max_edit_chars") or "").strip()
    if raw:
        try:
            return int(raw)
        except (TypeError, ValueError):
            logger.warning(
                f"max_edit_chars is not a whole number: {raw!r} -- "
                f"deriving it from the context window"
            )

    raw_window = str(cfg.get("context_tokens") or CONTEXT_TOKENS).strip()
    try:
        window = int(raw_window)
    except (TypeError, ValueError):
        logger.warning(
            f"context_tokens is not a whole number: {raw_window!r} -- "
            f"using {CONTEXT_TOKENS}"
        )
        window = CONTEXT_TOKENS
    return chars_for_context(window)


def plan_edit_chunks(lines: list, line_start: int, line_end: int,
                     limit: int, node_ranges=()) -> list:
    """Split a line range into contiguous chunks that each fit `limit` chars.

    Returns inclusive 1-based `(start, end)` pairs that tile
    `[line_start, line_end]` exactly -- no gap, no overlap -- so joining one
    reply per chunk with newlines is a drop-in replacement for the whole range.
    The caller sends one model call per chunk and stitches the answers, which
    is how a range larger than the budget stays editable instead of being cut
    down to a prefix.

    Cuts go on the graph's node boundaries first, because a node begins and
    ends at a statement: a cut there keeps whole functions, where an arbitrary
    line number splits them. A gap between nodes that is itself too big is cut
    on blank lines, which is also the only seam there is when a file has no
    nodes recorded at all (prose, unparsed types). A single node larger than
    the budget is cut on lines -- the one case where a line boundary is all
    there is to go on.

    `limit <= 0` means no budget, so the whole range is one chunk, matching the
    "no limit" reading `resolve_max_edit_chars` gives.
    """
    n = line_end - line_start + 1
    if n <= 0:
        return []
    if limit <= 0:
        return [(line_start, line_end)]

    seg = lines[line_start - 1:line_end]

    def size(a: int, b: int) -> int:
        """Characters in offsets [a, b) once joined by newlines."""
        return sum(len(seg[i]) for i in range(a, b)) + max(b - a - 1, 0)

    def blank_seams(a: int, b: int) -> list:
        """Offsets in [a, b) a split may start on: the line after a blank run.

        The blank stays with the piece above, so a piece never opens on blank
        lines. These are consulted only inside a piece that has to be split,
        which keeps an ordinary node-sized edit from being cut on a blank line
        that happens to sit inside a function.
        """
        return [i + 1 for i in range(a, b - 1)
                if not seg[i].strip() and seg[i + 1].strip()]

    def split_lines(a: int, b: int) -> list:
        """Fill greedily, cutting on whole lines. Never splits one line, so a
        single over-long line comes back as a chunk of its own."""
        pieces, start, used = [], a, 0
        for i in range(a, b):
            add = len(seg[i]) + (1 if i > start else 0)
            if i > start and used + add > limit:
                pieces.append((start, i))
                start, used = i, len(seg[i])
            else:
                used += add
        pieces.append((start, b))
        return pieces

    def divide(a: int, b: int) -> list:
        """Offsets [a, b) -> atoms that each fit, splitting only when forced."""
        if size(a, b) <= limit:
            return [(a, b)]
        seams = blank_seams(a, b)
        if not seams:
            return split_lines(a, b)
        atoms, start = [], a
        for seam in seams + [b]:
            atoms.extend(split_lines(start, seam)
                         if size(start, seam) > limit else [(start, seam)])
            start = seam
        return atoms

    # Node boundaries first: those are the cuts the graph can vouch for.
    node_cuts = set()
    for start, end in node_ranges or ():
        for boundary in (start, end + 1):
            offset = boundary - line_start
            if 0 < offset < n:
                node_cuts.add(offset)
    starts = [0] + sorted(node_cuts) + [n]

    # Then split whatever is still too big: the module-level run between two
    # nodes, a file with no nodes at all, or one function past the budget.
    atoms = []
    for i in range(len(starts) - 1):
        atoms.extend(divide(starts[i], starts[i + 1]))

    # Pack neighbouring atoms into a chunk while they still fit. Atoms are in
    # order and contiguous, so every chunk is a contiguous line range.
    chunks, a, b, used = [], 0, 0, 0
    for start, end in atoms:
        atom = size(start, end)
        if b > a and used + 1 + atom > limit:
            chunks.append((line_start + a, line_start + b - 1))
            a, b, used = start, end, atom
        elif b > a:
            b, used = end, used + 1 + atom
        else:
            a, b, used = start, end, atom
    if b > a:
        chunks.append((line_start + a, line_start + b - 1))
    return chunks


def apply_model_config(client, cfg: dict):
    """Point a live client at the configured endpoint and models.

    Mutating the client is not enough on its own — `chat()` used to take its
    model as a *default argument*, which Python binds once at import, so
    setting client.DEFAULT_MODEL changed nothing about what was sent. Fixing
    that is what makes any of this take effect.
    """
    client.base_url = (cfg.get("owui_url") or OWUI_URL).rstrip("/")
    client.api_key  = cfg.get("owui_api_key", "")
    client.headers  = {"Content-Type": "application/json"}
    if client.api_key:
        client.headers["Authorization"] = f"Bearer {client.api_key}"

    client.DEFAULT_MODEL    = cfg.get("default") or DEFAULT_MODEL
    client.CHAT_MODEL       = resolve_model(cfg, "chat")
    client.EDIT_MODEL       = resolve_model(cfg, "edit")
    client.PLANNER_MODEL    = cfg.get("planner") or PLANNER_MODEL
    client.SUMMARIZER_MODEL = cfg.get("summarizer") or SUMMARIZER_MODEL
    client.EDIT_PROMPT      = cfg.get("edit_prompt") or DEFAULT_EDIT_PROMPT
    return client


# ── Model list ────────────────────────────────────────────────────────────

MODELS_TTL   = 30.0
_MODELS_CACHE = {"key": None, "at": 0.0, "models": [], "error": None}


def _normalize_model(entry) -> Optional[dict]:
    """One entry from `/api/models`, reduced to what the UI needs.

    The endpoint mixes things together — chat models alongside access grants
    and knowledge *collections* — and an `id` alone does not make something a
    chat model, so those are filtered out by shape.

    `owned_by` is deliberately dropped: it reads "openai" on a preset whose
    own `connection_type` is "external", so inferring anything from it would
    be wrong. Display name and id are kept separate because they differ
    (`doug-bot` shows as "Doug-bot").
    """
    if not isinstance(entry, dict):
        return None
    if entry.get("resource_type") or entry.get("type") == "collection":
        return None                     # an access grant or a knowledge base
    if entry.get("arena"):
        return None                     # the arena pseudo-model

    mid = (entry.get("id") or "").strip()
    if not mid:
        return None

    info = entry.get("info") if isinstance(entry.get("info"), dict) else {}
    meta = info.get("meta") if isinstance(info.get("meta"), dict) else {}
    return {
        "id":              mid,
        "name":            (entry.get("name") or mid).strip(),
        "description":     (meta.get("description")
                            or entry.get("description") or "").strip(),
        "base_model_id":   (info.get("base_model_id") or "").strip(),
        "connection_type": (entry.get("connection_type")
                            or info.get("connection_type") or "").strip(),
        "preset":          bool(entry.get("preset") or info.get("base_model_id")),
    }


def list_models(base_url: Optional[str] = None, api_key: Optional[str] = None,
                force: bool = False, timeout: float = 10.0):
    """The endpoint's chat models, normalized. Returns `(models, error)`.

    The single fetcher for the app: app.py had a second copy of this that
    called `requests.get` directly in a route defined *below* `app.run()`, so
    it never registered — and app.py never imported `requests`, so it would
    have raised NameError if it had.

    A failed fetch leaves the last good list in place rather than emptying the
    picker, and returns the error alongside it so the caller can decide whether
    to trust it. An empty list with an error is the "nothing known" case.
    """
    key = (base_url or OWUI_URL, api_key if api_key is not None else OWUI_API_KEY)
    now = time.time()

    if (not force and _MODELS_CACHE["models"] and _MODELS_CACHE["key"] == key
            and now - _MODELS_CACHE["at"] < MODELS_TTL):
        return _MODELS_CACHE["models"], _MODELS_CACHE["error"]

    headers = {"Content-Type": "application/json"}
    if key[1]:
        headers["Authorization"] = f"Bearer {key[1]}"

    try:
        r = requests.get(f"{key[0].rstrip('/')}/api/models",
                         headers=headers, timeout=timeout)
        r.raise_for_status()
        raw = r.json().get("data", [])
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        logger.warning(f"Could not fetch models from {key[0]}: {error}")
        if _MODELS_CACHE["key"] != key:
            return [], error          # never present another key's list
        _MODELS_CACHE["error"] = error
        return _MODELS_CACHE["models"], error

    seen, models = set(), []
    for entry in raw:
        m = _normalize_model(entry)
        if m and m["id"] not in seen:
            seen.add(m["id"])
            models.append(m)
    models.sort(key=lambda m: m["name"].lower())

    _MODELS_CACHE.update({"key": key, "at": now, "models": models, "error": None})
    return models, None


# ── Reasoning traces ──────────────────────────────────────────────────────

_THINK_PAIR = re.compile(r"<think(?:ing)?\b[^>]*>.*?</think(?:ing)?\s*>",
                         re.DOTALL | re.IGNORECASE)
_THINK_OPEN = re.compile(r"<think(?:ing)?\b[^>]*>", re.IGNORECASE)


def strip_think(text: str):
    """Remove a reasoning model's thinking trace. Returns `(text, stripped)`.

    Paired `...` blocks go entirely. An *unclosed* opener — a truncated
    trace — is the awkward one: there is no way to tell where the reasoning
    stops and the answer starts, so only the tag itself is dropped and the
    text is left alone. This feeds an approve-before-write box, and silently
    discarding model output is worse than showing the user some noise.

    `stripped` is returned rather than just applied so the caller can say so;
    a model that ignores "no explanations" is worth knowing about.
    """
    if not text:
        return text, False
    stripped = bool(_THINK_PAIR.search(text))
    text = _THINK_PAIR.sub("", text)
    if _THINK_OPEN.search(text):
        stripped = True
        text = _THINK_OPEN.sub("", text)
    return text.strip("\n"), stripped


# ── Code fences ───────────────────────────────────────────────────────────

# A fence line: up to three spaces of indent, three or more backticks or tildes,
# then an optional info string ("python", "python title=main.py").
_FENCE_LINE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})[ \t]*(\S.*)?$")


def strip_code_fence(text: str):
    """Unwrap a markdown fence that wraps the whole reply. `(text, stripped)`.

    Some models wrap the entire answer in a fence however the prompt is worded —
    ```python\n…the file…\n``` for a full-file rewrite — which would otherwise
    land in the editor as literal backticks through the whole file.

    Only a fence around the *whole* reply is removed, because that is the one
    case with no second reading. A fence with prose around it is left alone on
    purpose: for a prose target the answer can legitimately be a sentence with a
    code block inside it ("Use this:\n```sh\nmake\n```"), and unwrapping that
    would silently drop the sentence. Nothing in the text distinguishes those two
    shapes, so this does not guess — the diff is where the user decides.

    The body may itself contain fences: the wrap is measured from the first
    non-blank line to the last, so nesting comes out intact. The opening fence
    may carry a language tag and the closing one may not. An *unclosed* fence,
    like an unclosed thinking trace, loses only the fence line.
    """
    if not text:
        return text, False

    lines = text.split("\n")
    nonblank = [i for i, line in enumerate(lines) if line.strip()]
    if not nonblank:
        return text, False

    first, last = nonblank[0], nonblank[-1]
    opening = _FENCE_LINE.match(lines[first])
    if not opening:
        return text, False

    marker  = opening.group(1)
    closing = _FENCE_LINE.match(lines[last])
    if (last > first and closing
            and closing.group(1)[0] == marker[0]
            and len(closing.group(1)) >= len(marker)
            and not closing.group(2)):
        return "\n".join(lines[first + 1:last]).strip("\n"), True

    # Unclosed: only the fence line goes, and the rest is kept verbatim.
    del lines[first]
    return "\n".join(lines).strip("\n"), True


class OWUIClient:

    def __init__(self):
        self.base_url         = OWUI_URL.rstrip("/")
        self.api_key          = OWUI_API_KEY
        self.DEFAULT_MODEL    = DEFAULT_MODEL
        self.CHAT_MODEL       = DEFAULT_MODEL
        self.EDIT_MODEL       = DEFAULT_MODEL
        self.SUMMARIZER_MODEL = SUMMARIZER_MODEL
        self.PLANNER_MODEL    = PLANNER_MODEL
        self.EDIT_PROMPT      = DEFAULT_EDIT_PROMPT
        self.headers          = {"Content-Type": "application/json"}
        if self.api_key:
            self.headers["Authorization"] = f"Bearer {self.api_key}"

    def chat(
        self,
        messages: list,
        model: Optional[str] = None,
        stream: bool = False,
        temperature: Optional[float] = None,
    ) -> str:
        """
        Send messages to Open WebUI and return assistant response text.
        Includes chat_id to bypass Open WebUI v0.9.5 NoneType bug.

        `model=None` means "whatever this client is configured with". It is
        deliberately not `model: str = DEFAULT_MODEL`: that binds the value
        once, at import, so the model a caller gets never followed the
        configured default.
        """
        model = model or self.DEFAULT_MODEL
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

    def get_models(self, force: bool = False) -> list:
        """Available models at this endpoint, normalized and cached.

        Delegates to the module-level fetcher so there is exactly one place
        that talks to `/api/models`, and it uses *this* client's URL and key
        rather than the environment's.
        """
        models, _ = list_models(base_url=self.base_url, api_key=self.api_key,
                                force=force)
        return models

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
        model: Optional[str] = None,
    ) -> dict:
        """Ask the planner model to assess project state and suggest next steps.

        Every context part is optional and None is the normal case rather than
        an error: no snapshot until the first indexed commit, no graph until the
        first build, no log on a fresh repo. What is missing is named in the
        prompt, so the model reads it as "not yet" rather than "nothing here".
        """
        import json, re
    
        context_parts = []
        if snapshot:
            context_parts.append(f"## Current Project State\n{snapshot}")
        if graph_summary:
            context_parts.append(f"## Codebase Structure\n{graph_summary}")
        if recent_commits:
            # `commit_hash` or `hash` depending on whether the caller passed a
            # commit record or a git log line; both mean the same thing here.
            commit_lines = "\n".join([
                f"- [{str(c.get('commit_hash') or c.get('hash') or '')[:8]}] "
                f"{c.get('message', '')}"
                for c in recent_commits[:5]
            ])
            context_parts.append(f"## Recent Commits\n{commit_lines}")
    
        missing = []
        if not snapshot:
            missing.append("no snapshot has been indexed for this project yet")
        if not graph_summary:
            missing.append("the code graph has not been built yet")
        if not recent_commits:
            missing.append("there is no commit history yet")

        prompt = (
            "\n\n".join(context_parts) +
            (("\n\n## Not Available\n" +
              "\n".join(f"- {m}" for m in missing))
             if missing else "") +
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
                model=model or self.PLANNER_MODEL,
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
