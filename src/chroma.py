"""Robust flat-backdrop removal, shared by the art stage and the frame stage.

The models never hand back the exact green they were asked for. A generated still
lands a few counts off, and on a video the H.264 encoder adds roughly +-20 of noise
on top of a flat backdrop and smears the chroma over a 2x2 block. A plain "replace
every pixel within tolerance of #00FF00" therefore does one of two bad things: it
leaves a green fringe around the character, or it eats into the art.

This module keys a flat backdrop in six steps instead:

1. estimate the backdrop colour from the border of the image, using a median so that
   a character limb touching the edge cannot drag the estimate;
2. mark every pixel that is close enough to that backdrop;
3. keep only the marked pixels that are *connected to the border*, so a green detail
   inside the character is never removed;
4. walk inwards from that background through the pixels that still carry a green cast,
   and stop at the outline the characters are drawn with. A tolerance always leaves a
   band of half-green behind, because the model paints the backdrop fading into the
   art; this step takes the whole band. What makes it safe is the drawing itself: the
   characters wear a bold dark outline, which is neither dark-enough-to-pass nor
   green, so the walk stops there and never reaches the art;
5. give each boundary pixel a soft alpha by unmixing it against the colour of the
   nearest opaque pixel, which is what a proper chroma key does;
6. measure the green that is *still* left on the pixels near the backdrop - a smear over
   a limb, a half-transparent effect - as a share of the backdrop, and use that share to
   lower the alpha and to solve the un-mix, which is what removes it exactly instead of
   with a hand-tuned despill threshold.

Steps 2 to 6 are the same code for a generated still and for a frame cut out of a
video, which is the point: the two stages cannot drift apart.
"""

from __future__ import annotations

import logging
from collections import Counter
from pathlib import Path
from typing import Dict, Optional, Tuple

import numpy as np

log = logging.getLogger(__name__)

RGB = Tuple[int, int, int]

DEFAULT_KEY_COLOR = "#00F700"

# Border band used to estimate the backdrop, in pixels.
BORDER_BAND = 6

# A pixel this close to the estimated backdrop is a candidate for background.
BASE_TOLERANCE = 40

# Below this alpha a pixel is treated as fully transparent, above it as foreground.
ALPHA_FLOOR = 0.06
ALPHA_CEIL = 0.94

# How far the backdrop estimate may drift before the key is called unreliable.
MAX_BACKDROP_SPREAD = 34.0

# At least this share of the border has to be backdrop-ish, or the image is not a
# flat-backdrop render and the keyer backs off instead of wrecking the art.
MIN_BORDER_MATCH = 0.55

# Codec noise floor. A flat backdrop that went through H.264 comes back a few counts
# off the colour it was painted in, and a plain "distance to the key" ramp turns every
# one of those noisy background pixels into a 10%-opaque haze instead of into nothing.
# Anything within ``max(NOISE_FLOOR_MIN, NOISE_FLOOR_SCALE * spread)`` of the backdrop
# is therefore read as background, and the ramp only starts above that dead zone.
NOISE_FLOOR_MIN = 10.0
NOISE_FLOOR_SCALE = 3.0

# Isolated specks of alpha that a codec leaves behind - stray opaque pixels in the
# middle of the backdrop, and nibbled-out holes inside the character - are dropped
# when they are smaller than this many pixels.
SPECK_MIN_PIXELS = 24
SPECK_AREA_SHARE = 3e-5

# How far from the measured border colour a pixel may sit and still be called
# backdrop when a frame is flattened. H.264 spreads a flat colour over roughly this
# band, so it is wide enough to catch the noise and far too narrow to touch the art.
FLATTEN_TOLERANCE = 40.0

# A backdrop the model painted as "pure green" comes back shifted: a still lands a few
# counts off, and a video clip moves the same green much further (measured on a real
# clip: #00F700 in, #22D60C..#2ED113 out - up to 46 counts). Those pixels are still
# the backdrop and still have to be keyed, so the guard below judges the border by
# "is this a strong bright green" instead of "is this within tolerance of one code".
#
# The three bounds are deliberately loose enough for every green the models produce
# and tight enough that white paper, a grey grid, a dark vignette, a cream character
# or a painted room all still fail, so the keyer keeps backing off those.
GREEN_FAMILY_MIN_LEVEL = 110        # the green channel has to be bright
GREEN_FAMILY_MIN_MARGIN = 60        # ... and stand well clear of red and blue
GREEN_FAMILY_MAX_OTHER = 140        # red and blue stay low: a pastel is not a key

# A pocket of backdrop that the character closes off - between the legs, between two
# fingers, under an arm - never touches the border, so the flood fill in
# ``_border_connected`` misses it and it survives the key as a blob of green stuck to
# the character. It is still the backdrop, and the project reserves this green for the
# backdrop ("never use #00F700 inside the artwork"), so a flat patch of it that the
# drawing walls in is a hole in the silhouette rather than a detail of the art.
#
# A pocket is only filled when its colour is this close to the measured backdrop, which
# is a much tighter test than the one that finds the background itself: the pocket is
# the backdrop seen through the character and carries the very same colour, whereas a
# green thing that belongs to the art is painted, shaded and outlined like everything
# else. Small patches are left alone in case they are codec dirt.
HOLE_TOLERANCE = 18.0
HOLE_MIN_PIXELS = 48
HOLE_FLAT_SHARE = 0.9

# Walking inwards from the background (step 4).
#
# A tolerance can never clean a real clip on its own. The model paints the backdrop
# running into the character - over a video it smears it into whole limbs - so wherever
# the two meet there is a band of half-green, and a threshold cuts that band in half
# and leaves the other half on the picture. The band is what reads as "the background
# is still showing through" around the legs and the feet.
#
# What makes the band separable is the drawing style: every character in this project
# wears a bold dark outline. So the background is not "the pixels whose colour is
# right", it is "the pixels the border can reach" - and the walk stops at the outline,
# because the outline is dark and carries no green cast at all. Green that the model
# splashed onto the art is reached and taken; the art itself is not, and the soft matte
# in step 5 turns the band into a proper gradient instead of a cut hole.
#
# ``GROW_STEPS`` bounds the walk so that a break in the outline cannot turn into a
# tunnel that empties the character, and ``GROW_GREEN_MARGIN`` is the cast that counts
# as "still backdrop": a shade of the character's own that happens to be greenish is
# still separated from the backdrop by the outline, and the outline is not green.
GROW_STEPS = 16
GROW_GREEN_MARGIN = 16

# The outline is drawn as a dark, saturated line, and the walk may not pass it. Judging
# "dark" against a fixed number is not enough: the model splashes the backdrop *over*
# the outline too, so a good part of the line comes back as a dark green - darker than
# the backdrop, but nowhere near black, and still green enough to pass a colour test.
# Walking through those pixels took the outline off the legs and left them looking like
# ghosts, so the wall is what is dark *compared with the backdrop actually in the file*:
# the outline sits far below it, the wash of backdrop sits at it.
GROW_BARRIER_LEVEL = 84
GROW_BARRIER_SHARE = 0.84

# How far into the grown band the soft matte still applies. Everything deeper than this
# is backdrop the walk took, and backdrop is transparent - not a ghost. Without the
# collar the band would come back as a half-transparent halo around the character,
# which on a pale background reads as a grey outline that the drawing never had.
COLLAR_PIXELS = 3

# Step 6, the green the walk could not reach.
#
# The walk stops at the outline, and where the model has smeared the backdrop over a
# limb the outline is not there any more: the smear that came back *darker* than the
# backdrop reads as one, ends the walk, and leaves a patch of dark green sitting on the
# character - the leg in a walking frame, a half-transparent effect streak.
#
# No colour test can separate those, because those pixels really are a mix of backdrop
# and art. But the mix can be measured. Green minus the larger of red and blue is at its
# maximum on the backdrop and near zero on the ink, the skin and the cloth this style is
# drawn with, so that value read against the same value on the backdrop says how much
# backdrop is in the pixel. The share then does both halves of the job: it lowers the
# alpha (half the backdrop in the pixel means half the opacity) and it solves
# ``p = a * F + (1 - a) * B`` for the colour ``F``, which is what turns a green streak
# over a white impact effect back into a white streak at half opacity.
#
# The share is only trusted within ``SPILL_REACH`` pixels of the background. Deeper in,
# a green pixel is the drawing's own and the drawing is allowed to have one - the reach
# is what keeps this test off the art.
#
# ``SPILL_MIN_GREEN`` was meant as a second guard, against codec noise on the dark line
# of a drawing, and it has to be low: the model draws its wind streaks *in the backdrop
# green*, so the pixels that most need the un-mix are dark, and a floor at the brightness
# of the backdrop skips exactly them. The cast margin is what keeps the noise out - a
# black line washed by the codec carries a cast of 20-30 counts, not the 30-90 that a
# real mix does - so the floor only has to keep the cast honest.
#
# The reach is measured from the edge of the character inwards, and it is generous
# because the spill is not always on the edge: a walking frame came back with flat
# backdrop green *inside* the torn hem of the skirt, 40-odd pixels in, where no walk from
# the border can arrive. Measured on the frames that were worst off, going from 32 to 64
# took the last visible green to zero and did not change the character's area by a single
# pixel - the gate that matters is the cast, and it is the same gate at any depth.
SPILL_REACH = 64
SPILL_MIN_GREEN = 32
SPILL_MIN_MARGIN = 28.0

# How many pixels wide the blend ring around the backdrop is taken to be when the soft
# matte is built. The outline of a drawing is a couple of pixels thick, so the mix of
# backdrop and line art lives right against the backdrop; this band is what catches it.
# It stays narrow on purpose: a wider band would start softening colours that belong to
# the character.
EDGE_BAND_PIXELS = 3


def parse_color(value) -> Optional[RGB]:
    """``"#00F700"`` / ``"00f700"`` / ``(0, 247, 0)`` -> ``(0, 247, 0)``."""
    if value is None:
        return None
    if isinstance(value, (tuple, list)) and len(value) == 3:
        return tuple(int(v) for v in value)  # type: ignore[return-value]
    text = str(value).strip().lstrip("#")
    if len(text) == 8:
        text = text[:6]
    if len(text) != 6:
        return None
    try:
        return tuple(int(text[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
    except ValueError:
        return None


def format_color(rgb) -> str:
    return "#%02X%02X%02X" % (int(rgb[0]), int(rgb[1]), int(rgb[2]))


def _border_pixels(arr: np.ndarray, band: int) -> np.ndarray:
    band = max(1, int(band))
    pieces = [arr[:band].reshape(-1, 3), arr[-band:].reshape(-1, 3)]
    if arr.shape[1] > 2 * band:
        pieces.append(arr[:, :band].reshape(-1, 3))
        pieces.append(arr[:, -band:].reshape(-1, 3))
    return np.concatenate(pieces).astype(np.float32)


def estimate_backdrop(arr: np.ndarray, band: int = BORDER_BAND):
    """Median backdrop colour plus how much the border deviates from it.

    The median ignores the minority of border pixels that belong to the character,
    so a limb or a prop that touches the edge does not bias the result.
    """
    border = _border_pixels(arr, band)
    median = np.median(border, axis=0)
    spread = float(np.median(np.abs(border - median).max(axis=1)))
    near = border[np.abs(border - median).max(axis=1) <= max(10.0, 3.0 * spread)]
    if len(near) >= 64:
        median = np.median(near, axis=0)
        spread = float(np.median(np.abs(near - median).max(axis=1)))
    share = float(len(near)) / float(len(border)) if len(border) else 0.0
    return tuple(int(round(float(v))) for v in median), spread, share


def _near_backdrop(arr: np.ndarray, key: RGB, tolerance: float) -> np.ndarray:
    distance = np.abs(arr.astype(np.int16) - np.array(key, dtype=np.int16)).max(axis=2)
    return distance <= float(tolerance)


def color_distance(left, right) -> int:
    """Largest single-channel difference between two colours."""
    return int(np.abs(np.array(left, dtype=np.int16) - np.array(right, dtype=np.int16)).max())


def is_screen_green(color: RGB) -> bool:
    """True for the family of greens a model paints as a chroma-key backdrop.

    The prompt asks for ``#00F700``, but the picture that comes back carries a shifted
    green - a still by a few counts, a video frame by far more - and it is still the
    backdrop. Telling "the same green screen, painted in a different code" apart from
    "not a green screen at all" is what keeps the keyer working on those frames without
    letting it loose on white paper, a dark vignette or a painted room.
    """
    red, green, blue = (int(value) for value in color)
    return (
        green >= GREEN_FAMILY_MIN_LEVEL
        and green - max(red, blue) >= GREEN_FAMILY_MIN_MARGIN
        and max(red, blue) <= GREEN_FAMILY_MAX_OTHER
    )


def _border_connected(mask: np.ndarray) -> np.ndarray:
    """Keep only the masked pixels reachable from the border of the image."""
    from scipy import ndimage

    labels, count = ndimage.label(mask, structure=np.ones((3, 3), dtype=bool))
    if count == 0:
        return np.zeros_like(mask)
    edge = np.concatenate([labels[0], labels[-1], labels[:, 0], labels[:, -1]])
    keep = np.unique(edge)
    keep = keep[keep != 0]
    if keep.size == 0:
        return np.zeros_like(mask)
    return np.isin(labels, keep)


def _fill_holes(
    mask: np.ndarray, arr: np.ndarray, key: RGB, tolerance: float
) -> Tuple[np.ndarray, int]:
    """Add the backdrop pockets the character walls off to the background mask.

    ``_border_connected`` keeps only what the border can reach, which is what protects
    a green detail inside the art - but it also keeps every pocket of backdrop the
    drawing closes off, and those come out of the key as green blobs stuck to the
    character. A pocket is handed back when it is a flat patch of the measured backdrop
    (see ``HOLE_TOLERANCE``) and large enough not to be codec dirt.
    """
    from scipy import ndimage

    enclosed = mask & ~_border_connected(mask)
    if not enclosed.any():
        return mask, 0
    labels, count = ndimage.label(enclosed, structure=np.ones((3, 3), dtype=bool))
    if count == 0:
        return mask, 0
    sizes = np.bincount(labels.ravel(), minlength=count + 1).astype(np.float64)
    flat = _near_backdrop(arr, key, tolerance)
    flat_counts = np.bincount(labels[flat].ravel(), minlength=count + 1).astype(np.float64)
    shares = np.divide(flat_counts, sizes, out=np.zeros_like(sizes), where=sizes > 0)
    accepted = (sizes >= HOLE_MIN_PIXELS) & (shares >= HOLE_FLAT_SHARE)
    accepted[0] = False
    if not accepted.any():
        return mask, 0
    filled = accepted[labels]
    return mask | filled, int(filled.sum())


def _wall_level(key: RGB) -> float:
    """How dark a pixel has to be to count as the outline, for this backdrop."""
    red, green, blue = (float(value) for value in key)
    luma = 0.299 * red + 0.587 * green + 0.114 * blue
    return max(float(GROW_BARRIER_LEVEL), GROW_BARRIER_SHARE * luma)


def _grow_through_spill(
    arr: np.ndarray, background: np.ndarray, key: RGB, steps: int = GROW_STEPS
) -> Tuple[np.ndarray, int]:
    """Walk inwards from the background, taking the green the model splashed inwards.

    The backdrop does not stop at the character: it fades into it, and on a video the
    model smears it over whole limbs. Wherever the two meet there is a band that is
    neither clean backdrop nor clean art, and the tolerance in step 2 always cuts that
    band in half - the half that stays is what shows up as green still sitting around
    the legs and feet.

    So the background is grown, one pixel at a time, into every neighbour that still
    carries a green cast. The walk cannot cross the outline: the outline sits far below
    the backdrop in brightness, so it fails the test and ends the walk. That is the whole trick,
    and it is why the style of the art is what makes the key clean - a character drawn
    without an outline would have to fall back on the colour test alone.

    Growth stops after ``steps`` rounds, so a gap in the outline costs a few pixels
    rather than the character, and it never seeds a region of its own: only pixels
    touching the background already found can join.
    """
    from scipy import ndimage

    rgb = arr.astype(np.float32)
    cast = rgb[..., 1] - np.maximum(rgb[..., 0], rgb[..., 2])
    luma = 0.299 * rgb[..., 0] + 0.587 * rgb[..., 1] + 0.114 * rgb[..., 2]
    wall = luma <= _wall_level(key)
    passable = (cast >= GROW_GREEN_MARGIN) & ~wall

    structure = np.ones((3, 3), dtype=bool)
    region = background.copy()
    grown = 0
    for _ in range(max(0, int(steps))):
        frontier = ndimage.binary_dilation(region, structure=structure) & ~region
        step = frontier & passable
        if not step.any():
            break
        region |= step
        grown += int(step.sum())
    return region, grown


def _soft_alpha(
    arr: np.ndarray, background: np.ndarray, key: RGB, noise_floor: float = 0.0
) -> np.ndarray:
    """Unmix every boundary pixel against the nearest opaque pixel.

    A pixel on the edge is a blend ``p = a * F + (1 - a) * B`` of the real
    foreground colour ``F`` and the backdrop ``B``. Solving for ``a`` gives a soft
    matte instead of a jagged one, and the same ``F`` is what removes the fringe.

    The blend lives in a band around the backdrop, so the ramp covers the background
    plus a few pixels of ring outside it. Everything further in is treated as the
    character and stays fully opaque, which is what stops a colour of the art that
    happens to sit near the key green from turning transparent.

    The ring has to be measured against the nearest pixel that is *definitely* the
    character, not against the nearest non-background pixel. A pixel in the ring is
    itself non-background, so the distance transform would hand it back its own colour,
    the ramp would divide it by itself and it would come out opaque with its green cast
    still on it - a green ring around an otherwise perfectly cleaned character.
    """
    from scipy import ndimage

    if not background.any():
        return np.ones(background.shape, dtype=np.float32)
    if background.all():
        return np.zeros(background.shape, dtype=np.float32)

    ring = ndimage.binary_dilation(background, iterations=EDGE_BAND_PIXELS) & ~background
    unknown = background | ring
    if unknown.all():
        return np.zeros(background.shape, dtype=np.float32)

    _, indices = ndimage.distance_transform_edt(unknown, return_indices=True)
    nearest = arr[indices[0], indices[1]].astype(np.float32)
    keyv = np.array(key, dtype=np.float32)

    denominator = np.linalg.norm(nearest - keyv, axis=2)
    numerator = np.linalg.norm(arr.astype(np.float32) - keyv, axis=2)
    # ``noise_floor`` is a dead zone at both ends of the ramp: a pixel no further from
    # the backdrop than the codec noise is background, and it reaches full opacity
    # only once it is the whole way to the nearest opaque pixel.
    span = np.maximum(denominator - noise_floor, 1e-3)
    blended = np.clip((numerator - noise_floor) / span, 0.0, 1.0)
    return np.where(unknown, blended, 1.0).astype(np.float32)


def _despeckle(alpha: np.ndarray, min_pixels: int) -> Tuple[np.ndarray, int]:
    """Drop blobs of alpha that are too small to be part of the character.

    Codec noise both sprinkles opaque pixels across the backdrop and nibbles single
    pixels out of the art, so the matte ends up dusted with one- and two-pixel
    islands. In a clip they read as flicker, and in a still they read as dirt.
    """
    from scipy import ndimage

    if min_pixels <= 1 or not (alpha > 0).any():
        return alpha, 0
    labels, count = ndimage.label(alpha > 0, structure=np.ones((3, 3), dtype=bool))
    if count == 0:
        return alpha, 0
    sizes = np.bincount(labels.ravel(), minlength=count + 1)
    sizes[0] = 0
    doomed = np.zeros(count + 1, dtype=bool)
    doomed[1:] = sizes[1:] < int(min_pixels)
    if not doomed.any():
        return alpha, 0
    cleaned = alpha.copy()
    cleaned[doomed[labels]] = 0.0
    return cleaned, int(count - np.count_nonzero(sizes[1:] >= int(min_pixels)))


def _unblend(arr: np.ndarray, alpha: np.ndarray, key: RGB) -> np.ndarray:
    """Recover the foreground colour of the semi-transparent edge pixels."""
    keyv = np.array(key, dtype=np.float32)
    source = arr.astype(np.float32)
    safe = np.maximum(alpha, 1e-3)[..., None]
    recovered = (source - (1.0 - alpha)[..., None] * keyv) / safe
    edge = (alpha > ALPHA_FLOOR) & (alpha < ALPHA_CEIL)
    out = np.where(edge[..., None], np.clip(recovered, 0.0, 255.0), source)
    return out


def _despill(
    arr: np.ndarray, alpha: np.ndarray, key: RGB, background: np.ndarray
) -> Tuple[np.ndarray, np.ndarray]:
    """Take the backdrop out of every pixel that still carries its green.

    Returns the corrected alpha and colour. See ``SPILL_REACH`` for why the share of
    backdrop inside a pixel is measured from the green cast instead of thresholded.
    """
    from scipy import ndimage

    rgb = arr.astype(np.float32)
    cast = rgb[..., 1] - np.maximum(rgb[..., 0], rgb[..., 2])
    key_cast = max(float(key[1]) - float(max(key[0], key[2])), 1.0)
    share = np.clip(cast / key_cast, 0.0, 1.0)

    reach = ndimage.distance_transform_edt(~background) <= float(SPILL_REACH)
    spill = reach & (rgb[..., 1] >= SPILL_MIN_GREEN) & (cast >= SPILL_MIN_MARGIN)
    if not spill.any():
        return alpha, rgb

    mix = np.where(spill, share, 0.0)
    out_alpha = np.where(spill, np.minimum(alpha, 1.0 - mix), alpha).astype(np.float32)

    keyv = np.array(key, dtype=np.float32)
    safe = np.maximum(1.0 - mix, 1e-3)[..., None]
    recovered = (rgb - mix[..., None] * keyv) / safe
    out_rgb = np.where(spill[..., None], np.clip(recovered, 0.0, 255.0), rgb)
    return out_alpha, out_rgb


def key_image(
    path: Path,
    key_color=None,
    tolerance: Optional[float] = None,
    soft_edge: bool = True,
    despill: bool = True,
    noise_floor: Optional[float] = None,
    despeckle: bool = True,
    grow: bool = True,
) -> Dict[str, object]:
    """Replace the flat backdrop of ``path`` with transparency, in place.

    ``key_color`` may be an explicit colour, ``None``/``"auto"`` to use the border
    estimate. Returns a summary dict; ``keyed`` is False when the image does not look
    like a flat-backdrop render, in which case the file is left untouched.
    """
    from PIL import Image

    summary: Dict[str, object] = {
        "keyed": False,
        "backdrop": None,
        "backdrop_spread": None,
        "border_match": None,
        "tolerance": None,
        "noise_floor": None,
        "specks_removed": 0,
        "alpha_pixels": 0,
        "partial_share": None,
        "drifted_from": None,
        "drift": None,
        "holes_filled": 0,
        "grown_px": 0,
        "despilled_px": 0,
        "reason": "",
    }

    image = Image.open(path).convert("RGBA")
    if image.getchannel("A").getextrema()[0] == 0 and image.getchannel("A").getextrema()[1] < 255:
        summary["reason"] = "already has an alpha channel"
        return summary

    arr = np.asarray(image.convert("RGB"))
    explicit = None if key_color in (None, "auto", "detect") else parse_color(key_color)
    # ``key`` is the colour the image *actually* carries on its border, measured rather
    # than assumed, because it is what the unmixing maths needs. The requested colour
    # can sit a couple of dozen counts away from what the model or the codec really
    # painted, and feeding the requested colour into the ramp turns the entire backdrop
    # into a faint haze instead of into transparency.
    key, spread, share = estimate_backdrop(arr)
    summary["backdrop"] = format_color(key)
    summary["backdrop_spread"] = round(spread, 2)
    summary["border_match"] = round(share, 3)

    tolerance = BASE_TOLERANCE if tolerance is None else float(tolerance)
    if explicit is not None:
        drift = color_distance(key, explicit)
        if drift > max(tolerance, 3.0 * spread):
            # A perfectly flat border that is some *other* colour is not the backdrop we
            # were promised: white paper, a dark vignette, a painted room. Keying it out
            # would eat the artwork, so back off and say why. A border that is a strong
            # bright green is the backdrop even when it sits this far from the requested
            # code: the model and the codec both shift the green, and refusing those
            # frames is what left whole video frames opaque and wrecked the alignment.
            if is_screen_green(key) and is_screen_green(explicit):
                summary["drifted_from"] = format_color(explicit)
                summary["drift"] = int(drift)
                log.debug(
                    "%s: backdrop reads %s, %d counts from %s - keying it as screen green",
                    Path(path).name, format_color(key), drift, format_color(explicit),
                )
            else:
                summary["reason"] = (
                    "the border is %s but the key colour is %s - not that backdrop"
                    % (format_color(key), format_color(explicit))
                )
                return summary
    elif spread > MAX_BACKDROP_SPREAD or share < MIN_BORDER_MATCH:
        summary["reason"] = ("no flat backdrop found (spread %.1f, border match %.0f%%)"
                             % (spread, 100.0 * share))
        return summary

    tolerance = max(tolerance, 3.0 * spread)
    summary["tolerance"] = round(tolerance, 1)

    if noise_floor is None:
        noise_floor = max(NOISE_FLOOR_MIN, NOISE_FLOOR_SCALE * spread)
    summary["noise_floor"] = round(float(noise_floor), 1)

    mask = _near_backdrop(arr, key, tolerance)
    background, holes = _fill_holes(mask, arr, key, max(float(noise_floor), HOLE_TOLERANCE))
    summary["holes_filled"] = holes
    if not background.any():
        summary["reason"] = "nothing connected to the border matched the backdrop"
        return summary

    # Step 4: the tolerance found the clean backdrop; the walk takes the band where the
    # backdrop fades into the drawing, up to the outline. Both are background - one is
    # the middle of it and the other is its edge.
    #
    # Only the collar of the band is kept for the soft matte. The rest is backdrop that
    # the walk proved is backdrop, so it goes to zero instead of becoming a translucent
    # halo: the ghost is exactly what a half-opaque fog looks like once the green is
    # gone. The collar is where the drawing's own anti-aliasing sits, and the matte is
    # what keeps the outline from going jagged.
    collar = None
    if grow:
        background, grown = _grow_through_spill(arr, background, key, steps=GROW_STEPS)
        summary["grown_px"] = grown
        if grown > 0:
            from scipy import ndimage

            depth = ndimage.distance_transform_edt(background)
            collar = depth <= COLLAR_PIXELS

    alpha = (
        _soft_alpha(arr, background, key, noise_floor)
        if soft_edge
        else np.where(background, 0.0, 1.0)
    )
    alpha = alpha.astype(np.float32)
    if collar is not None:
        alpha = np.where(collar, alpha, 0.0)
    if despeckle:
        alpha, removed = _despeckle(
            alpha, max(SPECK_MIN_PIXELS, int(alpha.size * SPECK_AREA_SHARE))
        )
        summary["specks_removed"] = removed
    rgb = _unblend(arr, alpha, key) if soft_edge else arr.astype(np.float32)
    if despill:
        before = alpha
        alpha, rgb = _despill(arr, alpha, key, background)
        summary["despilled_px"] = int((alpha < before).sum())

    alpha = np.where(alpha <= ALPHA_FLOOR, 0.0, np.where(alpha >= ALPHA_CEIL, 1.0, alpha))
    out = np.dstack([np.clip(rgb, 0, 255), alpha * 255.0]).astype(np.uint8)
    out[alpha <= 0.0] = 0

    Image.fromarray(out, "RGBA").save(path)
    summary["keyed"] = True
    summary["alpha_pixels"] = int(((alpha > 0.0) & (alpha < 1.0)).sum())
    summary["partial_share"] = round(float(((alpha > 0.0) & (alpha < 1.0)).mean()), 4)
    return summary


def flatten_backdrop(
    path: Path, color=None, tolerance: float = FLATTEN_TOLERANCE
) -> Dict[str, object]:
    """Snap every near-backdrop pixel of ``path`` to exactly one colour code.

    An optional pre-pass for a video frame, where the codec has spread the backdrop
    over a band of +-20: no two background pixels carry the same green any more, so
    "replace the colour the prompt asked for" has nothing to bite on. Measuring the
    dominant border colour instead of trusting the requested one is the point,
    because it is the *codec's* green that has to collapse to a single value.
    Anti-aliased edge pixels sit far from the backdrop and are left untouched.
    """
    from PIL import Image

    arr = np.asarray(Image.open(path).convert("RGB"))
    if color in (None, "auto", "detect"):
        counts = Counter(map(tuple, _border_pixels(arr, 2).astype(np.uint8).tolist()))
        key = counts.most_common(1)[0][0]
    else:
        key = parse_color(color)
    distance = np.abs(arr.astype(np.int16) - np.array(key, dtype=np.int16)).max(axis=2)
    mask = distance <= float(tolerance)
    out = arr.copy()
    out[mask] = np.array(key, dtype=np.uint8)
    Image.fromarray(out, "RGB").save(path)
    return {
        "color": format_color(key),
        "pixels": int(mask.sum()),
        "share": round(float(mask.mean()), 4),
    }


def composite_on_backdrop(
    path: Path, out_path: Path, color=None, fill: float = 0.7, side: Optional[int] = None
) -> Dict[str, object]:
    """Fill the transparency of ``path`` with a flat colour and grow the canvas.

    This is the video-input step, and it does three things at once:

    * every transparent pixel becomes *exactly* ``color``, so the model is handed a
      backdrop that really is one colour code - the clip it returns is a copy of this
      canvas rather than a fresh render of the character;
    * the character is trimmed, scaled to ``fill`` of the canvas and centred, which
      opens an even margin of flat colour on all four sides;
    * the canvas is square, so the video model has one obvious aspect ratio to keep.

    That margin is what actually stops the model from cropping the topknot or the
    feet off. Framing is copied from the reference image far more reliably than words
    about leaving space are obeyed.
    """
    from PIL import Image

    key = parse_color(color) or parse_color(DEFAULT_KEY_COLOR)
    image = Image.open(path).convert("RGBA")
    alpha = image.getchannel("A")
    box = alpha.point(lambda value: 255 if value > 8 else 0).getbbox()
    if box is None:
        raise ValueError(str(path) + " is fully transparent - there is nothing to send")
    if box == (0, 0, image.width, image.height) and alpha.getextrema()[0] > 240:
        # Nothing is transparent anywhere: the backdrop is still painted on. This is what
        # a raw stage-1 render looks like, and it is the one input that quietly ruined
        # this step - measuring the character by alpha measures the whole canvas, so the
        # character lands a fifth of the size it should be, standing on its own backdrop.
        image, box = _strip_painted_backdrop(image, key, path)
    image = image.crop(box)

    fill = min(0.95, max(0.2, float(fill)))
    canvas_side = int(side) if side else max(
        int(round(image.height / fill)), int(round(image.width / fill))
    )
    scale = min(
        (canvas_side * fill) / float(image.height),
        (canvas_side * fill) / float(image.width),
    )
    piece = image.resize(
        (max(1, int(round(image.width * scale))), max(1, int(round(image.height * scale)))),
        Image.LANCZOS,
    )
    canvas = Image.new("RGBA", (canvas_side, canvas_side), key + (255,))
    canvas.alpha_composite(
        piece, ((canvas_side - piece.width) // 2, (canvas_side - piece.height) // 2)
    )
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.convert("RGB").save(out_path)
    return {
        "reference": str(path),
        "output": str(out_path),
        "color": format_color(key),
        "canvas": [canvas_side, canvas_side],
        "character": [piece.width, piece.height],
        "fill": round(fill, 3),
    }


def _strip_painted_backdrop(image, key: RGB, path: Path):
    """A still that is not transparent anywhere -> its character, and its bounds.

    Only a real green screen is handled here. A border that is some *other* flat colour
    (white paper, a painted room) is not something this step can invent a silhouette for,
    so it says so instead of handing the video model a green rectangle.
    """
    arr = np.asarray(image.convert("RGB"))
    measured, spread, share = estimate_backdrop(arr)
    if not is_screen_green(measured):
        raise ValueError(
            str(path) + " has no transparent background and its border is "
            + format_color(measured) + ", which is not a green screen - run stage 2 on it"
            " first, or hand stage 3 a keyed PNG"
        )
    distance = np.abs(arr.astype(np.int16) - np.array(measured, dtype=np.int16)).max(axis=2)
    backdrop = distance <= max(float(FLATTEN_TOLERANCE), 2.0 * float(spread))
    flat = arr.copy()
    flat[backdrop] = np.array(key, dtype=np.uint8)
    ys, xs = np.nonzero(~backdrop)
    if not len(xs):
        raise ValueError(str(path) + " is one flat colour - there is no character in it")
    box = (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)
    log.info("  %s: no transparency - measuring the character against the painted"
             " backdrop and flattening it to %s", Path(path).name, format_color(key))
    return Image.fromarray(flat, "RGB").convert("RGBA"), box


def crop_to_alpha(
    path: Path, padding: int = 0, alpha_threshold: int = 8
) -> Optional[Tuple[int, int]]:
    """Cut the canvas down to the visible pixels, keeping an optional margin.

    Cropping is what makes the delivery size honest: a 512px copy of a portrait whose
    character only spans 300px of it would otherwise import into Unity at the wrong
    scale, and every asset would land at a different one.
    """
    from PIL import Image

    image = Image.open(path).convert("RGBA")
    box = image.getchannel("A").point(lambda v: 255 if v > alpha_threshold else 0).getbbox()
    if box is None:
        return None
    left, top, right, bottom = box
    if padding:
        left, top = max(0, left - int(padding)), max(0, top - int(padding))
        right = min(image.width, right + int(padding))
        bottom = min(image.height, bottom + int(padding))
    if (left, top, right, bottom) == (0, 0, image.width, image.height):
        return image.size
    cropped = image.crop((left, top, right, bottom))
    cropped.save(path)
    return cropped.size


def bbox_of_alpha(path: Path, threshold: int = 8):
    from PIL import Image

    image = Image.open(path).convert("RGBA")
    return image.getchannel("A").point(lambda v: 255 if v > threshold else 0).getbbox()


def visible_ratio(path: Path, threshold: int = 8) -> float:
    """Share of the canvas the character actually covers."""
    from PIL import Image

    image = Image.open(path).convert("RGBA")
    alpha = np.asarray(image.getchannel("A"))
    return float((alpha > threshold).mean())
