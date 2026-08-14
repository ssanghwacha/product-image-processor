#!/usr/bin/env python3
"""Batch product image processor.

Removes backgrounds with rembg, crops to content, scales each product to fit
consistently inside a circular icon slot, centers it on a transparent square
canvas, and renames the output sequentially (product-001.png, ...).
"""

import argparse
import csv
import math
import re
import sys
from pathlib import Path

import numpy as np
from PIL import Image

VALID_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff"}


def natural_sort_key(path: Path):
    return [
        int(chunk) if chunk.isdigit() else chunk.lower()
        for chunk in re.split(r"(\d+)", path.name)
    ]


def trim_to_content(img: Image.Image) -> Image.Image:
    """Crop transparent margins down to the bounding box of visible pixels."""
    alpha = img.getchannel("A")
    bbox = alpha.getbbox()
    if bbox is None:
        return img
    return img.crop(bbox)


def resize_rgba(img: Image.Image, new_w: int, new_h: int) -> Image.Image:
    """Resize an RGBA image without haloing at transparent edges.

    Plain Image.resize() blends fully-transparent "background" RGB values
    into semi-transparent edge pixels, producing a faint fringe/line around
    cutouts. Premultiplying by alpha before resizing (and dividing it back
    out after) avoids that.
    """
    arr = np.asarray(img).astype(np.float32)
    rgb, alpha = arr[..., :3], arr[..., 3:4]
    premultiplied = np.concatenate([rgb * (alpha / 255.0), alpha], axis=-1)
    premult_img = Image.fromarray(premultiplied.astype(np.uint8), "RGBA")
    resized = premult_img.resize((new_w, new_h), Image.LANCZOS)

    arr2 = np.asarray(resized).astype(np.float32)
    rgb2, alpha2 = arr2[..., :3], arr2[..., 3:4]
    safe_alpha = np.clip(alpha2, 1, 255)
    unpremultiplied = np.clip(rgb2 / (safe_alpha / 255.0), 0, 255)
    result = np.concatenate([unpremultiplied, alpha2], axis=-1).astype(np.uint8)
    return Image.fromarray(result, "RGBA")


def fit_in_circle(img: Image.Image, canvas_px: int, fill_ratio: float) -> Image.Image:
    """Scale `img` so its diagonal fits within `fill_ratio` of the circle
    inscribed in a canvas_px x canvas_px square, then center it on a
    transparent canvas of that size."""
    w, h = img.size
    diagonal = math.hypot(w, h)
    target_diagonal = canvas_px * fill_ratio
    scale = target_diagonal / diagonal if diagonal else 1.0

    new_w = max(1, round(w * scale))
    new_h = max(1, round(h * scale))
    resized = resize_rgba(img, new_w, new_h)

    canvas = Image.new("RGBA", (canvas_px, canvas_px), (0, 0, 0, 0))
    offset = ((canvas_px - new_w) // 2, (canvas_px - new_h) // 2)
    canvas.paste(resized, offset, resized)
    return canvas


def flatten_on_white(img: Image.Image) -> Image.Image:
    """Composite onto a white background before handing to rembg.

    Some source PNGs already have transparency, but the RGB values hidden
    behind alpha=0 are often garbage (e.g. black). Feeding that straight
    into rembg (which reads RGB only) bakes a black fringe into the new
    cutout's edges. Flattening onto white first guarantees a clean,
    neutral base to matte against.
    """
    white_bg = Image.new("RGB", img.size, (255, 255, 255))
    white_bg.paste(img, mask=img.getchannel("A"))
    return white_bg


def remove_background(img: Image.Image, session, alpha_matting: bool) -> Image.Image:
    from rembg import remove

    return remove(
        img,
        session=session,
        alpha_matting=alpha_matting,
        alpha_matting_foreground_threshold=240,
        alpha_matting_background_threshold=10,
        alpha_matting_erode_size=8,
    )


def main():
    parser = argparse.ArgumentParser(description="Process product images for a consistent icon set.")
    parser.add_argument("--input", default="input", help="Folder of source images (default: input)")
    parser.add_argument("--output", default="output", help="Folder to write processed images (default: output)")
    parser.add_argument("--size", type=int, default=60, help="Target square canvas size in px (default: 60)")
    parser.add_argument("--scale", type=int, default=1, help="Export multiplier, e.g. 4 for a 240x240 retina asset at size=60 (default: 1 -> exactly 60x60)")
    parser.add_argument("--fill-ratio", type=float, default=0.7, help="Fraction of the circle's diagonal the product should fill, 0-1 (default: 0.7)")
    parser.add_argument("--prefix", default="product", help="Output filename prefix (default: product)")
    parser.add_argument("--start", type=int, default=1, help="Starting number for sequential naming (default: 1)")
    parser.add_argument("--digits", type=int, default=3, help="Zero-padding digits for sequence number (default: 3)")
    parser.add_argument("--model", default="u2net", help="rembg model name (default: u2net; try isnet-general-use for sharper cutouts)")
    parser.add_argument("--no-alpha-matting", action="store_true", help="Disable alpha matting edge refinement (faster, but rougher edges)")
    args = parser.parse_args()

    input_dir = Path(args.input)
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    if not input_dir.is_dir():
        sys.exit(f"Input folder not found: {input_dir}")

    files = sorted(
        [p for p in input_dir.iterdir() if p.suffix.lower() in VALID_EXTS],
        key=natural_sort_key,
    )
    if not files:
        sys.exit(f"No images found in {input_dir} (looked for {', '.join(sorted(VALID_EXTS))})")

    try:
        from rembg import new_session
    except ImportError:
        sys.exit(
            "rembg is not installed. Run:\n"
            "  python3 -m venv venv && source venv/bin/activate\n"
            "  pip install -r requirements.txt"
        )

    print(f"Loading background-removal model '{args.model}' (first run downloads it, ~170MB)...")
    session = new_session(args.model)

    canvas_px = args.size * args.scale
    mapping_rows = []

    for i, src_path in enumerate(files, start=args.start):
        new_name = f"{args.prefix}-{i:0{args.digits}d}.png"
        dst_path = output_dir / new_name
        print(f"[{i - args.start + 1}/{len(files)}] {src_path.name} -> {new_name}")

        try:
            with Image.open(src_path) as raw:
                raw = raw.convert("RGBA")
                flattened = flatten_on_white(raw)
                cutout = remove_background(flattened, session, alpha_matting=not args.no_alpha_matting)
                cutout = trim_to_content(cutout)
                final = fit_in_circle(cutout, canvas_px, args.fill_ratio)
                final.save(dst_path, "PNG")
            mapping_rows.append((src_path.name, new_name, "ok"))
        except Exception as e:
            print(f"  ERROR: {e}", file=sys.stderr)
            mapping_rows.append((src_path.name, new_name, f"error: {e}"))

    mapping_path = output_dir / "mapping.csv"
    with open(mapping_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["original_filename", "new_filename", "status"])
        writer.writerows(mapping_rows)

    ok_count = sum(1 for row in mapping_rows if row[2] == "ok")
    print(f"\nDone: {ok_count}/{len(files)} images processed -> {output_dir}/")
    print(f"Mapping written to {mapping_path}")


if __name__ == "__main__":
    main()
