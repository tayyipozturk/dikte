"""X keysyms for the keys Dikte sends (pure data, safe to import anywhere)."""

from __future__ import annotations

CONTROL_L = 0xFFE3
SHIFT_L = 0xFFE1
INSERT = 0xFF63
RETURN = 0xFF0D
TAB = 0xFF09
V = 0x76

# modifiers first, the key last; released in reverse order
PASTE_COMBOS = {
    "ctrl_v": (CONTROL_L, V),
    "ctrl_shift_v": (CONTROL_L, SHIFT_L, V),
    "shift_insert": (SHIFT_L, INSERT),
}
PASTE_KEY_NAMES = {  # for xdotool on X11
    "ctrl_v": "ctrl+v",
    "ctrl_shift_v": "ctrl+shift+v",
    "shift_insert": "shift+Insert",
}


def char_to_keysym(char: str) -> int:
    """Latin-1 maps one-to-one; everything else uses the Unicode keysym range.

    Note: a compositor can only deliver a keysym that exists in the active
    keyboard layout, so non-ASCII typing is unreliable on Wayland. Dikte
    therefore pastes instead of typing whenever it can.
    """
    if char == "\n":
        return RETURN
    if char == "\t":
        return TAB
    code = ord(char)
    if 0x20 <= code <= 0x7E or 0xA0 <= code <= 0xFF:
        return code
    return 0x01000000 + code
