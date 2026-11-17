from __future__ import annotations

from persona_hub.desire import kernel
from persona_hub.desire.kernel import DEFAULT_CATALOG, DesireState, Gates

ALL_ON = Gates(
    driven=True,
    coupling=True,
    baseline_drift=True,
    heartbeat_autonomy=True,
    self_drive=True,
    swing=True,
)


def run_ticks(state: DesireState, gates: Gates, count: int, *, quiet: bool = False) -> None:
    for _ in range(count):
        kernel.tick(state, gates, quiet=quiet)


def test_gates_default_off_and_read_from_env():
    assert not any(Gates().to_dict().values())
    gates = Gates.from_env({"PERSONA_HUB_DESIRE_DRIVEN": "1", "PERSONA_HUB_DESIRE_SWING": "yes"})
    assert gates.driven and gates.swing
    assert not gates.coupling


def test_owner_fast_channel_values_are_a_red_line():
    assert kernel.PULSE_USER_ATTACHMENT == 0.18
    assert kernel.PULSE_SELF_EXPERIENCE < kernel.PULSE_USER_ATTACHMENT


def test_user_message_shifts_attention_and_raises_attachment():
    state = DesireState()
    state.drive["curiosity"] = 0.6
    before = state.drive["attachment"]
    kernel.on_user_message(state, Gates())
    assert state.drive["attachment"] > before
    assert state.drive["curiosity"] < 0.6


def test_self_experience_needs_its_gate_and_is_weaker_than_the_owner():
    state = DesireState()
    assert kernel.on_self_experience(state, Gates(), "curiosity") == 0.0
    self_gain = kernel.on_self_experience(state, Gates(self_drive=True), "attachment")
    owner_state = DesireState()
    before = owner_state.drive["attachment"]
    kernel.on_user_message(owner_state, Gates())
    assert self_gain < owner_state.drive["attachment"] - before


def test_repeated_stimulus_is_discounted():
    state = DesireState()
    first = kernel.pulse(state, "curiosity", 0.1, kind="read")
    second = kernel.pulse(state, "curiosity", 0.1, kind="read")
    assert second < first * 0.7


def test_drives_stay_bounded_with_every_gate_on():
    state = DesireState()
    for index in range(2000):
        if index % 7 == 0:
            kernel.pulse(state, "stress", 0.3, kind="event")
        if index % 11 == 0:
            kernel.add_thought(state, f"thought {index % 5}", "curiosity", strength=0.9)
        kernel.tick(state, ALL_ON)
    assert all(0.0 <= value <= 1.0 for value in state.drive.values())
    assert all(abs(value) <= kernel.SWING_MAX for value in state.momentum.values())
    assert len(state.thoughts) <= kernel.THOUGHT_POOL_MAX


def test_coupling_coefficients_respect_the_red_line():
    assert all(abs(k) <= kernel.MAX_COUPLING_K for _, _, k, _ in kernel.COUPLING)


def test_baseline_drift_is_capped_and_one_message_pulls_it_home():
    state = DesireState()
    run_ticks(state, Gates(baseline_drift=True), 5000)
    assert state.attachment_floor == kernel.ATTACH_FLOOR_CAP
    kernel.on_user_message(state, Gates())
    pulled = state.attachment_floor
    assert pulled < kernel.ATTACH_FLOOR_CAP
    expected = kernel.ATTACH_FLOOR_CAP - kernel.ATTACH_FLOOR_PULLBACK * (
        kernel.ATTACH_FLOOR_CAP - kernel.ATTACH_FLOOR_HOME
    )
    assert abs(pulled - expected) < 1e-9


def test_baseline_drift_does_nothing_with_its_gate_off():
    state = DesireState()
    run_ticks(state, Gates(), 500)
    assert state.attachment_floor == kernel.ATTACH_FLOOR_HOME


def test_swing_keeps_moving_after_a_pulse_and_settles():
    state = DesireState()
    kernel.pulse(state, "curiosity", 0.3, kind="event")
    assert state.momentum["curiosity"] > 0
    after_pulse = state.drive["curiosity"]
    kernel.tick(state, Gates(swing=True))
    # Drift alone would add a little too; momentum must add more than that.
    drift_only = after_pulse + kernel.DRIFT_RATE["curiosity"] * (kernel.IDLE_CEILING["curiosity"] - after_pulse)
    assert state.drive["curiosity"] > drift_only
    run_ticks(state, Gates(swing=True), 20)
    assert "curiosity" not in state.momentum


def test_swing_gate_off_only_decays():
    state = DesireState()
    kernel.pulse(state, "curiosity", 0.3, kind="event")
    after_pulse = state.drive["curiosity"]
    kernel.tick(state, Gates())
    drift_only = after_pulse + kernel.DRIFT_RATE["curiosity"] * (kernel.IDLE_CEILING["curiosity"] - after_pulse)
    assert abs(state.drive["curiosity"] - drift_only) < 1e-9
    assert 0 < state.momentum["curiosity"] < kernel.SWING_MAX


def test_opposite_event_eats_momentum_first():
    state = DesireState()
    kernel.pulse(state, "curiosity", 0.3, kind="event")
    positive = state.momentum["curiosity"]
    kernel.satisfy(state, "curiosity")
    assert state.momentum.get("curiosity", 0.0) < positive


def test_activity_makes_rest_reachable():
    """Before activity cost fatigue, the rest intent could never be chosen."""

    state = DesireState()
    today = "2026-01-01"
    for _ in range(6):
        kernel.mark_acted(state, today)
    for _ in range(2):
        kernel.mark_spoke(state, today)
    assert state.drive["fatigue"] >= kernel.FATIGUE_GATE
    intent = kernel.pick_intent(state, Gates())
    assert intent.want_action == DEFAULT_CATALOG.rest_intent[0]


def test_quiet_hours_recover_fatigue():
    state = DesireState()
    state.drive["fatigue"] = 0.6
    kernel.tick(state, Gates(), quiet=True)
    assert state.drive["fatigue"] < 0.6


def test_every_action_with_a_purpose_is_reachable():
    reachable = DEFAULT_CATALOG.reachable_actions()
    assert DEFAULT_CATALOG.act_actions <= reachable
    assert DEFAULT_CATALOG.speak_actions <= reachable


def test_reflection_rotates_between_diary_and_rereading():
    state = DesireState()
    state.drive.update({key: 0.0 for key in state.drive})
    state.drive["reflection"] = 0.4
    seen = set()
    for tick_count in range(6):
        state.tick_count = tick_count
        seen.add(kernel.pick_intent(state, Gates(), mutate=False).want_action)
    assert seen == {"write_diary", "read_reflect"}


def test_satisfy_starts_refractory_and_other_drives_take_over():
    state = DesireState()
    state.drive.update({key: 0.1 for key in state.drive})
    state.drive["curiosity"] = 0.45
    state.drive["social"] = 0.30
    assert kernel.pick_intent(state, Gates()).drive_key == "curiosity"
    kernel.satisfy(state, "curiosity")
    assert state.refractory["curiosity"] == kernel.REFRACTORY_TICKS
    assert kernel.pick_intent(state, Gates()).drive_key == "social"


def test_wildcard_is_deterministic_when_top_drives_tie():
    state = DesireState()
    state.drive.update({key: 0.1 for key in state.drive})
    state.drive["curiosity"] = 0.70
    state.drive["social"] = 0.69
    first = kernel.pick_intent(state, Gates(), mutate=False)
    second = kernel.pick_intent(state, Gates(), mutate=False)
    assert first.wildcard and first == second
    assert first.want_action in DEFAULT_CATALOG.wildcard_pool


def test_read_only_view_does_not_advance_stuck_counter():
    state = DesireState()
    state.drive["attachment"] = 0.5
    for _ in range(10):
        kernel.public_state(state, Gates())
    assert state.stuck_count == 0


def test_speak_and_act_limits():
    state = DesireState()
    today = "2026-01-01"
    speak = kernel.Intent("murmur", "attachment", "", 0.5)
    act = kernel.Intent("write_diary", "reflection", "", 0.5)
    assert kernel.should_speak(state, speak, today)
    assert not kernel.should_speak(state, act, today)
    kernel.mark_spoke(state, today)
    assert not kernel.should_speak(state, speak, today)  # gap
    state.tick_count += kernel.MIN_SPEAK_GAP_TICKS
    assert kernel.should_speak(state, speak, today)
    state.spoke_today = kernel.DAILY_SPEAK_CAP
    assert not kernel.should_speak(state, speak, today)
    assert kernel.should_speak(state, speak, "2026-01-02")  # new day resets
    assert kernel.should_act(state, act, today)
    state.acted_today, state.acted_date = kernel.DAILY_ACT_CAP, today
    assert not kernel.should_act(state, act, today)


def test_heartbeat_interval_follows_tension_and_respects_quiet_floor():
    calm = DesireState()
    calm.drive.update({key: 0.05 for key in calm.drive})
    tense = DesireState()
    tense.drive.update({key: 0.05 for key in tense.drive})
    tense.drive["attachment"] = 0.9
    gates = Gates(heartbeat_autonomy=True)
    assert kernel.next_interval_s(tense, gates) < kernel.next_interval_s(calm, gates)
    assert kernel.next_interval_s(tense, gates, quiet=True) >= kernel.HEARTBEAT_QUIET_FLOOR_S
    assert kernel.next_interval_s(tense, Gates()) == kernel.HEARTBEAT_BASE_S


def test_state_round_trip_and_tolerates_old_snapshots():
    state = DesireState()
    kernel.add_thought(state, "a line I read", "curiosity", strength=0.6)
    kernel.pulse(state, "curiosity", 0.2, kind="read")
    restored = DesireState.from_dict(state.to_dict())
    assert restored.to_dict() == state.to_dict()
    old = DesireState.from_dict({"drive": {"attachment": 0.4}, "attachment_floor": 9})
    assert old.drive["attachment"] == 0.4
    assert old.attachment_floor == kernel.ATTACH_FLOOR_CAP
    assert old.momentum == {}
