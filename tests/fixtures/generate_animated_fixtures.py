"""Generate test fixtures for animated GIF and animated WebP.

Run once inside the Docker test environment:
docker compose run --rm -v "$PWD":/src -w /src test \
    python tests/fixtures/generate_animated_fixtures.py
"""

from __future__ import annotations

from pathlib import Path

import pyvips

FIXTURES_DIR = Path(__file__).parent


def generate_animated_fixtures() -> None:
    width = 32
    height = 32
    # 5 distinct frames with different colors
    colors = [
        [255, 0, 0],
        [0, 255, 0],
        [0, 0, 255],
        [255, 255, 0],
        [255, 0, 255],
    ]
    frames = [(pyvips.Image.black(width, height, bands=3) + col).cast("uchar") for col in colors]
    # Join into a vertical strip
    strip = pyvips.Image.arrayjoin(frames, across=1)
    strip = strip.copy()
    strip.set_type(pyvips.GValue.gint_type, "page-height", height)
    # Non-uniform delays in milliseconds (or centiseconds depending on format):
    # e.g., [100, 200, 150, 300, 250] ms
    delays = [100, 200, 150, 300, 250]
    strip.set_type(pyvips.GValue.array_int_type, "delay", delays)
    strip.set_type(pyvips.GValue.gint_type, "loop", 0)

    gif_path = FIXTURES_DIR / "animated.gif"
    strip.write_to_file(str(gif_path))
    print(f"Generated {gif_path}")

    webp_path = FIXTURES_DIR / "animated.webp"
    strip.write_to_file(str(webp_path))
    print(f"Generated {webp_path}")


def main() -> None:
    generate_animated_fixtures()


if __name__ == "__main__":
    main()
