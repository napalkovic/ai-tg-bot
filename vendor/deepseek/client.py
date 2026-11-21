"""
Pure-HTTP DeepSeek chat client.

Speaks chat.deepseek.com's internal API directly using a captured signed-in
session (see `deepseek.auth`). For each message it:

    1. creates a chat session   (POST /api/v0/chat_session/create)
    2. fetches a PoW challenge   (POST /api/v0/chat/create_pow_challenge)
    3. solves it via the WASM    (deepseek.pow.DeepSeekPow)
    4. POSTs the completion       with the x-ds-pow-response header
    5. parses the SSE stream      into text

    from deepseek.auth import get_session
    from deepseek.client import DeepSeekClient

    client = DeepSeekClient(get_session())
    print(client.chat("Hello!"))                 # full reply
    for chunk in client.stream("Tell a joke"):   # streamed
        print(chunk, end="", flush=True)
"""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass
from typing import Iterator, Optional

import httpx

from .auth import Session, get_session
from .pow import DeepSeekPow

BASE = "https://chat.deepseek.com"
COMPLETION_PATH = "/api/v0/chat/completion"

# DeepSeek's mode pill, sent as `model_type` in the completion body. "default" is
# Instant (the fast model); "expert" is the stronger, slower model. Omitting the
# field lets the backend pick, so we always send one explicitly.
DEFAULT_MODEL_TYPE = "default"

# A conversation_id is an opaque "<chat_session_id>:<last_message_id>" token. It
# carries everything needed to resume a thread, so the client stays stateless.
_CID_SEP = ":"


def _encode_cid(session_id: str, message_id: Optional[int]) -> str:
    if message_id is None:
        return session_id
    return f"{session_id}{_CID_SEP}{message_id}"


def _decode_cid(conversation_id: Optional[str]) -> tuple[Optional[str], Optional[int]]:
    """Split a conversation_id back into (chat_session_id, parent_message_id)."""
    if not conversation_id:
        return None, None
    session_id, _, msg = conversation_id.partition(_CID_SEP)
    parent = int(msg) if msg.isdigit() else None
    return (session_id or None), parent


@dataclass
class Reply:
    """A completed chat reply plus the id to resume the conversation."""

    text: str
    conversation_id: str

    def __str__(self) -> str:  # so print(reply) shows the text
        return self.text


def _biz(data: dict) -> dict:
    """Unwrap DeepSeek's `data.biz_data` envelope, raising on API-level errors."""
    if data.get("code") != 0:
        raise RuntimeError(f"DeepSeek API error: {data.get('msg') or data}")
    biz = data.get("data", {}).get("biz_data")
    if biz is None:
        raise RuntimeError(f"Unexpected response shape: {data}")
    return biz


class DeepSeekClient:
    def __init__(
        self,
        session: Optional[Session] = None,
        allow_interactive: bool = True,
    ):
        # `allow_interactive=False` makes session resolution non-blocking: it
        # uses a cached/headless session and raises LoginRequired instead of
        # opening a browser window. The server passes False (see server/api.py).
        self.session = session or get_session(allow_interactive=allow_interactive)
        self._pow = DeepSeekPow()
        # The wasmtime Store behind the PoW solver is not reentrant; serialise
        # access so concurrent server requests don't corrupt it.
        self._pow_lock = threading.Lock()
        self._http = httpx.Client(
            base_url=BASE,
            headers=self._base_headers(),
            cookies=self.session.cookies,
            timeout=httpx.Timeout(120.0, read=300.0),
        )

    def _base_headers(self) -> dict:
        app_version = os.getenv("DEEPSEEK_APP_VERSION", "2.0.0")
        headers = {
            "authorization": f"Bearer {self.session.token}",
            "accept": "*/*",
            "content-type": "application/json",
            "user-agent": self.session.user_agent,
            "origin": BASE,
            "referer": f"{BASE}/",
            "x-app-version": app_version,
            "x-client-version": app_version,
            "x-client-platform": "web",
            "x-client-bundle-id": "com.deepseek.chat",
        }
        # Only send client hints we actually captured from the signed-in browser.
        # Sending a hardcoded locale/timezone makes every bridge user share the
        # same (and usually wrong) fingerprint.
        if self.session.locale:
            headers["x-client-locale"] = self.session.locale
            headers["accept-language"] = self.session.locale.replace("_", "-")
        if self.session.timezone_offset is not None:
            headers["x-client-timezone-offset"] = str(
                int(self.session.timezone_offset)
            )
        return headers

    # --- protocol steps -----------------------------------------------------

    def create_chat_session(self) -> str:
        r = self._http.post("/api/v0/chat_session/create", json={})
        r.raise_for_status()
        return _biz(r.json())["chat_session"]["id"]

    def _pow_header(self, target_path: str = COMPLETION_PATH) -> str:
        r = self._http.post(
            "/api/v0/chat/create_pow_challenge", json={"target_path": target_path}
        )
        r.raise_for_status()
        challenge = _biz(r.json())["challenge"]
        with self._pow_lock:
            return self._pow.make_header(challenge)

    # --- public API ---------------------------------------------------------

    def stream(
        self,
        prompt: str,
        conversation_id: Optional[str] = None,
        model: Optional[str] = None,
        thinking: bool = False,
        search: bool = False,
    ) -> "_Stream":
        """Stream a reply. Iterate it for text chunks; read `.conversation_id`
        afterwards to resume the thread. Pass an existing `conversation_id` to
        continue a previous conversation.

        `model` is DeepSeek's model_type wire value: "default" (Instant) or
        "expert"; it defaults to "default" on a NEW thread. It cannot be combined
        with `conversation_id` — a thread's model is fixed when it's created, so
        resuming keeps the original model. `thinking` enables DeepThink reasoning
        and `search` enables web search; both are independent of the model.
        """
        if conversation_id and model is not None:
            raise ValueError(
                "`model` cannot be set together with `conversation_id`; a thread's "
                "model is fixed when it is created. Pass `model` only on the first turn."
            )
        session_id, parent_id = _decode_cid(conversation_id)
        if session_id is None:
            # New thread: select the model (default when unspecified).
            session_id = self.create_chat_session()
            model_type: Optional[str] = model or DEFAULT_MODEL_TYPE
        else:
            # Resuming: let the existing thread's model stand (send no model_type).
            model_type = None
        return _Stream(self, prompt, session_id, parent_id, model_type, thinking, search)

    def chat(
        self,
        prompt: str,
        conversation_id: Optional[str] = None,
        model: Optional[str] = None,
        thinking: bool = False,
        search: bool = False,
    ) -> Reply:
        """Return the complete reply (`.text`) plus its `.conversation_id`."""
        s = self.stream(prompt, conversation_id=conversation_id,
                        model=model, thinking=thinking, search=search)
        text = "".join(s)
        return Reply(text=text, conversation_id=s.conversation_id)

    def close(self) -> None:
        self._http.close()


class _Stream:
    """Iterable of reply-text chunks. After it's consumed, `.conversation_id`
    holds the token for resuming the conversation."""

    def __init__(self, client: "DeepSeekClient", prompt: str, session_id: str,
                 parent_id: Optional[int], model: str,
                 thinking: bool, search: bool):
        self._client = client
        self._prompt = prompt
        self._session_id = session_id
        self._parent_id = parent_id
        self._model = model
        self._thinking = thinking
        self._search = search
        self._message_id: Optional[int] = None

    def __iter__(self) -> Iterator[str]:
        body = {
            "chat_session_id": self._session_id,
            "parent_message_id": self._parent_id,
            "prompt": self._prompt,
            "ref_file_ids": [],
            "thinking_enabled": self._thinking,
            "search_enabled": self._search,
            "action": None,
            "preempt": False,
        }
        # Only select a model on a new thread; on resume the thread keeps its own.
        if self._model is not None:
            body["model_type"] = self._model
        # PoW challenges are short-lived, so solve right before the request.
        headers = {"x-ds-pow-response": self._client._pow_header()}
        meta: dict = {}
        with self._client._http.stream(
            "POST", COMPLETION_PATH, json=body, headers=headers
        ) as resp:
            resp.raise_for_status()
            yield from _parse_sse(resp.iter_lines(), meta)
        if meta.get("message_id") is not None:
            self._message_id = meta["message_id"]

    @property
    def conversation_id(self) -> str:
        return _encode_cid(self._session_id, self._message_id)


_CONTENT_PATH = re.compile(r"^response/fragments/(-?\d+)/content$")
_REF_MARKER = re.compile(r"\[reference:\d+\]")
# Web-search answers also carry visible source markers like «[citation:1]» or
# a bare «[citation:]» (owner saw those at the end of replies). They are
# service data, not text — drop them before streaming to the user.
_CITATION_MARKER = re.compile(
    r"(?i)\[\s*citation\s*:?\s*[^\]]*\]|【\s*citation\s*[^】]*】"
)


def _parse_sse(lines, meta: Optional[dict] = None) -> Iterator[str]:
    """Turn DeepSeek's SSE completion stream into reply-text deltas.

    DeepSeek patches the response with an increment of JSON-patch frames:

        {"p": "response/fragments",          "o": "APPEND", "v": [...]}
        {"p": "response/fragments/-1/content", "o": "APPEND", "v": "текст"}
        {"p": "response",                    "o": "BATCH",  "v": [ {p, o, v}, … ]}

    Two protocol details former versions got wrong:

    * ``o`` is sometimes absent; the frame is still an APPEND (observed live:
      the beginning of an answer arrived as
      ``{"p":"response/fragments/-1/content","v":"годня"}`` with no ``o`` and
      used to be dropped, eating characters from the start of the reply).
      Only when ``o`` is explicitly ``SET`` does the frame mean "assign", and
      such frames target scalar fields, not reply text.
    * Fragment creation can be wrapped in a ``BATCH`` frame, so batch items
      must be applied too.

    With DeepThink / web search the fragment list is also rebuilt several
    times (fragments reorder or disappear), so instead of counting emitted
    characters per index we rebuild the whole RESPONSE text after every frame
    and emit only the tail we have not sent yet.
    """
    fragments: list[dict] = []
    active: Optional[int] = None
    emitted = ""

    def response_text() -> str:
        joined = "".join(
            (frag.get("content") or "")
            for frag in fragments
            if frag.get("type") == "RESPONSE"
        )
        # Invisible citation markers for web-search sources; users should
        # never see them in Telegram («[reference:1]», «[citation:2]»).
        return _CITATION_MARKER.sub("", _REF_MARKER.sub("", joined))

    def delta() -> Iterator[str]:
        nonlocal emitted
        full = response_text()
        if full == emitted:
            return
        # A snapshot may be a partial view of what we already streamed; never
        # "un-emit" text, otherwise the next append starts from the wrong place.
        if len(full) < len(emitted) and emitted.startswith(full):
            return
        if full.startswith(emitted):
            fresh = full[len(emitted):]
        else:
            common = 0
            for old, new_ch in zip(emitted, full):
                if old != new_ch:
                    break
                common += 1
            fresh = full[common:]
        emitted = full
        if fresh:
            yield fresh

    def set_fragments(raw) -> None:
        nonlocal fragments
        fragments = [
            {"type": frag.get("type"), "content": frag.get("content") or ""}
            for frag in raw
            if isinstance(frag, dict)
        ]

    def add_fragment(raw) -> None:
        if isinstance(raw, dict):
            fragments.append(
                {"type": raw.get("type"), "content": raw.get("content") or ""}
            )

    def append_to(index, text: str) -> None:
        if index is None:
            return
        if index < 0:
            index += len(fragments)
        if not (0 <= index < len(fragments)):
            return
        fragments[index]["content"] = (fragments[index].get("content") or "") + text

    def patch(path: str, op, value) -> None:
        """Apply one JSON-patch frame; a missing ``o`` means APPEND."""
        nonlocal active
        if op == "BATCH":
            items = value if isinstance(value, list) else []
            for item in items:
                if not isinstance(item, dict) or not isinstance(item.get("p"), str):
                    continue
                patch(f"{path}/{item['p']}", item.get("o"), item.get("v"))
            return

        if path == "response/fragments":
            if op == "APPEND":
                for frag in _parse_fragment_list(value):
                    add_fragment(frag)
                active = len(fragments) - 1 if fragments else None
            return

        if meta is not None and path.endswith("message_id") and isinstance(value, int):
            meta["message_id"] = value

        m = _CONTENT_PATH.match(path)
        if m:
            index = int(m.group(1))
            active = index if index >= 0 else len(fragments) - 1
            if isinstance(value, str) and op in (None, "APPEND"):
                append_to(active, value)

    for line in lines:
        if not line or not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue

        v = obj.get("v")

        # Snapshot frame: the full response object, replacing what we knew.
        if isinstance(v, dict) and "response" in v:
            if meta is not None:
                _capture_message_id(meta, v)
            raw = [
                frag for frag in v["response"].get("fragments", [])
                if isinstance(frag, dict)
            ]
            set_fragments(raw)
            active = len(fragments) - 1 if fragments else None
            yield from delta()
            continue

        # Path-setting patch frame (single op or BATCH of ops).
        if isinstance(obj.get("p"), str):
            patch(obj["p"], obj.get("o"), v)
            yield from delta()
            continue

        # Bare append to the current path.
        if isinstance(v, str):
            append_to(active, v)
            yield from delta()


def _parse_fragment_list(v) -> list:
    """Read the fragment descriptors out of a `response/fragments` APPEND frame.

    The payload is a JSON array of one fragment object, but tolerate a bare
    object (or something unreadable) rather than dropping the stream.
    """
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except json.JSONDecodeError:
            return []
    if isinstance(v, dict):
        return [v]
    if isinstance(v, list):
        return [f for f in v if isinstance(f, dict)]
    return []


def _capture_message_id(meta: dict, snapshot: dict) -> None:
    """Best-effort: pull the assistant message_id out of a snapshot frame.

    DeepSeek nests the assistant message under `response`; we check there first,
    then the snapshot root, accepting `message_id` or `id`.
    """
    for container in (snapshot.get("response"), snapshot):
        if isinstance(container, dict):
            mid = container.get("message_id", container.get("id"))
            if isinstance(mid, int):
                meta["message_id"] = mid
                return
