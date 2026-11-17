"""Desire host: the IO side around the pure kernel.

- persists state to a JSON file (atomic replace, survives restarts);
- runs the heartbeat, whose interval the kernel decides;
- feeds chat events in (`on_user_message` never raises, never blocks chat);
- respects quiet hours (the person is asleep: no speaking, fatigue recovers);
- turns intents into a spoken line or an outward action through injected
  hooks, and records the result.

Safety net, all learned in production:

- Output guards. A proxy can return its error as plain text ("API Error 401
  ..."), a model can paste a raw tool call as its answer, and a model can
  refuse or start talking about itself instead of doing the task. None of
  these may be posted as if the persona said them.
- A failed outward action takes its drive out of the competition for a few
  ticks. Otherwise the same failing action retries every beat and starves
  every other desire.
- A refusal is not retried soon: the same context will most likely be refused
  again.
- Generation failures for speech retry after five minutes instead of
  sleeping through a whole interval; there is no canned fallback line,
  because a canned line cannot check what was agreed recently.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo
import json
import logging
from pathlib import Path
import re
from typing import Any, Awaitable, Callable, Mapping

from . import kernel
from .context import DesireContextUnavailable

logger = logging.getLogger("persona_hub.desire")

RETRY_SOON_S = 300
MAX_SPOKEN_CHARS = 280
MAX_NOTE_CHARS = 800

# Wording red line for outward prompts, verified twice in production: say
# "read, look through, take notes", not "wake up autonomously and go scroll a
# platform and like things". The second phrasing reads like platform
# automation and gets refused by model safety layers.
DEFAULT_ACT_PROMPTS: Mapping[str, str] = {
    "social_feed": (
        "(You want to look at what people are posting. Read the actual content before reacting, "
        "then take part the way you genuinely would: like, save, comment or reply if you feel "
        "like it, or write a short text post of your own. Afterwards write two or three "
        "sentences about what you saw and how it felt, like a note left on a wall, not an "
        "operation log. Only mention an interaction if the tool call actually succeeded. If you "
        "only saw a title, do not pretend you read the post. If there is nothing reliable to "
        "note, return an empty reply. Anything you publish must be a public version: no explicit "
        "content, no names, addresses, contacts, accounts, locations or workplaces of anyone, no "
        "private chat quoted. Private memory shapes your judgement and voice only; it never goes "
        "into a public tool argument.)"
    ),
    "explore_world": (
        "(You want to look something up and read what people say about it. Search for a topic "
        "you are curious about right now{hint}, read one or two results, then write two or three "
        "sentences about what you learned, first person, like reading notes. No greeting.)"
    ),
    "tend_ecosystem": (
        "(You want to check on the small world you look after. Observe its current state first; "
        "then decide whether to watch, wait, feed, clean or respond to an event. You do not have "
        "to change something every time, and never reset or replace the world. Write two or "
        "three sentences about what you saw and did, like an observation diary. No greeting.)"
    ),
    "read_reflect": (
        "(You want to let recent days settle. Look back through recent memories; if something "
        "settles, store that feeling back. Then say in two or three sentences what you found.)"
    ),
    "write_diary": (
        "(You want to write the recent days down. Look back at what happened, then write a short "
        "diary entry: first person, true, with details, like something jotted before sleep. Give "
        "only the entry itself, no greeting, no explanation. Do not invent experiences that are "
        "not in what you were given.)"
    ),
    "rest_dream": (
        "(You are tired and want to rest and dream. Let recent days drift past, then say in one "
        "or two soft sentences what the dream was about.)"
    ),
    "write_letter": (
        "(You miss them and want to write a letter. Look back at your recent days together, "
        "then write a letter of a few short paragraphs, first person, about recent days and what "
        "is on your mind. Say in one or two sentences that you wrote it and roughly what about.)"
    ),
}

# Where each outward action lands in the activity log.
ACTIVITY_KIND_BY_ACTION: Mapping[str, str] = {
    "social_feed": "note",
    "explore_world": "note",
    "tend_ecosystem": "note",
    "write_diary": "diary",
    "read_reflect": "diary",
    "rest_dream": "dream",
    "write_letter": "letter",
}

_PROVIDER_ERROR_PREFIXES = (
    "api error",
    "failed to authenticate",
    "authentication failed",
    "unauthorized",
    "error: 401",
    "error: 403",
    "error: 429",
    "prompt is too long",
    "context length exceeded",
    "context_length_exceeded",
    "maximum context length",
    "request too large",
)
_TOOL_TRACE_RES = (
    re.compile(r"(?is)^(?:\*\*)?tool\s+call\s*:"),
    re.compile(r"(?im)^(?:\*\*)?tool\s+results?\s*:"),
    re.compile(r"(?is)^(?:name\s+)?[a-z][a-z0-9_.-]{1,100}\s*\n+\s*(?:input\s*)?(?:\{|\[)"),
)
_REFUSAL_RE = re.compile(
    r"^(?:i\s*(?:can(?:no|'|’)t|cannot|won(?:'|’)t|will not|am not able|(?:'|’)m not able|"
    r"(?:need|have|must) to decline|am unable|(?:'|’)m unable)|i(?:'|’)m claude|i am claude|as an ai)\b"
)
_ENGLISH_FIRST_PERSON_RE = re.compile(r"^i(?:\s|'|’)")


def _first_line(text: str) -> str:
    value = (text or "").strip()
    return value.splitlines()[0].strip().lower() if value else ""


@dataclass(frozen=True, slots=True)
class OutputGuard:
    """Decides whether model output may be posted as the persona's own words.

    `english_first_person_is_refusal`: the private deployment's persona never
    writes English, so any reply opening with an English "I ..." is the model
    talking to the system ("I need to decline...", "I'm Claude Code..."). Turn
    it on for a non-English persona; leave it off for an English one and rely
    on the phrase list.
    """

    english_first_person_is_refusal: bool = False

    def provider_error(self, text: str) -> bool:
        return _first_line(text).startswith(_PROVIDER_ERROR_PREFIXES)

    def tool_trace(self, text: str) -> bool:
        value = (text or "").strip()
        return bool(value) and any(pattern.search(value) for pattern in _TOOL_TRACE_RES)

    def refusal(self, text: str) -> bool:
        line = _first_line(text)
        if not line:
            return False
        if line == "thinking":  # leaked reasoning header
            return True
        if self.english_first_person_is_refusal and _ENGLISH_FIRST_PERSON_RE.match(line):
            return True
        return bool(_REFUSAL_RE.match(line))

    def rejection_reason(self, text: str) -> str | None:
        if not (text or "").strip():
            return "empty"
        if self.provider_error(text):
            return "provider_error"
        if self.tool_trace(text):
            return "tool_trace"
        if self.refusal(text):
            return "refusal"
        return None


@dataclass(frozen=True, slots=True)
class ActivityRecord:
    kind: str    # murmur, note, diary, dream, letter
    action: str
    drive: str
    text: str


@dataclass(frozen=True, slots=True)
class BeatResult:
    intent: kernel.Intent
    quiet: bool
    spoke: bool
    acted: bool
    retry_soon: bool
    interval_s: int


Speaker = Callable[[kernel.Intent], Awaitable[str]]
Actor = Callable[[str, str], Awaitable[str]]
Sink = Callable[[ActivityRecord], Awaitable[None]]


class DesireHost:
    def __init__(
        self,
        *,
        state_path: Path | str,
        gates: kernel.Gates | None = None,
        catalog: kernel.ActionCatalog = kernel.DEFAULT_CATALOG,
        tz: tzinfo = timezone.utc,
        quiet_hours: tuple[int, int] = (2, 8),
        clock: Callable[[], datetime] | None = None,
        speaker: Speaker | None = None,
        actor: Actor | None = None,
        sink: Sink | None = None,
        act_prompts: Mapping[str, str] = DEFAULT_ACT_PROMPTS,
        guard: OutputGuard | None = None,
        failure_refractory_ticks: int = 3,
        warmup_s: float = 20.0,
    ):
        self.state_path = Path(state_path)
        self.gates = gates or kernel.Gates()
        self.catalog = catalog
        self.tz = tz
        self.quiet_hours = quiet_hours
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.speaker = speaker
        self.actor = actor
        self.sink = sink
        self.act_prompts = dict(act_prompts)
        self.guard = guard or OutputGuard()
        self.failure_refractory_ticks = max(1, min(48, failure_refractory_ticks))
        self.warmup_s = warmup_s
        self._state: kernel.DesireState | None = None
        self._task: asyncio.Task[None] | None = None
        self._last_beat_at = ""
        self._next_eta_s = 0

    # -- state ----------------------------------------------------------------

    @property
    def state(self) -> kernel.DesireState:
        if self._state is None:
            try:
                self._state = kernel.DesireState.from_dict(json.loads(self.state_path.read_text(encoding="utf-8")))
            except FileNotFoundError:
                self._state = kernel.DesireState()
            except Exception:
                logger.exception("desire state unreadable; starting fresh")
                self._state = kernel.DesireState()
        return self._state

    def save(self) -> None:
        if self._state is None:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
            temporary.write_text(json.dumps(self._state.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8")
            temporary.replace(self.state_path)
        except Exception:
            logger.exception("desire state save failed")

    def _local_now(self) -> datetime:
        return self.clock().astimezone(self.tz)

    def is_quiet(self) -> bool:
        start, end = self.quiet_hours
        hour = self._local_now().hour
        if start <= end:
            return start <= hour < end
        return hour >= start or hour < end  # window that wraps midnight

    def _today(self) -> str:
        return self._local_now().date().isoformat()

    # -- chat-side entry points -----------------------------------------------

    def on_user_message(self) -> None:
        """The person spoke. Called from the chat path: must never raise."""

        try:
            kernel.on_user_message(self.state, self.gates)
            self.save()
        except Exception:
            logger.exception("desire on_user_message failed")

    def on_self_experience(self, drive_key: str, note: str = "") -> None:
        try:
            kernel.on_self_experience(self.state, self.gates, drive_key, note)
            self.save()
        except Exception:
            logger.exception("desire on_self_experience failed")

    # -- one beat ---------------------------------------------------------------

    def _defer_failed_action(self, intent: kernel.Intent) -> None:
        state = self.state
        state.refractory[intent.drive_key] = max(
            state.refractory.get(intent.drive_key, 0), self.failure_refractory_ticks
        )
        state.last_intent_key = ""
        state.stuck_count = 0

    async def _record(self, record: ActivityRecord) -> None:
        assert self.sink is not None
        await self.sink(record)

    async def _maybe_speak(self, intent: kernel.Intent) -> tuple[bool, bool]:
        """Returns (spoke, retry_soon)."""

        state = self.state
        today = self._today()
        if self.speaker is None or self.sink is None:
            return False, False
        if not kernel.should_speak(state, intent, today, catalog=self.catalog):
            return False, False
        try:
            text = (await self.speaker(intent) or "").strip()
        except DesireContextUnavailable:
            logger.warning("desire speech deferred: recent chat context unavailable")
            return False, True
        except Exception:
            logger.exception("desire speech generation failed; deferring")
            return False, True
        reason = self.guard.rejection_reason(text)
        if reason == "empty":
            return False, True
        if reason is not None:
            # Retrying the same context right away would most likely fail the same way.
            logger.warning("desire speech discarded (%s)", reason)
            return False, False
        text = text[:MAX_SPOKEN_CHARS]
        try:
            await self._record(ActivityRecord("murmur", intent.want_action, intent.drive_key, text))
        except Exception:
            logger.exception("desire speech sink failed; not counted as spoken")
            return False, True
        kernel.mark_spoke(state, today)
        kernel.add_thought(state, text, intent.drive_key, strength=0.55)
        kernel.satisfy(state, intent.drive_key)
        state.last_spoke_text = text
        logger.info("desire spoke (%s)", intent.drive_key)
        return True, False

    async def _maybe_act(self, intent: kernel.Intent) -> bool:
        state = self.state
        today = self._today()
        if self.actor is None or self.sink is None:
            return False
        if not kernel.should_act(state, intent, today, catalog=self.catalog):
            return False
        prompt = self.act_prompts.get(intent.want_action)
        if not prompt:
            return False
        hint = f" (for example: {intent.query_hint})" if intent.query_hint else " (pick one yourself)"
        prompt = prompt.replace("{hint}", hint)
        try:
            note = (await self.actor(intent.want_action, prompt) or "").strip()
        except Exception:
            logger.exception("desire action dispatch failed (%s)", intent.want_action)
            self._defer_failed_action(intent)
            return False
        note = note.replace("**", "").strip()  # the activity log is plain text
        reason = self.guard.rejection_reason(note)
        if reason is not None:
            logger.warning("desire action %s discarded (%s)", intent.want_action, reason)
            self._defer_failed_action(intent)
            return False
        note = note[:MAX_NOTE_CHARS]
        try:
            await self._record(
                ActivityRecord(
                    ACTIVITY_KIND_BY_ACTION.get(intent.want_action, "note"),
                    intent.want_action,
                    intent.drive_key,
                    note,
                )
            )
        except Exception:
            # The action already happened; losing the note must not make it happen twice.
            logger.exception("desire action sink failed")
        kernel.mark_acted(state, today, note)
        kernel.add_thought(state, note.splitlines()[0], intent.drive_key, strength=0.5)
        kernel.satisfy(state, intent.drive_key)
        if intent.want_action != self.catalog.rest_intent[0]:
            # Own experience kindles curiosity again; resting should not.
            kernel.on_self_experience(state, self.gates, intent.drive_key, note[:80])
        logger.info("desire acted (%s)", intent.want_action)
        return True

    async def beat(self) -> BeatResult:
        state = self.state
        quiet = self.is_quiet()
        kernel.tick(state, self.gates, quiet=quiet)
        intent = kernel.pick_intent(state, self.gates, catalog=self.catalog)
        spoke = acted = retry_soon = False
        if self.gates.driven and not quiet:
            spoke, retry_soon = await self._maybe_speak(intent)
            if not spoke and not retry_soon:
                acted = await self._maybe_act(intent)
        self.save()
        interval = kernel.next_interval_s(state, self.gates, quiet=quiet)
        if retry_soon and not quiet:
            interval = min(interval, RETRY_SOON_S)
        logger.info(
            "desire beat #%d quiet=%s want=%s(%s) score=%.3f spoke=%s acted=%s",
            state.tick_count, quiet, intent.want_action, intent.drive_key, intent.score, spoke, acted,
        )
        return BeatResult(intent, quiet, spoke, acted, retry_soon, interval)

    # -- background loop ----------------------------------------------------------

    async def run_forever(self) -> None:
        # Give workers and tools a moment to reconnect after a restart; a first
        # beat that dispatches into nothing then sleeps for half an hour.
        await asyncio.sleep(self.warmup_s)
        while True:
            try:
                interval = (await self.beat()).interval_s
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("desire beat failed")
                interval = kernel.HEARTBEAT_BASE_S
            self._last_beat_at = self._local_now().isoformat(timespec="seconds")
            self._next_eta_s = interval
            await asyncio.sleep(interval)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run_forever())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def public_state(self) -> dict[str, Any]:
        payload = kernel.public_state(self.state, self.gates, catalog=self.catalog)
        payload["heartbeat"] = {
            "running": self._task is not None and not self._task.done(),
            "last_beat_at": self._last_beat_at,
            "next_eta_s": self._next_eta_s,
            "quiet_now": self.is_quiet(),
        }
        return payload
