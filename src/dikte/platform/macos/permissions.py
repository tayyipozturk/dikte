"""The macOS privacy permissions Dikte needs, and how to ask for them.

Microphone      – to hear you.
Accessibility   – to press Cmd+V / type into other apps (also allows the hotkey tap).
Input Monitoring – only needed if the hotkey tap cannot be created without it.
"""

from __future__ import annotations

import ctypes
import logging
import subprocess
from dataclasses import dataclass
from typing import Callable

import AVFoundation
import Quartz
from ApplicationServices import AXIsProcessTrusted, AXIsProcessTrustedWithOptions, kAXTrustedCheckOptionPrompt

log = logging.getLogger(__name__)

_MIC_STATUS = {0: "undetermined", 1: "restricted", 2: "denied", 3: "granted"}
_PANE = "x-apple.systempreferences:com.apple.preference.security?"
SETTINGS_URLS = {
    "microphone": _PANE + "Privacy_Microphone",
    "accessibility": _PANE + "Privacy_Accessibility",
    "input_monitoring": _PANE + "Privacy_ListenEvent",
}
_carbon: ctypes.CDLL | None = None


@dataclass(frozen=True)
class Permissions:
    microphone: str
    accessibility: bool
    input_monitoring: bool


def check() -> Permissions:
    status = AVFoundation.AVCaptureDevice.authorizationStatusForMediaType_(AVFoundation.AVMediaTypeAudio)
    return Permissions(
        microphone=_MIC_STATUS.get(int(status), "undetermined"),
        accessibility=bool(AXIsProcessTrusted()),
        input_monitoring=bool(Quartz.CGPreflightListenEventAccess()),
    )


def request_microphone(done: Callable[[bool], None] | None = None) -> None:
    def handler(granted: bool) -> None:
        log.info("Microphone permission %s", "granted" if granted else "denied")
        if done:
            done(bool(granted))

    AVFoundation.AVCaptureDevice.requestAccessForMediaType_completionHandler_(AVFoundation.AVMediaTypeAudio, handler)


def request_accessibility() -> bool:
    return bool(AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: True}))


def request_input_monitoring() -> bool:
    return bool(Quartz.CGRequestListenEventAccess())


def open_settings(kind: str) -> None:
    subprocess.Popen(["open", SETTINGS_URLS[kind]])  # noqa: S603,S607 - fixed arguments


def secure_input_enabled() -> bool:
    """True while some app holds Secure Keyboard Entry (password field, secure terminal)."""
    global _carbon
    try:
        if _carbon is None:
            _carbon = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/Carbon.framework/Carbon")
            _carbon.IsSecureEventInputEnabled.restype = ctypes.c_bool
        return bool(_carbon.IsSecureEventInputEnabled())
    except (OSError, AttributeError):
        return False
