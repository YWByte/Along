"""Time-labelled contact sheets: dense frames packed into one image for motion review."""

import hashlib
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def contact_sheet(paths, times, target: Path, columns=6, tile_width=320, label=True):
    """Tiles are read left-to-right, top-to-bottom; each carries its offset from the first frame."""
    if not paths:
        raise ValueError("拼图至少需要一张图片")
    rows = math.ceil(len(paths) / columns)
    with Image.open(paths[0]) as first:
        ratio = first.height / first.width
    tile = (tile_width, max(1, round(tile_width * ratio)))
    sheet = Image.new("RGB", (tile[0] * min(columns, len(paths)), tile[1] * rows), "white")
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default(size=max(14, tile_width // 14))
    for index, (path, captured) in enumerate(zip(paths, times)):
        with Image.open(path) as image:
            image = image.convert("RGB")
            image.thumbnail(tile)
            x, y = (index % columns) * tile[0], (index // columns) * tile[1]
            sheet.paste(image, (x, y))
        if label:
            text = f"{index + 1} +{captured - times[0]:.0f}s"
            box = draw.textbbox((x + 6, y + 6), text, font=font)
            draw.rectangle((box[0] - 4, box[1] - 3, box[2] + 4, box[3] + 3), fill="black")
            draw.text((x + 6, y + 6), text, fill="white", font=font)
        draw.rectangle((x, y, x + tile[0] - 1, y + tile[1] - 1), outline="white", width=2)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    sheet.save(target, "JPEG", quality=82)
    return target


def sheet_id(frame_ids):
    return "sheet_" + hashlib.sha256("|".join(frame_ids).encode()).hexdigest()[:20]


def dense_pick(rows, limit):
    """Keep consecutive 1 fps frames when they fit; otherwise thin uniformly across the range."""
    if len(rows) <= limit:
        return list(rows)
    step = (len(rows) - 1) / (limit - 1)
    return [rows[round(i * step)] for i in range(limit)]
