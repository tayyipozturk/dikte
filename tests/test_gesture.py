from dikte.gesture import Action as A
from dikte.gesture import GestureConfig, HotkeyGesture


def make(mode="hold"):
    return HotkeyGesture(GestureConfig(mode=mode, hold_s=0.2, double_tap_s=0.35, combo_window_s=1.0))


def test_push_to_talk():
    g = make()
    assert g.key_down(0.0) == [A.PREPARE]
    assert g.tick(0.1) == []
    assert g.tick(0.25) == [A.BEGIN]
    assert g.tick(0.3) == []
    assert g.key_up(2.0) == [A.FINISH]


def test_single_tap_cancels_quietly():
    g = make()
    g.key_down(0.0)
    assert g.key_up(0.1) == [A.CANCEL]


def test_double_tap_starts_hands_free_and_tap_stops():
    g = make()
    g.key_down(0.0)
    g.key_up(0.1)
    assert g.key_down(0.3) == [A.PREPARE]
    assert g.key_up(0.4) == [A.HANDS_FREE]
    assert g.hands_free
    assert g.tick(5.0) == []
    assert g.key_down(6.0) == []
    assert g.key_up(6.1) == [A.FINISH]
    assert not g.hands_free


def test_slow_second_tap_is_not_double_tap():
    g = make()
    g.key_down(0.0)
    g.key_up(0.1)
    g.key_down(0.9)
    assert g.key_up(1.0) == [A.CANCEL]


def test_shortcut_cancels_and_release_is_ignored():
    g = make()
    g.key_down(0.0)
    assert g.other_key(0.05) == [A.CANCEL]
    assert g.tick(0.5) == []
    assert g.key_up(0.6) == []
    # a shortcut does not count as the first tap of a double tap
    g.key_down(0.7)
    assert g.key_up(0.8) == [A.CANCEL]


def test_keys_late_in_a_long_hold_are_ignored():
    g = make()
    g.key_down(0.0)
    g.tick(0.3)
    assert g.other_key(0.5) == [A.CANCEL]  # within combo window
    g2 = make()
    g2.key_down(0.0)
    g2.tick(0.3)
    assert g2.other_key(3.0) == []
    assert g2.key_up(4.0) == [A.FINISH]


def test_escape_cancels_hold_and_hands_free():
    g = make()
    g.key_down(0.0)
    g.tick(0.3)
    assert g.escape(1.0) == [A.CANCEL]
    assert g.key_up(1.2) == []
    g2 = make("toggle")
    g2.key_down(0.0)
    g2.key_up(0.1)
    assert g2.escape(2.0) == [A.CANCEL]
    assert not g2.hands_free


def test_toggle_mode_tap_and_hold():
    g = make("toggle")
    g.key_down(0.0)
    assert g.key_up(0.1) == [A.HANDS_FREE]
    g.key_down(3.0)
    assert g.key_up(3.1) == [A.FINISH]
    g.key_down(5.0)
    assert g.tick(5.3) == [A.BEGIN]
    assert g.key_up(6.0) == [A.FINISH]


def test_voice_mode_tap_toggles_listening():
    g = make("voice")
    g.key_down(0.0)
    assert g.key_up(0.1) == [A.CANCEL, A.TOGGLE_LISTEN]


def test_shortcut_during_hands_free_is_ignored():
    g = make("toggle")
    g.key_down(0.0)
    g.key_up(0.1)
    g.key_down(1.0)
    assert g.other_key(1.1) == []
    assert g.key_up(1.2) == []
    assert g.hands_free


def test_external_stop_resets_hands_free():
    g = make("toggle")
    g.key_down(0.0)
    g.key_up(0.1)
    g.end_hands_free()
    assert not g.hands_free
    assert g.key_down(1.0) == [A.PREPARE]
