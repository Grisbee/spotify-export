"""Draws the app icon: green playlist (three lines + a note) and download tray on black.

Writes launcher/AppIcon.icns (macOS bundle icon) and static/icon.png (favicon).
Needs Pillow; macOS `iconutil` builds the .icns.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
SIZE = 1024
SCALE = 4  # draw oversized, then downsample for smooth edges
BACKGROUND = (0, 0, 0, 255)
GREEN = (30, 215, 96, 255)  # #1ED760, same as the web UI accent


def draw() -> Image.Image:
    s = SIZE * SCALE
    u = s / 1024  # one unit on the 1024 design grid

    # macOS icon grid: an 824 px rounded square centred in the 1024 canvas.
    icon = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(icon)
    d.rounded_rectangle([100 * u, 100 * u, 924 * u, 924 * u], radius=185 * u, fill=BACKGROUND)
    bar = 46 * u
    # Playlist lines, shortest last.
    for i, right in enumerate((640, 640, 520)):
        y = (300 + i * 105) * u
        d.rounded_rectangle([250 * u, y, right * u, y + bar], radius=bar / 2, fill=GREEN)
    # Eighth note to the right of the lines.
    d.ellipse([640 * u, 470 * u, 750 * u, 560 * u], fill=GREEN)
    d.rounded_rectangle([712 * u, 260 * u, 750 * u, 520 * u], radius=19 * u, fill=GREEN)
    d.polygon([(750 * u, 260 * u), (820 * u, 300 * u), (820 * u, 360 * u), (750 * u, 320 * u)], fill=GREEN)

    # Download arrow into a tray.
    cx = 512 * u
    d.rounded_rectangle([cx - 23 * u, 600 * u, cx + 23 * u, 735 * u], radius=23 * u, fill=GREEN)
    d.polygon([(cx - 85 * u, 700 * u), (cx + 85 * u, 700 * u), (cx, 790 * u)], fill=GREEN)
    tray = [(330 * u, 740 * u), (330 * u, 820 * u), (694 * u, 820 * u), (694 * u, 740 * u)]
    d.line(tray, fill=GREEN, width=round(bar), joint="curve")
    for x, y in (tray[0], tray[-1]):
        d.ellipse([x - bar / 2, y - bar / 2, x + bar / 2, y + bar / 2], fill=GREEN)

    return icon.resize((SIZE, SIZE), Image.LANCZOS)


def main() -> None:
    icon = draw()
    icon.resize((256, 256), Image.LANCZOS).save(HERE.parent / "static" / "icon.png")

    tmp = Path(tempfile.mkdtemp())
    try:
        iconset = tmp / "AppIcon.iconset"
        iconset.mkdir()
        for px in (16, 32, 128, 256, 512):
            icon.resize((px, px), Image.LANCZOS).save(iconset / f"icon_{px}x{px}.png")
            icon.resize((px * 2, px * 2), Image.LANCZOS).save(iconset / f"icon_{px}x{px}@2x.png")
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(HERE / "AppIcon.icns")], check=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"Zapisano {HERE / 'AppIcon.icns'} i static/icon.png")


if __name__ == "__main__":
    main()
