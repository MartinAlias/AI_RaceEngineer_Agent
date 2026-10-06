"""The engineer conversation: a Claude tool-use loop with the rider in the terminal.

A manual loop rather than the SDK's beta tool runner, so that the conversation
can stop for the rider between turns, survive a dropped connection, and be saved
as a transcript.

Request settings:
- Model: claude-opus-5-5 (override with RACE_ENGINEER_MODEL). Thinking is always
  on for this model; depth is set with effort (RACE_ENGINEER_EFFORT, default high).
- Server-side refusal fallback ("default" routing) is enabled.
- The system prompt and tools never change, so they are cached; the conversation
  itself is cached turn to turn with automatic caching.
- History is append-only: assistant turns are sent back exactly as received.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field
from typing import Any

import anthropic

from .. import repo
from .prompts import SYSTEM_PROMPT
from .tools import TOOLS, RiderIO, Toolbox

DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_EFFORT = "high"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_TOOL_ROUNDS = 20  # per engineer turn, guards against a runaway loop
QUIT_WORDS = {"q", "quit", "exit", "done", "bye"}
NO_CREDENTIALS = (
    "No Anthropic API credentials found. Create a key at https://platform.claude.com, then run "
    "`setx ANTHROPIC_API_KEY sk-ant-...` and open a new terminal."
)


class EngineerOffline(Exception):
    """The API could not be reached."""


class EngineerUnavailable(Exception):
    """The API refused the request for a reason the rider has to fix (credentials, billing...)."""


@dataclass
class EngineerConfig:
    model: str = field(default_factory=lambda: os.environ.get("RACE_ENGINEER_MODEL", DEFAULT_MODEL))
    effort: str = field(default_factory=lambda: os.environ.get("RACE_ENGINEER_EFFORT", DEFAULT_EFFORT))
    max_tokens: int = 16000


def make_client() -> anthropic.Anthropic:
    """Credentials come from ANTHROPIC_API_KEY or an `ant auth login` profile.

    Short connect timeout so a dead paddock connection fails fast; long read timeout
    because a thinking turn can take a while.
    """
    try:
        return anthropic.Anthropic(timeout=anthropic.Timeout(180.0, connect=10.0), max_retries=2)
    except anthropic.AnthropicError:
        raise EngineerUnavailable(NO_CREDENTIALS) from None


class Engineer:
    def __init__(
        self,
        conn: sqlite3.Connection,
        client: Any,
        io: RiderIO,
        *,
        bike_id: int,
        session_id: int | None,
        feedback_id: int | None = None,
        config: EngineerConfig | None = None,
    ):
        self.conn = conn
        self.client = client
        self.io = io
        self.bike_id = bike_id
        self.session_id = session_id
        self.feedback_id = feedback_id
        self.config = config or EngineerConfig()
        self.tools = Toolbox(conn, io, bike_id=bike_id, session_id=session_id)
        self.messages: list[dict[str, Any]] = []
        self.conversation_id: int | None = None

    def opening_message(self) -> str:
        """First user turn: the complaint, plus what the keyword tagger guessed."""
        if self.feedback_id is None:
            return "The rider wants to talk through the bike. Start by getting the session context."
        f = repo.get_feedback(self.conn, self.feedback_id)
        session = repo.get_session(self.conn, f.session_id)
        track = repo.get_track(self.conn, session.track_id)
        tags = ", ".join(f"{k}={v}" for k, v in (("end", f.end), ("phase", f.phase),
                                                    ("corners", ",".join(f.corners) or None),
                                                    ("symptom", f.symptom), ("severity", f.severity)) if v)
        text = (f'Rider feedback #{f.id} from session #{session.id} ({track.name}, {session.type}, {session.date}): '
                f'"{f.text}"')
        if tags:
            text += f"\nKeyword tags (automatic guesses, may be wrong): {tags}"
        if self.session_id is not None and str(self.session_id) != repo.get_state(self.conn, "current_session_id"):
            text += ("\nThis feedback was logged earlier and is being reviewed after the fact; "
                     "the rider may not be at the track now.")
        return text

    def run(self, opening: str) -> None:
        """Talk with the rider until they quit. Raises EngineerOffline / EngineerUnavailable."""
        self.messages.append({"role": "user", "content": opening})
        try:
            while True:
                self._engineer_turn()
                reply = self._rider_turn()
                if reply is None:
                    return
                self.messages.append({"role": "user", "content": reply})
        finally:
            self.save()

    def _rider_turn(self) -> str | None:
        while True:
            reply = self.io.ask("you")
            if reply is None or reply.strip().lower() in QUIT_WORDS:
                return None
            if reply.strip():
                return reply.strip()

    def _engineer_turn(self) -> None:
        for _ in range(MAX_TOOL_ROUNDS):
            response = self._create()

            if response.stop_reason == "refusal":
                # Not added to the history; the rider can rephrase.
                self.io.notice("The engineer declined to answer that. Try rephrasing.")
                return
            if response.stop_reason == "max_tokens":
                # A truncated turn may hold a half-written tool call: never run it.
                self.io.notice("The engineer's answer was cut off. Ask it to continue or be shorter.")
                return

            self.messages.append({"role": "assistant", "content": response.content})
            text = "\n\n".join(b.text for b in response.content if b.type == "text" and b.text.strip())
            if text:
                self.io.say(text)

            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if not tool_uses:
                return
            results = []
            for block in tool_uses:
                output, is_error = self.tools.run(block.name, block.input)
                result: dict[str, Any] = {"type": "tool_result", "tool_use_id": block.id, "content": output}
                if is_error:
                    result["is_error"] = True
                results.append(result)
            self.messages.append({"role": "user", "content": results})  # all results in one message
            self.save()
        self.io.notice("The engineer made too many lookups in one go; stopping here.")

    def _create(self) -> Any:
        try:
            return self.client.beta.messages.create(
                model=self.config.model,
                max_tokens=self.config.max_tokens,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                messages=self.messages,
                output_config={"effort": self.config.effort},
                cache_control={"type": "ephemeral"},
                betas=[FALLBACK_BETA],
                fallbacks="default",
            )
        except anthropic.APIConnectionError as error:  # includes timeouts, after the SDK's retries
            raise EngineerOffline(str(error)) from None
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as error:
            raise EngineerUnavailable(f"Anthropic API rejected the credentials: {error.message}") from None
        except anthropic.RateLimitError:
            raise EngineerUnavailable("Anthropic API rate limit reached. Wait a minute and try again.") from None
        except anthropic.APIStatusError as error:
            raise EngineerUnavailable(f"Anthropic API error {error.status_code}: {error.message}") from None
        except TypeError as error:
            # The SDK resolves credentials lazily and reports a missing key this way.
            if "authentication" not in str(error):
                raise
            raise EngineerUnavailable(NO_CREDENTIALS) from None

    def save(self) -> None:
        """Store the transcript once the engineer has said something (a failed start isn't a conversation)."""
        if not any(m["role"] == "assistant" for m in self.messages):
            return
        self.conversation_id = repo.save_conversation(
            self.conn, self.conversation_id, bike_id=self.bike_id, session_id=self.session_id,
            feedback_id=self.feedback_id, model=self.config.model,
            messages=[{"role": m["role"], "content": _jsonable(m["content"])} for m in self.messages],
        )


def _jsonable(content: Any) -> Any:
    if isinstance(content, str):
        return content
    out = []
    for block in content:
        if isinstance(block, dict):
            out.append(block)
        elif hasattr(block, "to_dict"):
            out.append(block.to_dict())
        else:
            out.append(dict(vars(block)))
    return out
