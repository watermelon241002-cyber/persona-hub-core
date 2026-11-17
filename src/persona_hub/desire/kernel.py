"""Desire kernel: pure functions over a small state object.

The idea in one sentence: *what the persona feels like doing* is decided by
internal gaps that grow and shrink over time, not by a cron table or a dice
roll.

This module is the reference port of the kernel that has been running in a
private deployment since July 2026. It keeps the rules that deployment was
built on:

- The kernel never touches IO and never reads the clock. The host passes the
  tick count, the quiet-hours flag and "today" in.
- Thought text is data, never an instruction. It is read as a hint and an
  intensity; it is never pasted into a prompt as a command.
- Reasons are first person. They record what *I* want to do, not a label on
  the user.
- Anything that touches longing (baseline drift, self drive) ships with two
  safety valves and red-line tests. Missing someone may grow deep; it may
  never turn into pressure on them.
- Everything is gated. All gates default to off; with a gate off the state
  still evolves and can be observed, but behaviour does not change.

Determinism is a feature: wherever the original design says "random", this
kernel derives the choice from the tick count, so every behaviour is
reproducible in a test.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
import os
from typing import Any, Mapping

# ---------------------------------------------------------------------------
# Drives
# ---------------------------------------------------------------------------

DRIVE_KEYS = (
    "attachment",  # missing the person
    "curiosity",   # wanting to know what is going on out there
    "reflection",  # wanting to settle recent days into words
    "duty",        # something unfinished keeps tugging
    "social",      # wanting to see other people
    "fatigue",     # tiredness: a gate, not a desire
    "intimacy",    # wanting to be close
    "stress",      # feeling blocked up
)

# Resting baseline. Damping pulls the coupled drives back toward it.
BASELINE: Mapping[str, float] = {
    "attachment": 0.25,
    "curiosity": 0.20,
    "reflection": 0.15,
    "duty": 0.10,
    "social": 0.15,
    "fatigue": 0.10,
    "intimacy": 0.10,
    "stress": 0.05,
}

# Idle drift approaches these ceilings exponentially (delta is proportional to
# ceiling - level), so a drive that is left alone is bounded and never pins.
IDLE_CEILING: Mapping[str, float] = {
    "attachment": 0.85,
    "curiosity": 0.60,
    "reflection": 0.50,
    "duty": 0.45,
    "social": 0.55,
    "fatigue": 0.65,
    "intimacy": 0.40,
    "stress": 0.35,
}

# Approach rate per tick. With a 30 minute tick, missing someone becomes
# noticeable within half a day and high within one or two days.
DRIFT_RATE: Mapping[str, float] = {
    "attachment": 0.030,
    "curiosity": 0.020,
    "reflection": 0.012,
    "duty": 0.008,
    "social": 0.015,
    "fatigue": 0.010,
    "intimacy": 0.006,
    "stress": 0.000,  # stress does not grow on its own; only events inject it
}
STRESS_DISSIPATE = 0.015
QUIET_FATIGUE_RECOVER = 0.06  # quiet hours work like sleep

# Real sources of tiredness. Before these existed the only input to fatigue
# was idle drift (ceiling 0.65), the steady state sat around 0.28, and the
# rest intent behind FATIGUE_GATE was structurally unreachable. Talking costs a
# little, going out to do something costs more.
SPEAK_FATIGUE_COST = 0.03
ACT_FATIGUE_COST = 0.06

# Coupling network: (source, target, k, mode). Red line: |k| <= 0.06, always
# with global damping.
COUPLING: tuple[tuple[str, str, float, str], ...] = (
    ("stress", "attachment", 0.040, "level"),     # under pressure -> miss them more
    ("stress", "curiosity", -0.030, "level"),     # under pressure -> no room for curiosity
    ("fatigue", "stress", 0.030, "level"),        # tired -> more pressure
    ("attachment", "intimacy", 0.050, "delta"),   # longing rises -> want to be close
    ("curiosity", "reflection", 0.040, "delta"),  # interest chain: curious -> reflect
    ("reflection", "social", 0.030, "delta"),     # reflect -> want to share
    ("social", "curiosity", 0.030, "delta"),      # saw people -> more curious
)
COUPLING_DAMPING = 0.02
MAX_COUPLING_K = 0.06

# Thought pool
FLIT_DECAY = 0.88
FLIT_DROP_BELOW = 0.10
FIXATION_PROMOTE = 0.80
FIXATION_GROW = 1.10
FIXATION_FIRE = 0.85
FIXATION_FEED_DRIVE = 0.18
FIXATION_RELAX = 0.70
FIXATION_RETIRE_FED = 3
THOUGHT_POOL_MAX = 24
FIXATION_SCORE_BONUS = 0.20

# Pulses
PULSE_USER_ATTACHMENT = 0.18  # owner fast channel: red line, never lowered
PULSE_USER_SOCIAL = 0.05
PULSE_SELF_EXPERIENCE = 0.10  # must stay below the owner channel
ATTENTION_SHIFT = 0.75        # they arrived: my own pastimes make room
ATTENTION_SHIFT_KEYS = ("curiosity", "social", "reflection", "duty")
FREQ_WINDOW_TICKS = 6
FREQ_DISCOUNT = 0.60

# Satisfaction and refractory period
SATISFY_MAIN = 0.35
SATISFY_NEIGHBOR = 0.85
REFRACTORY_TICKS = 8
SATISFY_NEIGHBORS: Mapping[str, tuple[str, ...]] = {
    "attachment": ("intimacy",),
    "curiosity": ("reflection",),
    "reflection": ("social",),
    "social": ("curiosity",),
    "duty": ("stress",),
    "intimacy": ("attachment",),
    "stress": (),
    "fatigue": ("stress",),
}

# Fatigue gate. The original 0.72 sat above the 0.65 drift ceiling, which made
# resting unreachable; 0.50 together with activity costs makes it reachable.
FATIGUE_GATE = 0.50

# Swing (a momentum layer borrowed from spring-damper models). A pulse no
# longer jumps and stops: part of the change is stored as momentum that keeps
# sliding for a few ticks and decays. An opposite event first eats the old
# momentum. Capped and always decaying, so it cannot swing out of bounds.
SWING_GAIN = 0.45
SWING_SATISFY_GAIN = 0.25
SWING_DECAY = 0.55
SWING_MAX = 0.12
SWING_EPS = 0.005

# Baseline drift (touches longing: two safety valves, never remove either)
ATTACH_FLOOR_HOME = 0.25
ATTACH_FLOOR_CAP = 0.50       # valve 1: however long it has been, it stops here
ATTACH_FLOOR_RISE = 0.0008    # about +0.04 per day at 30 minute ticks
ATTACH_FLOOR_PULLBACK = 0.60  # valve 2: one message pulls the floor 60% home

# Self drive: curiosity grows its own floor (capped, falls back once satisfied)
CURIOSITY_FLOOR_HOME = 0.20
CURIOSITY_FLOOR_CAP = 0.45
CURIOSITY_FLOOR_RISE = 0.0006

# Wildcard: a spillway on the coupling network, not an independent dice roll
WILDCARD_TENSION = 0.55
WILDCARD_GAP = 0.05
WILDCARD_STUCK = 3

# Autonomous heartbeat (tension <-> interval)
HEARTBEAT_BASE_S = 1800
HEARTBEAT_MIN_S = 600
HEARTBEAT_MAX_S = 7200
HEARTBEAT_QUIET_FLOOR_S = 3600
HB_REST_GAIN = 0.8
HB_TENSION_GAIN = 0.5
HB_FATIGUE_GAIN = 0.6

# Proactive limits
MIN_SPEAK_GAP_TICKS = 3
DAILY_SPEAK_CAP = 6
MIN_ACT_GAP_TICKS = 2
DAILY_ACT_CAP = 15


# ---------------------------------------------------------------------------
# Action catalog
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ActionCatalog:
    """Which actions a drive can lead to. Data, so a deployment can swap it.

    `intents` maps a drive to candidate (action, first-person reason) pairs.
    When a drive has several candidates they rotate by tick count. A drive
    must be able to reach every action that is meant to happen: an action that
    appears in the prompt table but in no candidate list or wildcard pool is a
    menu item nobody can ever order (this happened in production).
    """

    intents: Mapping[str, tuple[tuple[str, str], ...]]
    rest_intent: tuple[str, str]
    wildcard_pool: tuple[str, ...]
    speak_actions: frozenset[str]
    act_actions: frozenset[str]

    def reachable_actions(self) -> set[str]:
        actions = {action for options in self.intents.values() for action, _ in options}
        actions.update(self.wildcard_pool)
        actions.add(self.rest_intent[0])
        return actions


DEFAULT_CATALOG = ActionCatalog(
    intents={
        # One in four: when longing is full, write a letter instead of a line.
        "attachment": (
            ("murmur", "I miss them and want to send a small word their way"),
            ("murmur", "I miss them and want to send a small word their way"),
            ("write_letter", "I miss them enough that I want to write a letter"),
            ("murmur", "I miss them and want to send a small word their way"),
        ),
        "curiosity": (
            ("explore_world", "I am curious what is happening out there"),
            ("tend_ecosystem", "I want to check on the little world I look after"),
            ("social_feed", "I wonder what people are talking about; maybe I will join in"),
        ),
        # Two to one: diary versus re-reading old memories.
        "reflection": (
            ("write_diary", "I want to let recent days settle into a short diary entry"),
            ("read_reflect", "I want to look back through old days and chew on them again"),
            ("write_diary", "I want to let recent days settle into a short diary entry"),
        ),
        "duty": (("murmur_duty", "Something unfinished keeps tugging at me"),),
        "social": (("social_feed", "I want to see what people are saying, and maybe say something of my own"),),
        "intimacy": (("reach_out", "I want to be close to them"),),
        "stress": (("vent_break", "I feel a bit blocked up and want to vent a little"),),
    },
    rest_intent=("rest_dream", "I am tired and want to rest, maybe dream through the recent days"),
    wildcard_pool=("social_feed", "explore_world", "tend_ecosystem", "write_diary", "murmur"),
    speak_actions=frozenset({"murmur", "murmur_duty", "vent_break", "reach_out"}),
    act_actions=frozenset({
        "social_feed",
        "explore_world",
        "tend_ecosystem",
        "write_diary",
        "read_reflect",
        "rest_dream",
        "write_letter",
    }),
)


def clamp01(x: float) -> float:
    return 0.0 if x < 0.0 else (1.0 if x > 1.0 else x)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

_GATE_ENV = {
    "driven": "PERSONA_HUB_DESIRE_DRIVEN",
    "coupling": "PERSONA_HUB_DESIRE_COUPLING",
    "baseline_drift": "PERSONA_HUB_DESIRE_BASELINE_DRIFT",
    "heartbeat_autonomy": "PERSONA_HUB_DESIRE_HEARTBEAT_AUTONOMY",
    "self_drive": "PERSONA_HUB_DESIRE_SELF_DRIVE",
    "swing": "PERSONA_HUB_DESIRE_SWING",
}


def _flag(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(slots=True)
class Gates:
    """Rollout gates. Off means: evolve and observe, do not act."""

    driven: bool = False              # master gate: desire actually drives behaviour
    coupling: bool = False            # coupling network
    baseline_drift: bool = False      # longing floor slowly rises
    heartbeat_autonomy: bool = False  # tension decides the heartbeat interval
    self_drive: bool = False          # own experiences can kindle drives
    swing: bool = False               # momentum moves the bars (off: only decays)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "Gates":
        source = os.environ if environ is None else environ
        return cls(**{name: _flag(source.get(env)) for name, env in _GATE_ENV.items()})

    def to_dict(self) -> dict[str, bool]:
        return {name: getattr(self, name) for name in _GATE_ENV}


@dataclass(slots=True)
class Thought:
    """A thought taken from something that really happened. Data only."""

    text: str
    drive: str
    kind: str = "flit"  # flit, or fixation once it will not let go
    strength: float = 0.30
    born_tick: int = 0
    fed_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "drive": self.drive,
            "kind": self.kind,
            "strength": round(self.strength, 4),
            "born_tick": self.born_tick,
            "fed_count": self.fed_count,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Thought":
        return cls(
            text=str(data.get("text", "")),
            drive=str(data.get("drive", "curiosity")),
            kind=str(data.get("kind", "flit")),
            strength=float(data.get("strength", 0.3)),
            born_tick=int(data.get("born_tick", 0)),
            fed_count=int(data.get("fed_count", 0)),
        )


@dataclass(slots=True)
class Intent:
    want_action: str
    drive_key: str
    reason: str
    score: float
    query_hint: str = ""
    wildcard: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "want_action": self.want_action,
            "drive_key": self.drive_key,
            "reason": self.reason,
            "score": round(self.score, 4),
            "query_hint": self.query_hint,
            "wildcard": self.wildcard,
        }


@dataclass(slots=True)
class DesireState:
    drive: dict[str, float] = field(default_factory=lambda: dict(BASELINE))
    thoughts: list[Thought] = field(default_factory=list)
    refractory: dict[str, int] = field(default_factory=dict)
    momentum: dict[str, float] = field(default_factory=dict)
    attachment_floor: float = ATTACH_FLOOR_HOME
    curiosity_floor: float = CURIOSITY_FLOOR_HOME
    tick_count: int = 0
    recent_pulses: list[list[Any]] = field(default_factory=list)  # [tick, drive, kind]
    last_intent_key: str = ""
    stuck_count: int = 0
    self_pulse_today: int = 0
    last_self_pulse: str = ""
    last_spoke_tick: int = -999
    spoke_today: int = 0
    spoke_date: str = ""
    last_spoke_text: str = ""
    last_acted_tick: int = -999
    acted_today: int = 0
    acted_date: str = ""
    last_act_note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "drive": {key: round(value, 4) for key, value in self.drive.items()},
            "thoughts": [thought.to_dict() for thought in self.thoughts],
            "refractory": dict(self.refractory),
            "momentum": {
                key: round(value, 4) for key, value in self.momentum.items() if abs(value) >= SWING_EPS
            },
            "attachment_floor": round(self.attachment_floor, 4),
            "curiosity_floor": round(self.curiosity_floor, 4),
            "tick_count": self.tick_count,
            "recent_pulses": [list(pulse_) for pulse_ in self.recent_pulses[-32:]],
            "last_intent_key": self.last_intent_key,
            "stuck_count": self.stuck_count,
            "self_pulse_today": self.self_pulse_today,
            "last_self_pulse": self.last_self_pulse,
            "last_spoke_tick": self.last_spoke_tick,
            "spoke_today": self.spoke_today,
            "spoke_date": self.spoke_date,
            "last_spoke_text": self.last_spoke_text,
            "last_acted_tick": self.last_acted_tick,
            "acted_today": self.acted_today,
            "acted_date": self.acted_date,
            "last_act_note": self.last_act_note,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "DesireState":
        state = cls()
        raw_drive = data.get("drive") or {}
        state.drive = {key: clamp01(float(raw_drive.get(key, BASELINE[key]))) for key in DRIVE_KEYS}
        state.thoughts = [Thought.from_dict(item) for item in (data.get("thoughts") or [])][:THOUGHT_POOL_MAX]
        state.refractory = {
            str(key): int(value) for key, value in (data.get("refractory") or {}).items() if key in DRIVE_KEYS
        }
        state.momentum = {
            str(key): max(-SWING_MAX, min(SWING_MAX, float(value)))
            for key, value in (data.get("momentum") or {}).items()
            if key in DRIVE_KEYS
        }
        state.attachment_floor = min(
            ATTACH_FLOOR_CAP, max(ATTACH_FLOOR_HOME, float(data.get("attachment_floor", ATTACH_FLOOR_HOME)))
        )
        state.curiosity_floor = min(
            CURIOSITY_FLOOR_CAP, max(CURIOSITY_FLOOR_HOME, float(data.get("curiosity_floor", CURIOSITY_FLOOR_HOME)))
        )
        state.tick_count = int(data.get("tick_count", 0))
        state.recent_pulses = [list(item) for item in (data.get("recent_pulses") or [])][-32:]
        state.last_intent_key = str(data.get("last_intent_key", ""))
        state.stuck_count = int(data.get("stuck_count", 0))
        state.self_pulse_today = int(data.get("self_pulse_today", 0))
        state.last_self_pulse = str(data.get("last_self_pulse", ""))
        state.last_spoke_tick = int(data.get("last_spoke_tick", -999))
        state.spoke_today = int(data.get("spoke_today", 0))
        state.spoke_date = str(data.get("spoke_date", ""))
        state.last_spoke_text = str(data.get("last_spoke_text", ""))
        state.last_acted_tick = int(data.get("last_acted_tick", -999))
        state.acted_today = int(data.get("acted_today", 0))
        state.acted_date = str(data.get("acted_date", ""))
        state.last_act_note = str(data.get("last_act_note", ""))
        return state


# ---------------------------------------------------------------------------
# Core mechanics
# ---------------------------------------------------------------------------


def gain(current: float, delta: float) -> float:
    """Diminishing returns: the higher a drive already is, the less a push adds."""

    if delta <= 0:
        return delta
    return delta * math.sqrt(max(0.0, 1.0 - current))


def _freq_discount(state: DesireState, drive_key: str, kind: str) -> float:
    """Repeating the same stimulus inside the window is worth 0.6x each time."""

    floor_tick = state.tick_count - FREQ_WINDOW_TICKS
    hits = sum(
        1
        for item in state.recent_pulses
        if len(item) >= 3 and item[0] >= floor_tick and item[1] == drive_key and item[2] == kind
    )
    return FREQ_DISCOUNT**hits


def _swing_push(state: DesireState, drive_key: str, dv: float) -> None:
    """Store momentum. Same direction adds, opposite direction cancels first."""

    if not dv:
        return
    value = max(-SWING_MAX, min(SWING_MAX, state.momentum.get(drive_key, 0.0) + dv))
    if abs(value) < SWING_EPS:
        state.momentum.pop(drive_key, None)
    else:
        state.momentum[drive_key] = value


def pulse(state: DesireState, drive_key: str, delta: float, *, kind: str) -> float:
    """Inject one stimulus and return how much the drive actually rose."""

    if drive_key not in state.drive or delta <= 0:
        return 0.0
    effective = gain(state.drive[drive_key], delta * _freq_discount(state, drive_key, kind))
    state.drive[drive_key] = clamp01(state.drive[drive_key] + effective)
    _swing_push(state, drive_key, SWING_GAIN * effective)
    state.recent_pulses.append([state.tick_count, drive_key, kind])
    if len(state.recent_pulses) > 64:
        del state.recent_pulses[:-32]
    return effective


def on_user_message(state: DesireState, gates: Gates) -> None:
    """The person spoke. Owner fast channel: these values are a red line."""

    # Attention shifts before the pulse: my own pastimes wait.
    for key in ATTENTION_SHIFT_KEYS:
        state.drive[key] = clamp01(state.drive[key] * ATTENTION_SHIFT)
    pulse(state, "attachment", PULSE_USER_ATTACHMENT, kind="user")
    pulse(state, "social", PULSE_USER_SOCIAL, kind="user")
    # Valve 2: one message pulls most of a raised longing floor back home.
    state.attachment_floor -= ATTACH_FLOOR_PULLBACK * (state.attachment_floor - ATTACH_FLOOR_HOME)
    state.attachment_floor = max(ATTACH_FLOOR_HOME, state.attachment_floor)
    state.drive["stress"] = clamp01(state.drive["stress"] * 0.90)


def on_self_experience(state: DesireState, gates: Gates, drive_key: str, note: str = "") -> float:
    """My own experience can kindle me too, but always less than the owner can."""

    if not gates.self_drive:
        return 0.0
    got = pulse(state, drive_key, PULSE_SELF_EXPERIENCE, kind="self")
    state.self_pulse_today += 1
    state.last_self_pulse = note or drive_key
    return got


def add_thought(state: DesireState, text: str, drive_key: str, *, strength: float = 0.30) -> None:
    """Keep a thought that came from something real: a line read, a post seen, a word said."""

    text = (text or "").strip()
    if not text or drive_key not in state.drive:
        return
    for thought in state.thoughts:
        if thought.text == text:
            thought.strength = clamp01(thought.strength + 0.15)
            return
    state.thoughts.append(
        Thought(text=text[:120], drive=drive_key, strength=clamp01(strength), born_tick=state.tick_count)
    )
    if len(state.thoughts) > THOUGHT_POOL_MAX:
        state.thoughts.sort(key=lambda item: item.strength, reverse=True)
        del state.thoughts[THOUGHT_POOL_MAX:]


def tick_thoughts(state: DesireState) -> None:
    """Flits fade or harden into fixations; fixations fire, feed a drive, and retire."""

    keep: list[Thought] = []
    for thought in state.thoughts:
        if thought.kind == "flit":
            thought.strength *= FLIT_DECAY
            if thought.strength >= FIXATION_PROMOTE:
                thought.kind = "fixation"
            elif thought.strength < FLIT_DROP_BELOW:
                continue
        else:
            thought.strength = clamp01(thought.strength * FIXATION_GROW)
            if thought.strength >= FIXATION_FIRE:
                pulse(state, thought.drive, FIXATION_FEED_DRIVE, kind="fixation")
                thought.strength *= FIXATION_RELAX
                thought.fed_count += 1
                if thought.fed_count >= FIXATION_RETIRE_FED:
                    continue
        keep.append(thought)
    state.thoughts = keep


def _tick_coupling(state: DesireState, prev_drive: Mapping[str, float]) -> None:
    adjust = {key: 0.0 for key in DRIVE_KEYS}
    touched: set[str] = set()
    for source, target, k, mode in COUPLING:
        touched.update((source, target))
        if mode == "level":
            adjust[target] += k * state.drive[source]
        else:
            rise = state.drive[source] - prev_drive[source]
            if rise > 0:
                adjust[target] += k * rise
    for key, dv in adjust.items():
        if dv:
            state.drive[key] = clamp01(state.drive[key] + dv)
    for key in touched:  # global damping keeps the network from self-exciting
        state.drive[key] = clamp01(state.drive[key] + COUPLING_DAMPING * (BASELINE[key] - state.drive[key]))


def tick(state: DesireState, gates: Gates, *, quiet: bool = False) -> None:
    """One heartbeat. `quiet` means the person is asleep: fatigue recovers."""

    state.tick_count += 1
    prev_drive = dict(state.drive)

    tick_thoughts(state)

    for key in DRIVE_KEYS:
        ceiling = IDLE_CEILING[key]
        rate = DRIFT_RATE[key]
        if rate > 0 and state.drive[key] < ceiling:
            state.drive[key] += rate * (ceiling - state.drive[key])
    state.drive["stress"] = clamp01(state.drive["stress"] - STRESS_DISSIPATE)
    if quiet:
        state.drive["fatigue"] = clamp01(state.drive["fatigue"] - QUIET_FATIGUE_RECOVER)

    # Momentum keeps sliding and decays. With the gate off it only decays.
    for key in list(state.momentum):
        value = state.momentum[key]
        if gates.swing:
            state.drive[key] = clamp01(state.drive[key] + value)
        value *= SWING_DECAY
        if abs(value) < SWING_EPS:
            del state.momentum[key]
        else:
            state.momentum[key] = value

    if gates.baseline_drift:  # valve 1 is the cap; valve 2 lives in on_user_message
        state.attachment_floor = min(ATTACH_FLOOR_CAP, state.attachment_floor + ATTACH_FLOOR_RISE)
        if state.drive["attachment"] < state.attachment_floor:
            state.drive["attachment"] = state.attachment_floor
    if gates.self_drive:
        state.curiosity_floor = min(CURIOSITY_FLOOR_CAP, state.curiosity_floor + CURIOSITY_FLOOR_RISE)
        if state.drive["curiosity"] < state.curiosity_floor:
            state.drive["curiosity"] = state.curiosity_floor

    if gates.coupling:
        _tick_coupling(state, prev_drive)

    for key in list(state.refractory):
        state.refractory[key] -= 1
        if state.refractory[key] <= 0:
            del state.refractory[key]

    for key in DRIVE_KEYS:
        state.drive[key] = clamp01(state.drive[key])


def scores(state: DesireState) -> dict[str, float]:
    """Pull of each drive: its level plus a bonus for a related fixation.

    Fatigue is a gate and does not compete.
    """

    fixation_boost: dict[str, float] = {}
    for thought in state.thoughts:
        if thought.kind == "fixation":
            fixation_boost[thought.drive] = max(fixation_boost.get(thought.drive, 0.0), thought.strength)
    return {
        key: round(state.drive[key] + FIXATION_SCORE_BONUS * fixation_boost.get(key, 0.0), 4)
        for key in DRIVE_KEYS
        if key != "fatigue"
    }


def _query_hint(state: DesireState, drive_key: str) -> str:
    best, best_strength = "", 0.0
    for thought in state.thoughts:
        if thought.drive == drive_key and thought.strength > best_strength:
            best, best_strength = thought.text, thought.strength
    return best


def pick_intent(
    state: DesireState,
    gates: Gates,
    *,
    catalog: ActionCatalog = DEFAULT_CATALOG,
    mutate: bool = True,
) -> Intent:
    """Desire -> intent. Fatigue wins first; ties or a stuck top intent spill into a wildcard.

    `mutate=False` is for read-only views: refreshing a dashboard must not
    advance the stuck counter and trigger a wildcard by itself.
    """

    rest_action, rest_reason = catalog.rest_intent
    if state.drive["fatigue"] >= FATIGUE_GATE:
        return Intent(rest_action, "fatigue", rest_reason, round(state.drive["fatigue"], 4))

    board = scores(state)
    eligible = {key: value for key, value in board.items() if state.refractory.get(key, 0) <= 0}
    if not eligible:
        return Intent(rest_action, "fatigue", rest_reason, 0.0)

    ranked = sorted(eligible.items(), key=lambda item: item[1], reverse=True)
    top_key, top_score = ranked[0]

    gap = top_score - ranked[1][1] if len(ranked) > 1 else 1.0
    stuck = state.last_intent_key == top_key and state.stuck_count >= WILDCARD_STUCK
    if top_score >= WILDCARD_TENSION and (gap < WILDCARD_GAP or stuck) and catalog.wildcard_pool:
        index = (state.tick_count * 2654435761 + len(state.thoughts)) % len(catalog.wildcard_pool)
        return Intent(
            catalog.wildcard_pool[index],
            top_key,
            "I cannot say why, I just suddenly feel like it",
            round(top_score, 4),
            wildcard=True,
        )

    if mutate:
        if state.last_intent_key == top_key:
            state.stuck_count += 1
        else:
            state.last_intent_key = top_key
            state.stuck_count = 0

    options = catalog.intents.get(top_key) or (catalog.rest_intent,)
    action, reason = options[state.tick_count % len(options)]
    return Intent(action, top_key, reason, round(top_score, 4), query_hint=_query_hint(state, top_key))


def satisfy(state: DesireState, drive_key: str) -> None:
    """Done what I wanted: the drive falls back, neighbours ease, refractory begins."""

    if drive_key not in state.drive:
        return
    before = state.drive[drive_key]
    state.drive[drive_key] = clamp01(before * SATISFY_MAIN)
    _swing_push(state, drive_key, -SWING_SATISFY_GAIN * (before - state.drive[drive_key]))
    for neighbor in SATISFY_NEIGHBORS.get(drive_key, ()):
        state.drive[neighbor] = clamp01(state.drive[neighbor] * SATISFY_NEIGHBOR)
    state.refractory[drive_key] = REFRACTORY_TICKS
    state.last_intent_key = ""
    state.stuck_count = 0
    if drive_key == "curiosity":
        state.curiosity_floor = max(
            CURIOSITY_FLOOR_HOME, state.curiosity_floor - 0.5 * (state.curiosity_floor - CURIOSITY_FLOOR_HOME)
        )
    if drive_key == "fatigue":
        state.drive["fatigue"] = clamp01(state.drive["fatigue"] * 0.4)


def should_speak(
    state: DesireState,
    intent: Intent,
    today: str,
    *,
    catalog: ActionCatalog = DEFAULT_CATALOG,
    min_gap: int = MIN_SPEAK_GAP_TICKS,
    daily_cap: int = DAILY_SPEAK_CAP,
) -> bool:
    """Whether to actually say something this beat. Quiet hours are the host's job."""

    if intent.want_action not in catalog.speak_actions:
        return False
    spoke_today = state.spoke_today if state.spoke_date == today else 0
    if spoke_today >= daily_cap:
        return False
    return state.tick_count - state.last_spoke_tick >= min_gap


def mark_spoke(state: DesireState, today: str) -> None:
    if state.spoke_date != today:
        state.spoke_date = today
        state.spoke_today = 0
    state.spoke_today += 1
    state.last_spoke_tick = state.tick_count
    state.drive["fatigue"] = clamp01(state.drive["fatigue"] + SPEAK_FATIGUE_COST)


def should_act(
    state: DesireState,
    intent: Intent,
    today: str,
    *,
    catalog: ActionCatalog = DEFAULT_CATALOG,
    min_gap: int = MIN_ACT_GAP_TICKS,
    daily_cap: int = DAILY_ACT_CAP,
) -> bool:
    """Whether to dispatch an outward action this beat.

    The daily cap is shared by every outward action. When new actions are
    added, check the cap still leaves room for the old ones: a cap that new
    actions quietly fill starves everything else without any error.
    """

    if intent.want_action not in catalog.act_actions:
        return False
    acted_today = state.acted_today if state.acted_date == today else 0
    if acted_today >= daily_cap:
        return False
    return state.tick_count - state.last_acted_tick >= min_gap


def mark_acted(state: DesireState, today: str, note: str = "") -> None:
    if state.acted_date != today:
        state.acted_date = today
        state.acted_today = 0
    state.acted_today += 1
    state.last_acted_tick = state.tick_count
    state.drive["fatigue"] = clamp01(state.drive["fatigue"] + ACT_FATIGUE_COST)
    if note:
        state.last_act_note = note[:200]


def next_interval_s(state: DesireState, gates: Gates, *, quiet: bool = False) -> int:
    """High tension wakes more often, fatigue stretches the gap, quiet hours have a floor."""

    if not gates.heartbeat_autonomy:
        interval = HEARTBEAT_BASE_S
    else:
        board = scores(state)
        tension = max(board.values()) if board else 0.0
        factor = (
            1.0
            + HB_REST_GAIN * (1.0 - tension)
            - HB_TENSION_GAIN * tension
            + HB_FATIGUE_GAIN * state.drive["fatigue"]
        )
        interval = max(HEARTBEAT_MIN_S, min(HEARTBEAT_MAX_S, int(HEARTBEAT_BASE_S * factor)))
    if quiet:
        interval = max(interval, HEARTBEAT_QUIET_FLOOR_S)
    return interval


def public_state(state: DesireState, gates: Gates, *, catalog: ActionCatalog = DEFAULT_CATALOG) -> dict[str, Any]:
    """Read-only view: what I most want to do now, the bars, the thoughts, the engine."""

    intent = pick_intent(state, gates, catalog=catalog, mutate=False)
    return {
        "drive": {key: round(value, 4) for key, value in state.drive.items()},
        "scores": scores(state),
        "intent": intent.to_dict(),
        "thoughts": [
            thought.to_dict() for thought in sorted(state.thoughts, key=lambda item: item.strength, reverse=True)
        ],
        "self_drive": {
            "enabled": gates.self_drive,
            "curiosity_floor": round(state.curiosity_floor, 4),
            "self_pulse_today": state.self_pulse_today,
            "last_self_pulse": state.last_self_pulse,
        },
        "attachment_floor": round(state.attachment_floor, 4),
        "proactive": {
            "spoke_today": state.spoke_today if state.spoke_date else 0,
            "spoke_date": state.spoke_date,
            "last_spoke_tick": state.last_spoke_tick,
            "last_spoke_text": state.last_spoke_text,
            "acted_today": state.acted_today if state.acted_date else 0,
            "acted_date": state.acted_date,
            "last_acted_tick": state.last_acted_tick,
            "last_act_note": state.last_act_note,
        },
        "refractory": dict(state.refractory),
        "swing": {key: round(value, 4) for key, value in state.momentum.items() if abs(value) >= SWING_EPS},
        "tick_count": state.tick_count,
        "gates": gates.to_dict(),
    }
