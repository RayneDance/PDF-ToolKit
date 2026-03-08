from __future__ import annotations

from argparse import ArgumentParser
from pathlib import Path

from PIL import Image, ImageDraw


BACKGROUND = "#081218"
BORDER = "#224a5e"
CARD = "#102532"
BLUE = "#11b8f5"
YELLOW = "#ffd166"
CORAL = "#ff6f61"
GREEN = "#72e0a0"
WHITE = "#e8f6ff"


def build_icon(size: int = 256) -> Image.Image:
    image = Image.new("RGBA", (size, size), BACKGROUND)
    draw = ImageDraw.Draw(image)

    outer_margin = int(size * 0.08)
    corner = int(size * 0.16)
    draw.rounded_rectangle(
        (outer_margin, outer_margin, size - outer_margin, size - outer_margin),
        radius=corner,
        fill=CARD,
        outline=BORDER,
        width=max(4, size // 42),
    )

    left = int(size * 0.2)
    top = int(size * 0.19)
    bar_width = int(size * 0.2)
    bar_height = int(size * 0.62)
    bar_radius = int(size * 0.07)
    draw.rounded_rectangle((left, top, left + bar_width, top + bar_height), radius=bar_radius, fill=BLUE)

    block_left = int(size * 0.44)
    block_width = int(size * 0.36)
    block_height = int(size * 0.14)
    gap = int(size * 0.05)
    colors = [YELLOW, CORAL, GREEN]
    for index, color in enumerate(colors):
        block_top = top + index * (block_height + gap)
        draw.rounded_rectangle(
            (block_left, block_top, block_left + block_width, block_top + block_height),
            radius=bar_radius,
            fill=color,
        )

    text_top = int(size * 0.72)
    letter_width = int(size * 0.09)
    letter_height = int(size * 0.11)
    spacing = int(size * 0.025)
    start_x = int(size * 0.18)

    # P
    draw.rounded_rectangle((start_x, text_top, start_x + letter_width, text_top + letter_height), radius=letter_width // 4, fill=WHITE)
    draw.rounded_rectangle(
        (start_x + letter_width // 3, text_top + letter_height // 3, start_x + letter_width, text_top + letter_height),
        radius=letter_width // 4,
        fill=CARD,
    )

    # D
    d_x = start_x + letter_width + spacing
    draw.rounded_rectangle((d_x, text_top, d_x + letter_width, text_top + letter_height), radius=letter_width // 4, fill=WHITE)
    draw.rounded_rectangle(
        (d_x + letter_width // 3, text_top + letter_height // 6, d_x + letter_width, text_top + letter_height - letter_height // 6),
        radius=letter_width // 3,
        fill=CARD,
    )

    # F
    f_x = d_x + letter_width + spacing
    draw.rounded_rectangle((f_x, text_top, f_x + letter_width // 3, text_top + letter_height), radius=letter_width // 5, fill=WHITE)
    draw.rounded_rectangle((f_x, text_top, f_x + letter_width, text_top + letter_height // 4), radius=letter_width // 5, fill=WHITE)
    draw.rounded_rectangle(
        (f_x, text_top + letter_height // 2 - letter_height // 10, f_x + int(letter_width * 0.75), text_top + letter_height // 2 + letter_height // 10),
        radius=letter_width // 6,
        fill=WHITE,
    )

    return image


def export_icon(output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    image = build_icon(256)
    image.save(output_path, format="ICO", sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])


def main() -> None:
    parser = ArgumentParser(description="Export the PDF Toolkit Windows installer icon.")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    export_icon(args.output)


if __name__ == "__main__":
    main()
