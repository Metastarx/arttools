"""Seedance video generation through the relay, plus frame extraction.

Video is what keeps an animation consistent. Drawing one frame per image request
lets the character drift between frames - the pose, the proportions and the face all
creep - whereas one clip that is cut into frames cannot drift, because a single
generation produced all of them together.

Three things make that usable, and all three live here:

* the prompt. The model copies the *framing* of the reference image far more
  faithfully than it follows words, so the words are about motion, and the margin
  around the character is baked into the reference image instead
  (:func:`src.chroma.composite_on_backdrop`);
* the relay's video endpoints, which differ from the image ones: ``seconds`` has to
  be a string, ``ratio`` has to be omitted once a first frame is attached, and the
  finished file comes back from ``/v1/videos/<id>/content`` rather than from the
  task record;
* cutting the returned mp4 into PNG frames.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import requests

from src.api import data_uri, sniff_mime

log = logging.getLogger(__name__)

VIDEO_SUBMIT = "/v1/video/generations"
VIDEO_CONTENT = "/v1/videos/{task_id}/content"

# The Seedance models the relay exposes. Order is "slower but steadier" first.
VIDEO_MODELS: Tuple[str, ...] = (
    "doubao-seedance-2-0-260128",
    "doubao-seedance-2-0-fast-260128",
    "doubao-seedance-2-5-260628",
)

DEFAULT_SECONDS = 5
DEFAULT_RESOLUTION = "720p"

FACINGS = ("left", "right")

# --------------------------------------------------------------------------------------
# The prompt
# --------------------------------------------------------------------------------------

STYLE_LOCK = """\
STRICT STYLE LOCK. You are animating the exact character that is drawn in the
reference image, and every single frame has to look like it came from the same artist
as that reference: the same head-to-body proportion, the same line weight and line
colour, the same flat unshaded colour fills, the same palette, the same face, the same
hair, the same clothes and the same accessories. Copy the reference character exactly.
Never restyle, never recolour, never add or remove detail, never redraw him in another
art style, never smooth him into a 3D render and never turn him into a different
character.

The costume is part of that lock. Read every piece of the reference outfit before you
start - hair and headwear, robe, sash, belt, knot, tassel, trimmings, trousers, boots
and any accessory - and keep each piece, and the exact colour of each piece, fixed for
the whole clip. A dark sash, belt, strap or trim stays that same dark colour in every
single frame and never turns red or any other colour, and two parts never swap colours
with each other. If the reference has a small red tassel hanging from a dark sash, the
sash stays dark and only the tassel is red, in every frame.
"""

STILL_CAMERA = """\
This is a sprite-sheet source for a 2D game, so the camera is locked onto the
character and the character is pinned in place. He stays in exactly the same spot in
the middle of the frame for the whole clip: he never moves sideways, never slides,
never drifts and never changes size, and the background never scrolls or shifts
either. The camera is completely locked off - it never pans, never tilts, never zooms
and the viewing angle never changes.
"""

WALK_BODY = """\
Only his own legs, arms, hips and head move: he runs as if he were on a treadmill
that is holding him in place.

He keeps facing the viewer for the whole clip - exactly the same front view as the
reference, the same head, the same face, both eyes visible and both shoulders square
to the camera. He never turns to the side, never goes into a three-quarter view, never
shows a profile and never turns around. In this game every character is drawn facing
the camera and the engine mirrors the sprite left and right, so the drawing must not
do that turning itself.

The run cycle has to be clear and easy to read at gameplay scale: the knees lift
alternately with a visible pump, the feet leave the ground one at a time, the body
bobs a little with each step, the arms swing opposite to the legs and the shoulders
counter-rotate. It has to look like running, not like standing still and shuffling,
and not like a slow walk.
"""

IDLE_BODY = """\
He is standing still in exactly the same front view as the reference, facing the
viewer, with his weight settled evenly on both feet and his arms relaxed at his sides.
He does not walk, does not take a step and does not turn: only a slow breath in the
chest and shoulders, a small bob of the head and a light sway of the arms, small and
calm.
"""

HIT_BODY = """\
He is hit by an attack. The clip starts on his ordinary standing pose, then he snaps
backwards away from the blow - his head jerks back, his shoulders hunch, his arms fly
up and he takes a short step back with one foot - and he then settles back down into
exactly the standing pose he started in. The whole reaction is played in place: apart
from that one short step, his feet stay on the same spot of ground and he never
leaves the frame.
"""

ATTACK_BODY = """\
He stands his ground and swings one wide, heavy strike, played in place. The clip has
three clear beats and no more: he draws the arm back and loads his weight onto the back
foot, then whips that arm through one wide flat arc across the front of his body while
the hips turn with it, then the swing carries all the way through and he pulls back into
his guard.

Both feet stay planted on the same spot of ground for the whole clip: he never steps,
never jumps, never hops and never slides, and the distance between his feet does not
change. The whole action is read from the waist up and from the arm that swings.

Keep the swing compact and keep it inside the frame. At the furthest point of the
strike his fist stops about one head-width in front of his chest and there is still a
clear band of empty green beyond it - the arm must not reach the edge of the image at
any point, not even for a single frame. He is not launching a wide roundhouse blow
that leaves the picture; it is a short, snappy strike.
"""

DEAD_BODY = """\
He is beaten and this is his last clip. All the strength goes out of him at once: his
knees give way, the body folds forward and down, and he drops onto the ground and stays
there, ending as a low, still heap. The fall happens on the spot - he never leaves the
frame, he does not slide along the ground and his feet stay near where they started.

The clip ends on that final resting pose and holds it.
"""

# Anything that is not one of the three the project ships with still has to render.
# A new action is a new word on the command line, not a new branch in this file - the
# generic body keeps it in place and looping, and the per-action detail is what
# ``hareness/prompts/<name>.xml`` is for.
GENERIC_BODY = """\
He performs one clean {motion} action, played in place. The pose changes clearly enough
that the action reads as {motion} from the first frame to the last, and the clip plays
through one complete cycle that closes on the pose it started from.

He keeps facing the viewer in the same front view as the reference for the whole clip
and stays on exactly the same spot of ground: his feet do not travel, he never leaves
the frame and he never changes size.
"""

FRAMING = """\
Framing, in every single frame: keep the wide, even margin of empty green that the
reference image already has, all the way around the character. There must be a clear
band of green above his hair, a clear band of green below his feet and a clear band of
green to his left and to his right. No part of him and nothing he carries may ever
touch the edge of the image, and nothing may ever be cropped off.
"""

LOOP = """\
The last frame shows the same pose as the first frame, so that the clip loops
seamlessly. No motion blur, no speed lines, no dust, no text, no watermark and no
subtitles.
"""

ONESHOT_END = """\
This clip is a one-shot action: it plays once and it does NOT loop. It must not return
to the pose it started from - it ends on its own final pose and stops there. No motion
blur, no speed lines, no dust, no text, no watermark and no subtitles.
"""

BACKDROP = """\
The background is ONE absolutely flat solid pure green screen, RGB {key_hex}. Every
background pixel in the whole clip is exactly the same green with the identical RGB
value - no variation in shade from one pixel to the next and no variation from one
frame to the next. It covers every pixel behind the character from edge to edge, with
no gradient, no vignette, no shading, no noise, no texture, no floor, no ground, no
shadow, no scenery and no props. Nothing but the character is drawn on top of it.
"""


# The states the game plays for a character. "walk" is the game's Run channel, "hit" is
# Hurt; the art folder is named after the state, so the names here are the ones the
# pipeline's clips are called both on the command line and in hareness/animation.yaml.
ONESHOT_MOTIONS: Tuple[str, ...] = ("attack", "dead")


def motion_names() -> Tuple[str, ...]:
    return ("idle", "walk", "hit", "attack", "dead")


def build_prompt(
    motion: str = "walk",
    *,
    facing: str = "left",
    key_color: str = "#00F700",
    extra: str = "",
) -> str:
    """Assemble the prompt for one clip.

    ``motion`` picks the body block; the style lock, the framing, the backdrop and the
    loop line are shared, so a clip can only ever differ from another one in what the
    character *does*.

    ``extra`` is the person's own instruction - the prompt presets picked on the page
    (:mod:`src.prompts`) plus whatever was typed - and it goes last, so it has the final
    word after the built-in blocks.
    """
    motion = (motion or "walk").strip().lower()
    facing = "right" if str(facing).strip().lower() == "right" else "left"
    if motion == "idle":
        body = IDLE_BODY.format(facing=facing)
    elif motion == "hit":
        body = HIT_BODY
    elif motion == "walk":
        body = WALK_BODY.format(facing=facing)
    elif motion == "attack":
        body = ATTACK_BODY
    elif motion == "dead":
        body = DEAD_BODY
    else:
        # An action nobody wrote a body for. Render it anyway instead of refusing: the
        # clip is still pinned in place and still loops, and the words that make it read
        # as *this* action belong in a prompt preset, not in a code change.
        log.info("no built-in body for '%s' - using the generic one"
                 " (write hareness/prompts/%s.xml to say what it looks like)",
                 motion, motion)
        body = GENERIC_BODY.format(motion=motion, facing=facing)
    # idle / walk / hit come back to where they started; attack and dead do not, so the
    # closing line is chosen per motion rather than appended to every clip.
    end = ONESHOT_END if motion in ONESHOT_MOTIONS else LOOP
    blocks: List[str] = [STYLE_LOCK, STILL_CAMERA, body, FRAMING,
                         BACKDROP.format(key_hex=key_color), end]
    if str(extra or "").strip():
        blocks.append(str(extra).strip())
    return "\n\n".join(block.strip() for block in blocks) + "\n"


# --------------------------------------------------------------------------------------
# The relay
# --------------------------------------------------------------------------------------


class VideoError(RuntimeError):
    pass


def data_url(path: Path) -> str:
    blob = Path(path).read_bytes()
    return data_uri(sniff_mime(blob) or "image/png", blob)


def _detail(response: requests.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return (response.text or "")[:400]
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error)[:400]
        if error:
            return str(error)[:400]
    return json.dumps(payload, ensure_ascii=False)[:400]


def submit_video(
    base_url: str,
    api_key: str,
    model: str,
    prompt: str,
    reference: Path,
    *,
    seconds: int = DEFAULT_SECONDS,
    resolution: str = DEFAULT_RESOLUTION,
    ratio: Optional[str] = None,
    timeout: float = 300.0,
    session: Optional[requests.Session] = None,
) -> Dict[str, Any]:
    """Start one clip and return the relay's task record.

    Two quirks of this endpoint are load-bearing. ``seconds`` has to be sent as a
    *string*, and ``ratio`` has to be left out whenever a first frame is attached:
    Seedance derives the aspect ratio from that image, and a second opinion is a 400.
    """
    body: Dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "seconds": str(seconds),
        "images": [data_url(reference)],
        "metadata": {"resolution": resolution},
    }
    if ratio:
        body["metadata"]["ratio"] = ratio

    url = base_url.rstrip("/") + VIDEO_SUBMIT
    headers = {"Authorization": "Bearer " + api_key, "Content-Type": "application/json"}
    session = session or requests.Session()
    response = session.post(url, json=body, headers=headers, timeout=timeout)
    if response.status_code >= 400:
        raise VideoError("HTTP " + str(response.status_code) + " from " + VIDEO_SUBMIT
                         + ": " + _detail(response))
    payload = response.json()
    task_id = payload.get("task_id") or payload.get("id")
    if not task_id:
        raise VideoError("no task id in the response: "
                         + json.dumps(payload, ensure_ascii=False)[:400])
    return payload


def is_mp4(blob: bytes) -> bool:
    return len(blob) > 4096 and blob[4:8] == b"ftyp"


def fetch_video(
    base_url: str,
    api_key: str,
    task_id: str,
    *,
    wait_s: float = 1800.0,
    poll_s: float = 15.0,
    timeout: float = 300.0,
    session: Optional[requests.Session] = None,
    on_note: Optional[Callable[[str], None]] = None,
) -> bytes:
    """Poll ``/v1/videos/<id>/content`` until the rendered mp4 is ready.

    The content endpoint is the only one that works: the task record itself answers
    403, so there is nothing to poll for a status code. A response that is not an mp4
    yet simply means "still rendering".
    """
    url = base_url.rstrip("/") + VIDEO_CONTENT.format(task_id=task_id)
    headers = {"Authorization": "Bearer " + api_key}
    session = session or requests.Session()
    deadline = time.time() + float(wait_s)
    note = "no attempt yet"
    while time.time() < deadline:
        response = session.get(url, headers=headers, timeout=timeout)
        if response.status_code < 400 and is_mp4(response.content):
            return response.content
        if response.status_code >= 400:
            note = "HTTP " + str(response.status_code) + " " + _detail(response)[:120]
        else:
            note = "not an mp4 yet (" + str(response.headers.get("Content-Type")) + ")"
        if on_note:
            on_note(note)
        time.sleep(float(poll_s))
    raise VideoError("timed out after " + str(wait_s) + "s waiting for " + str(task_id))


def generate_clip(
    base_url: str,
    api_key: str,
    model: str,
    prompt: str,
    reference: Path,
    *,
    seconds: int = DEFAULT_SECONDS,
    resolution: str = DEFAULT_RESOLUTION,
    ratio: Optional[str] = None,
    timeout: float = 300.0,
    wait_s: float = 1800.0,
    poll_s: float = 15.0,
    session: Optional[requests.Session] = None,
    on_note: Optional[Callable[[str], None]] = None,
) -> Tuple[bytes, Dict[str, Any]]:
    """Submit and collect one clip; returns ``(mp4 bytes, task record)``."""
    record = submit_video(
        base_url, api_key, model, prompt, reference,
        seconds=seconds, resolution=resolution, ratio=ratio,
        timeout=timeout, session=session,
    )
    task_id = record.get("task_id") or record.get("id")
    log.info("   task %s (%s)", task_id, record.get("status", "?"))
    blob = fetch_video(
        base_url, api_key, task_id,
        wait_s=wait_s, poll_s=poll_s, timeout=timeout, session=session, on_note=on_note,
    )
    return blob, record


# --------------------------------------------------------------------------------------
# Cutting the clip into frames
# --------------------------------------------------------------------------------------


def extract_frames(mp4: Path, out_dir: Path) -> Tuple[List[Path], Dict[str, Any]]:
    """Write ``frame_001.png`` ... next to the clip and return them with the metadata."""
    import imageio.v2 as imageio
    from PIL import Image

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for stale in out_dir.glob("*.png"):
        stale.unlink()

    reader = imageio.get_reader(str(mp4), "ffmpeg")
    try:
        raw = dict(reader.get_meta_data())
    except Exception:
        raw = {}
    meta = {key: value for key, value in raw.items()
            if isinstance(value, (int, float, str, list, dict, tuple))}
    paths: List[Path] = []
    try:
        for index, array in enumerate(reader):
            path = out_dir / ("frame_%03d.png" % (index + 1))
            Image.fromarray(array).save(path)
            paths.append(path)
    finally:
        reader.close()
    return paths, meta


def read_fps(meta: Dict[str, Any]) -> float:
    try:
        return float(meta.get("fps") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def contact_sheet(
    paths: Sequence[Path], out_path: Path, columns: int = 8, tile: int = 256
) -> Optional[Path]:
    """Lay the frames out in a grid on a light background, for eyeballing a clip."""
    from PIL import Image

    if not paths:
        return None
    rows = (len(paths) + columns - 1) // columns
    sheet = Image.new("RGBA", (columns * tile, rows * tile), (245, 245, 245, 255))
    for index, path in enumerate(paths):
        image = Image.open(path).convert("RGBA")
        image.thumbnail((tile, tile), Image.LANCZOS)
        x = (index % columns) * tile + (tile - image.width) // 2
        y = (index // columns) * tile + (tile - image.height) // 2
        sheet.alpha_composite(image, (x, y))
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.convert("RGB").save(out_path)
    return out_path
