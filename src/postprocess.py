"""Sprite post-processing: key the flat background out, then trim the canvas.

The art pipeline asks the model for a fixed pure-green (``#00FF00``) background
because the GPT / Gemini image models cannot return a real alpha channel. This
module turns that green into transparency and then crops the file down to the
visible asset, which is what a Unity sprite actually needs.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

log = logging.getLogger(__name__)

RGB = Tuple[int, int, int]

DEFAULT_KEY_COLOR = "#00FF00"

# A pixel counts as "green screen" when green sits this far above red and blue.
GREENNESS_FLOOR = 18
GREENNESS_CEIL = 70

# At least this share of the border pixels must match the key colour, otherwise
# keying is skipped so that we never eat part of the artwork by accident.
BORDER_MATCH_RATIO = 0.55


def parse_color(value: Optional[str]) -> Optional[RGB]:
    """``"#00ff00"`` / ``"00ff00"`` / ``"#0f0"`` -> ``(0, 255, 0)``."""
    if not value:
        return None
    text = str(value).strip().lstrip("#")
    if len(text) == 3:
        text = "".join(char * 2 for char in text)
    if len(text) != 6:
        return None
    try:
        return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))
    except ValueError:
        return None


def format_color(color: Sequence[int]) -> str:
    return "#%02X%02X%02X" % (color[0], color[1], color[2])


def _distance(left: Sequence[int], right: Sequence[int]) -> float:
    return max(abs(left[0] - right[0]), abs(left[1] - right[1]), abs(left[2] - right[2]))


def _is_green_key(color: Sequence[int]) -> bool:
    return color[1] > color[0] + 40 and color[1] > color[2] + 40


def _border_samples(image) -> List[RGB]:
    width, height = image.size
    points = [
        (0, 0),
        (width - 1, 0),
        (0, height - 1),
        (width - 1, height - 1),
        (width // 4, 0),
        (width // 2, 0),
        (3 * width // 4, 0),
        (width // 4, height - 1),
        (width // 2, height - 1),
        (3 * width // 4, height - 1),
        (0, height // 2),
        (width - 1, height // 2),
    ]
    return [tuple(image.getpixel(point))[:3] for point in points]


def border_match_ratio(image, key: Sequence[int], tolerance: int) -> float:
    """How much of the border looks like the key colour (0.0 - 1.0)."""
    samples = _border_samples(image)
    green_key = _is_green_key(key)
    hits = 0
    for sample in samples:
        if _distance(sample, key) <= tolerance:
            hits += 1
        elif green_key and (sample[1] - max(sample[0], sample[2])) >= GREENNESS_CEIL:
            hits += 1
    return hits / float(len(samples)) if samples else 0.0


def border_alpha_ratio(image) -> float:
    """Share of border samples that are already fully transparent."""
    samples = _border_samples(image)
    if not samples:
        return 0.0
    width, height = image.size
    points = [
        (0, 0),
        (width - 1, 0),
        (0, height - 1),
        (width - 1, height - 1),
        (width // 4, 0),
        (width // 2, 0),
        (3 * width // 4, 0),
        (width // 4, height - 1),
        (width // 2, height - 1),
        (3 * width // 4, height - 1),
        (0, height // 2),
        (width - 1, height // 2),
    ]
    rgba = image.convert("RGBA")
    clear = sum(1 for point in points if rgba.getpixel(point)[3] == 0)
    return clear / float(len(points))


def detect_key_colors(image, tolerance: int = 32) -> List[RGB]:
    """Fall back to the dominant border colour when no key colour is given."""
    samples = _border_samples(image)
    best_color, best_hits = None, 0
    for candidate in samples:
        hits = sum(1 for sample in samples if _distance(sample, candidate) <= tolerance)
        if hits > best_hits:
            best_color, best_hits = candidate, hits
    if best_color is None or best_hits < 4:
        return []
    return [best_color]


def _keep_factor(
    pixel: Sequence[int], key: Sequence[int], tolerance: int, green_key: bool, soft_edge: bool
) -> int:
    """255 = keep the pixel, 0 = fully background."""
    distance = _distance(pixel, key)
    if distance <= tolerance:
        return 0

    factor = 255
    if soft_edge and distance < tolerance * 2:
        factor = int(255 * (distance - tolerance) / max(1, tolerance))

    if green_key:
        score = pixel[1] - max(pixel[0], pixel[2])
        if score >= GREENNESS_CEIL:
            return 0
        if score > GREENNESS_FLOOR:
            ramp = int(255 * (GREENNESS_CEIL - score) / (GREENNESS_CEIL - GREENNESS_FLOOR))
            factor = min(factor, ramp)
    return max(0, min(255, factor))


def _despill(pixel: Sequence[int], key: Sequence[int], tolerance: int, green_key: bool) -> RGB:
    """Pull the green out of the anti-aliased fringe so no halo survives."""
    red, green, blue = pixel[0], pixel[1], pixel[2]
    if green_key and green > max(red, blue) and _distance(pixel, key) < tolerance * 3:
        green = max(red, blue)
    return (red, green, blue)


def key_out(
    path: Path,
    color: Optional[str] = DEFAULT_KEY_COLOR,
    tolerance: int = 48,
    soft_edge: bool = True,
    despill: bool = True,
    require_border: bool = True,
) -> bool:
    """Replace the flat background with transparency (rewrites ``path`` as RGBA)."""
    try:
        from PIL import Image
    except ImportError:
        log.warning("Pillow is not installed - skipping keying for %s", path)
        return False

    image = Image.open(path).convert("RGBA")

    key: Optional[RGB] = None
    if color and str(color).strip().lower() not in ("auto", "detect"):
        key = parse_color(color)
        if key is None:
            log.warning("Could not parse key colour %r - falling back to auto detection.", color)
    if key is None:
        detected = detect_key_colors(image, max(int(tolerance), 32))
        if not detected:
            log.warning("%s: no flat border colour detected - skipped keying.", path.name)
            return False
        key = detected[0]

    tolerance = max(0, int(tolerance))
    green_key = _is_green_key(key)

    if border_alpha_ratio(image) >= BORDER_MATCH_RATIO:
        log.info("%s: the border is already transparent - keying skipped.", path.name)
        return False

    if require_border:
        ratio = border_match_ratio(image, key, tolerance)
        if ratio < BORDER_MATCH_RATIO:
            log.warning(
                "%s: only %.0f%% of the border matches %s - keying skipped to protect the art "
                "(pass --no-cutout or a different --key-color).",
                path.name,
                ratio * 100,
                format_color(key),
            )
            return False

    pixels = list(image.getdata())
    result = []
    changed = 0
    for red, green, blue, alpha in pixels:
        if alpha == 0:
            result.append((red, green, blue, 0))
            continue
        factor = _keep_factor((red, green, blue), key, tolerance, green_key, soft_edge)
        if factor >= 255:
            result.append((red, green, blue, alpha))
            continue
        changed += 1
        if factor <= 0:
            result.append((red, green, blue, 0))
            continue
        new_alpha = int(alpha * factor / 255)
        if despill:
            red, green, blue = _despill((red, green, blue), key, tolerance, green_key)
        result.append((red, green, blue, new_alpha))

    if not changed:
        log.info("%s: nothing matched %s (tolerance %s) - left untouched.", path.name, format_color(key), tolerance)
        return False

    image.putdata(result)
    image.save(path)
    log.info(
        "%s: keyed out %s with tolerance %s (%s pixels changed).",
        path.name,
        format_color(key),
        tolerance,
        changed,
    )
    return True


def trim_to_content(
    path: Path, padding: int = 0, alpha_threshold: int = 8
) -> Optional[Tuple[int, int]]:
    """Crop the canvas down to the visible pixels; returns the new size."""
    try:
        from PIL import Image
    except ImportError:
        log.warning("Pillow is not installed - skipping trim for %s", path)
        return None

    image = Image.open(path)
    if "A" not in image.mode:
        log.warning("%s: no alpha channel - run the cutout step before trimming.", path.name)
        return None

    alpha = image.getchannel("A").point(lambda value: 255 if value > alpha_threshold else 0)
    box = alpha.getbbox()
    if box is None:
        log.warning("%s: the image is fully transparent - nothing to trim.", path.name)
        return None

    left, top, right, bottom = box
    if padding:
        left = max(0, left - padding)
        top = max(0, top - padding)
        right = min(image.width, right + padding)
        bottom = min(image.height, bottom + padding)

    if (left, top, right, bottom) == (0, 0, image.width, image.height):
        log.info("%s: already tight (%sx%s) - no trim needed.", path.name, image.width, image.height)
        return None

    before = image.size
    image.crop((left, top, right, bottom)).save(path)
    log.info(
        "%s: trimmed %sx%s -> %sx%s (padding %s).",
        path.name,
        before[0],
        before[1],
        right - left,
        bottom - top,
        padding,
    )
    return (right - left, bottom - top)


def _resample_filter(name: str):
    """Map a config name to a Pillow resampling filter (old/new Pillow)."""
    from PIL import Image

    table = {}
    for key, attr in (
        ("nearest", "NEAREST"),
        ("bilinear", "BILINEAR"),
        ("bicubic", "BICUBIC"),
        ("lanczos", "LANCZOS"),
    ):
        table[key] = getattr(Image, attr, None)
    resampling = getattr(Image, "Resampling", None)
    if resampling is not None:
        for key, attr in (
            ("nearest", "NEAREST"),
            ("bilinear", "BILINEAR"),
            ("bicubic", "BICUBIC"),
            ("lanczos", "LANCZOS"),
        ):
            value = getattr(resampling, attr, None)
            if value is not None:
                table[key] = value
    return table.get(str(name or "lanczos").strip().lower()) or table.get("lanczos")


def resize_max_side(
    path: Path,
    max_side: int,
    suffix: Optional[str] = None,
    allow_upscale: bool = True,
    resample: str = "lanczos",
) -> Optional[Tuple[int, int]]:
    """Write ``<folder>/<max_side>/<name>`` with its longest side set to ``max_side``.

    Used for the fixed delivery sizes (512 / 256 px) next to the full-resolution
    crop, so every asset in the project lands on the same sprite sizes. Copies go
    into a size-named sub folder so the folder scan never re-processes them.
    Returns the new ``(width, height)`` or ``None`` when the file was skipped.
    """
    try:
        from PIL import Image
    except ImportError:
        log.warning("Pillow is not installed - skipping resize for %s", path)
        return None

    try:
        max_side = int(max_side)
    except (TypeError, ValueError):
        return None
    if max_side <= 0:
        return None

    image = Image.open(path)
    width, height = image.size
    longest = max(width, height)
    if longest <= 0:
        return None

    if longest < max_side and not allow_upscale:
        log.info(
            "%s: %sx%s is already smaller than %s px - size skipped (allow_upscale is off).",
            path.name,
            width,
            height,
            max_side,
        )
        return None
    if longest < max_side:
        log.warning(
            "%s: upscaling %sx%s to %s px on the longest side (softens the art).",
            path.name,
            width,
            height,
            max_side,
        )

    ratio = max_side / float(longest)
    new_width = max(1, int(round(width * ratio)))
    new_height = max(1, int(round(height * ratio)))
    # Pin the longest side exactly, so rounding never leaves it at 511 or 513.
    if width >= height:
        new_width = max_side
    else:
        new_height = max_side

    resized = image.resize((new_width, new_height), _resample_filter(resample))
    if suffix is None:
        target_dir = path.parent / str(max_side)
    else:
        target_dir = path.parent
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / (path.stem + (suffix or "") + path.suffix)
    resized.save(target)
    log.info(
        "%s: wrote %sx%s copy -> %s",
        path.name,
        new_width,
        new_height,
        target.relative_to(path.parent.parent) if path.parent.parent in target.parents else target,
    )
    return (new_width, new_height)


def has_transparency(path: Path) -> bool:
    """True when the file already carries a real alpha channel with holes."""
    try:
        from PIL import Image
    except ImportError:
        return False
    try:
        image = Image.open(path)
        if "A" not in image.mode:
            return False
        return image.getchannel("A").getextrema()[0] == 0
    except Exception:
        return False


def _alpha_bbox(image, alpha_threshold: int = 8):
    """Bounding box of the pixels that are not (nearly) transparent."""
    mask = image.getchannel("A").point(lambda value: 255 if value > alpha_threshold else 0)
    return mask.getbbox()


def align_frames(
    paths: Sequence[Path], padding: int = 0, alpha_threshold: int = 8, crop: bool = True
) -> Optional[Tuple[int, int]]:
    """Re-paste every frame of a clip onto one shared canvas, where it already was.

    Frames are drawn one at a time, so the model never puts the character in exactly
    the same spot twice, and every frame would otherwise carry its own trimmed bounds -
    which is what makes a Unity sprite jitter while it plays.

    Two things make this the right way round for this pipeline:

    * the canvas is the **union** of every frame's bounds, so it is the one canvas that
      holds the whole clip and nothing is ever clipped off it (``crop=False`` keeps the
      frames' own size instead, which is what ``--no-trim`` asks for);
    * a frame keeps the position it was drawn in - every frame is shifted by the same
      ``(min left, min top)`` offset and nothing else. The video model already holds the
      character in place: measured on a real 121 frame clip at 960px, the ground line
      moves 0-16px and the character's centre 1-17px, while putting the box centre back
      on the middle of the canvas would shove the body around by up to 102px on a clip
      that lunges. Re-centring buys a couple of pixels of steadiness and pays for it with
      a sway that is an order of magnitude bigger - and it is also what hid the drift
      :func:`src.stages.clip_stats` reports: the number said the clip moves, the delivered
      frames did not.

    ``paths`` must be the keyed frames *after* the backdrop is gone and *before* anything
    crops them: the position being kept is the one in the file, so a frame that was already
    trimmed to its own bounds has nothing left to align.

    Returns the shared ``(width, height)`` or ``None`` when the clip cannot be read.
    """
    try:
        from PIL import Image
    except ImportError:
        log.warning("Pillow is not installed - skipping frame alignment")
        return None

    loaded = []
    boxes = []
    for path in paths:
        try:
            image = Image.open(path).convert("RGBA")
        except Exception as exc:
            log.warning("Could not read %s for alignment: %s", path, exc)
            return None
        box = _alpha_bbox(image, alpha_threshold)
        if box is None:
            log.warning("%s has no visible pixels - cannot align the clip.", Path(path).name)
            return None
        loaded.append((Path(path), image, box))
        boxes.append(box)

    if crop:
        left = min(box[0] for box in boxes)
        top = min(box[1] for box in boxes)
        right = max(box[2] for box in boxes)
        bottom = max(box[3] for box in boxes)
    else:
        left = top = 0
        right = max(image.width for _, image, _ in loaded)
        bottom = max(image.height for _, image, _ in loaded)
    canvas_size = (right - left + 2 * padding, bottom - top + 2 * padding)
    for path, image, box in loaded:
        piece = image.crop(box)
        canvas = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
        canvas.paste(piece, (box[0] - left + padding, box[1] - top + padding))
        canvas.save(path)
    return canvas_size


def scale_frames(
    paths: Sequence[Path],
    max_side: int,
    *,
    allow_upscale: bool = True,
    resample: str = "lanczos",
) -> List[Path]:
    """Scale a whole clip by ONE factor so the frames stay aligned.

    :func:`resize_max_side` works per file, which would re-trim the alignment of a
    clip. Animation frames have to share a canvas, so they share a factor as well.
    The copies go into a ``<size>/`` sub folder next to the frames.
    """
    try:
        from PIL import Image
    except ImportError:
        log.warning("Pillow is not installed - skipping clip resize")
        return []

    try:
        max_side = int(max_side)
    except (TypeError, ValueError):
        return []
    if max_side <= 0:
        return []

    loaded = []
    longest = 0
    for path in paths:
        try:
            image = Image.open(path).convert("RGBA")
        except Exception as exc:
            log.warning("Could not read %s for resizing: %s", path, exc)
            return []
        longest = max(longest, max(image.size))
        loaded.append((Path(path), image))

    if longest <= 0:
        return []
    if longest < max_side and not allow_upscale:
        log.info("clip is %sx smaller than %s px - size skipped (allow_upscale is off).", longest, max_side)
        return []
    if longest < max_side:
        log.warning("upscaling the clip from %s px to %s px on the longest side.", longest, max_side)

    factor = max_side / float(longest)
    target_dir = loaded[0][0].parent / str(max_side)
    target_dir.mkdir(parents=True, exist_ok=True)
    written: List[Path] = []
    for path, image in loaded:
        size = (
            max(1, int(round(image.width * factor))),
            max(1, int(round(image.height * factor))),
        )
        target = target_dir / path.name
        image.resize(size, _resample_filter(resample)).save(target)
        written.append(target)
    return written


def make_strip_sheet(paths: Sequence[Path], out_path: Path) -> Optional[Path]:
    """Lay the aligned frames out in one horizontal strip.

    That is the layout Unity imports directly with Sprite Mode = Multiple.
    """
    try:
        from PIL import Image
    except ImportError:
        return None
    frames = []
    for path in paths:
        try:
            frames.append(Image.open(path).convert("RGBA"))
        except Exception as exc:
            log.warning("Could not read %s for the strip sheet: %s", path, exc)
            return None
    if not frames:
        return None
    width = sum(frame.width for frame in frames)
    height = max(frame.height for frame in frames)
    sheet = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    left = 0
    for frame in frames:
        sheet.paste(frame, (left, height - frame.height))
        left += frame.width
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out_path)
    return out_path


def process_sprite(
    path: Path,
    key_color: Optional[str] = DEFAULT_KEY_COLOR,
    tolerance: int = 48,
    trim: bool = True,
    padding: int = 0,
    despill: bool = True,
    alpha_threshold: int = 8,
) -> Dict[str, object]:
    """Make the background transparent, then crop to the asset bounds.

    Handles both cases the models produce: a flat green background that has to be
    keyed out, or a native alpha channel that only needs trimming.
    """
    summary: Dict[str, object] = {
        "keyed": False,
        "already_transparent": False,
        "trimmed": None,
        "size": None,
    }

    summary["keyed"] = key_out(path, color=key_color, tolerance=tolerance, despill=despill)

    if trim:
        try:
            from PIL import Image

            image = Image.open(path)
            if "A" in image.mode and image.getchannel("A").getextrema()[0] == 0:
                summary["already_transparent"] = not summary["keyed"]
                if summary["already_transparent"]:
                    log.info("%s: the model already returned a transparent background.", path.name)
                summary["trimmed"] = trim_to_content(
                    path, padding=padding, alpha_threshold=alpha_threshold
                )
        except ImportError:
            pass

    try:
        from PIL import Image

        summary["size"] = Image.open(path).size
    except Exception:
        pass
    return summary


def make_transparent(path: Path, color: Optional[str] = None, tolerance: int = 24, **kwargs) -> bool:
    """Backwards-compatible wrapper: auto-detects the colour when none is given."""
    return key_out(path, color=color or "auto", tolerance=tolerance, despill=False, require_border=False, **kwargs)
