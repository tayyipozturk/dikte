"""Render Dikte's app icon (white microphone on a blue-violet tile) as an .icns file.

Usage: python scripts/make_icon.py OUTPUT.icns
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

from AppKit import (
    NSBezierPath,
    NSBitmapImageRep,
    NSColor,
    NSCompositingOperationSourceAtop,
    NSCompositingOperationSourceOver,
    NSDeviceRGBColorSpace,
    NSFontWeightSemibold,
    NSGradient,
    NSGraphicsContext,
    NSImage,
    NSImageSymbolConfiguration,
    NSPNGFileType,
    NSRectFillUsingOperation,
)
from Foundation import NSMakeRect, NSZeroRect


def _white_symbol(point_size: float) -> NSImage:
    config = NSImageSymbolConfiguration.configurationWithPointSize_weight_(point_size, NSFontWeightSemibold)
    symbol = NSImage.imageWithSystemSymbolName_accessibilityDescription_("mic.fill", None)
    symbol = symbol.imageWithSymbolConfiguration_(config)
    size = symbol.size()
    tinted = NSImage.alloc().initWithSize_(size)
    tinted.lockFocus()
    symbol.drawInRect_fromRect_operation_fraction_(NSMakeRect(0, 0, size.width, size.height), NSZeroRect,
                                                   NSCompositingOperationSourceOver, 1.0)
    NSColor.whiteColor().set()
    NSRectFillUsingOperation(NSMakeRect(0, 0, size.width, size.height), NSCompositingOperationSourceAtop)
    tinted.unlockFocus()
    return tinted


def render_png(pixels: int) -> bytes:
    rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, pixels, pixels, 8, 4, True, False, NSDeviceRGBColorSpace, 0, 0
    )
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.setCurrentContext_(NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep))
    inset = pixels * 0.09
    tile = NSMakeRect(inset, inset, pixels - 2 * inset, pixels - 2 * inset)
    path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(tile, pixels * 0.19, pixels * 0.19)
    gradient = NSGradient.alloc().initWithStartingColor_endingColor_(
        NSColor.colorWithSRGBRed_green_blue_alpha_(0.18, 0.47, 0.98, 1.0),
        NSColor.colorWithSRGBRed_green_blue_alpha_(0.46, 0.26, 0.88, 1.0),
    )
    gradient.drawInBezierPath_angle_(path, -90)
    symbol = _white_symbol(pixels * 0.40)
    size = symbol.size()
    symbol.drawInRect_fromRect_operation_fraction_(
        NSMakeRect((pixels - size.width) / 2, (pixels - size.height) / 2, size.width, size.height),
        NSZeroRect, NSCompositingOperationSourceOver, 1.0,
    )
    NSGraphicsContext.restoreGraphicsState()
    return bytes(rep.representationUsingType_properties_(NSPNGFileType, {}))


def main(output: str) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "Dikte.iconset"
        iconset.mkdir()
        for points in (16, 32, 128, 256, 512):
            (iconset / f"icon_{points}x{points}.png").write_bytes(render_png(points))
            (iconset / f"icon_{points}x{points}@2x.png").write_bytes(render_png(points * 2))
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", output], check=True)  # noqa: S603,S607


if __name__ == "__main__":
    main(sys.argv[1])
