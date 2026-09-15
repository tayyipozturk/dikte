"""Small floating pill at the bottom of the screen: recording / transcribing.

It is a non-activating panel that ignores the mouse, so it never takes focus
away from the app you are dictating into. Main thread only.
"""

from __future__ import annotations

from AppKit import (
    NSBackingStoreBuffered,
    NSColor,
    NSEvent,
    NSFont,
    NSFontWeightMedium,
    NSPanel,
    NSScreen,
    NSStatusWindowLevel,
    NSTextField,
    NSView,
    NSVisualEffectBlendingModeBehindWindow,
    NSVisualEffectMaterialHUDWindow,
    NSVisualEffectStateActive,
    NSVisualEffectView,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSWindowCollectionBehaviorIgnoresCycle,
    NSWindowCollectionBehaviorStationary,
    NSWindowStyleMaskBorderless,
    NSWindowStyleMaskNonactivatingPanel,
)
from Foundation import NSMakeRect

_WIDTH, _HEIGHT = 290.0, 40.0
_BARS = 8
_BOTTOM_OFFSET = 90.0


class Hud:
    def __init__(self) -> None:
        rect = NSMakeRect(0, 0, _WIDTH, _HEIGHT)
        panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel, NSBackingStoreBuffered, False
        )
        panel.setLevel_(NSStatusWindowLevel)
        panel.setOpaque_(False)
        panel.setBackgroundColor_(NSColor.clearColor())
        panel.setHasShadow_(True)
        panel.setIgnoresMouseEvents_(True)
        panel.setHidesOnDeactivate_(False)
        panel.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces | NSWindowCollectionBehaviorFullScreenAuxiliary
            | NSWindowCollectionBehaviorStationary | NSWindowCollectionBehaviorIgnoresCycle
        )
        background = NSVisualEffectView.alloc().initWithFrame_(rect)
        background.setMaterial_(NSVisualEffectMaterialHUDWindow)
        background.setBlendingMode_(NSVisualEffectBlendingModeBehindWindow)
        background.setState_(NSVisualEffectStateActive)
        background.setWantsLayer_(True)
        background.layer().setCornerRadius_(_HEIGHT / 2)
        background.layer().setMasksToBounds_(True)
        panel.setContentView_(background)

        self._dot = NSView.alloc().initWithFrame_(NSMakeRect(16, (_HEIGHT - 10) / 2, 10, 10))
        self._dot.setWantsLayer_(True)
        self._dot.layer().setCornerRadius_(5)
        background.addSubview_(self._dot)

        self._label = NSTextField.labelWithString_("")
        self._label.setFrame_(NSMakeRect(34, (_HEIGHT - 18) / 2, _WIDTH - 34 - 82, 18))
        self._label.setFont_(NSFont.monospacedDigitSystemFontOfSize_weight_(13, NSFontWeightMedium))
        self._label.setTextColor_(NSColor.labelColor())
        background.addSubview_(self._label)

        self._bars = []
        for i in range(_BARS):
            bar = NSView.alloc().initWithFrame_(NSMakeRect(_WIDTH - 76 + i * 7, (_HEIGHT - 14) / 2, 4, 14))
            bar.setWantsLayer_(True)
            bar.layer().setCornerRadius_(2)
            background.addSubview_(bar)
            self._bars.append(bar)
        self._panel = panel

    def show(self, text: str, color: NSColor, level: float | None) -> None:
        """level: 0..1 for the meter, or None to hide the meter."""
        self._label.setStringValue_(text)
        self._dot.layer().setBackgroundColor_(color.CGColor())
        for i, bar in enumerate(self._bars):
            lit = level is not None and level > i / _BARS
            alpha = 0.0 if level is None else (0.9 if lit else 0.18)
            bar.layer().setBackgroundColor_(NSColor.labelColor().colorWithAlphaComponent_(alpha).CGColor())
        if not self._panel.isVisible():
            self._place()
            self._panel.orderFrontRegardless()

    def hide(self) -> None:
        if self._panel.isVisible():
            self._panel.orderOut_(None)

    def _place(self) -> None:
        """Bottom centre of the screen the mouse is on."""
        mouse = NSEvent.mouseLocation()
        screens = list(NSScreen.screens() or [])
        screen = next(
            (s for s in screens
             if s.frame().origin.x <= mouse.x < s.frame().origin.x + s.frame().size.width
             and s.frame().origin.y <= mouse.y < s.frame().origin.y + s.frame().size.height),
            NSScreen.mainScreen(),
        )
        if screen is None:
            return
        visible = screen.visibleFrame()
        x = visible.origin.x + (visible.size.width - _WIDTH) / 2
        y = visible.origin.y + _BOTTOM_OFFSET
        self._panel.setFrameOrigin_((x, y))
