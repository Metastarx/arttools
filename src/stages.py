"""The five stages of the artwork -> animation pipeline.

    1  artwork        the LLM draws the character on a flat green screen
    2  artwork_ready  key the green out, crop, write 512 / 256
    3  video_input    fill with flat green, open a margin around him
    4  video          Seedance turns that still into a moving clip
    5  frames         cut the clip up, key it, crop it, write 512 / 256

Five *steps*, four *stages* (:data:`STAGES`): a step is one command and one folder and
can be rerun on its own, a stage is what somebody actually thinks about while making an
animation. Steps 2 and 3 are the same kind of work - local image processing - and run
together as 原画处理, so the numbers do not line up:

    阶段 1  原画生成   AI     step 1   ->  01_artwork
    阶段 2  原画处理   本机   step 2+3 ->  02_artwork_ready  01_video_input
    阶段 3  视频生成   AI     step 4   ->  02_video
    阶段 4  视频处理   本机   step 5   ->  03_frames

Where they write depends on what they make. Stills go under ``resource/image/``, clips
and the frames cut from them under ``resource/video/`` - both as
``<YYYYMMDD>/<stamp>_<name>/``, so a run is two folders with the same name in two trees
rather than one folder holding six kinds of file:

    resource/image/20260923/20260923-172725_monster_imp/01_artwork/
                                                     /02_artwork_ready/
    resource/video/20260923/20260923-172725_monster_imp/01_video_input/
                                                     /02_video/<clip>/<model>/
                                                     /03_frames/<clip>/<model>/

Everything in ``resource`` is work in progress. What is worth keeping is promoted to
``origin`` (see :mod:`src.library`), and that is what the video stages read from.

Stages 2 and 5 are the same code on purpose. Their input differs - one is the still the
LLM drew, the other is a frame cut out of a clip - but everything after that is
identical, so a frame can never end up prepared differently from the artwork it was
animated from. That is the whole reason the pipeline is split this way.
"""

from __future__ import annotations

import logging
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from src import chroma, naming, video
from src.config import PROJECT_ROOT, Settings
from src.paths import (
    IMAGE_DIR_NAME,
    IMAGE_SUFFIXES,
    VIDEO_DIR_NAME,
    active_root,
    load_paths,
    relative_to_root,
    relativize_paths as relativize,
)

log = logging.getLogger(__name__)

STAGE_DIRS: Dict[int, str] = {
    1: "01_artwork",
    2: "02_artwork_ready",
    3: "01_video_input",
    4: "02_video",
    5: "03_frames",
}

# Which tree each stage writes into. Stages 1-2 are stills, 3-5 are clips.
STAGE_KINDS: Dict[int, str] = {
    1: IMAGE_DIR_NAME,
    2: IMAGE_DIR_NAME,
    3: VIDEO_DIR_NAME,
    4: VIDEO_DIR_NAME,
    5: VIDEO_DIR_NAME,
}

# The four stages, and the only place they are described. ``steps`` says which of the
# five folders a stage is made of, so the page can show four tabs and four pips while the
# run folder keeps five directories - which is what keeps a run made before this table
# existed readable, and keeps any single step runnable on its own.
ENGINE_AI = "AI"
ENGINE_LOCAL = "本机"

STAGES: Tuple[Dict[str, Any], ...] = (
    {
        "n": 1,
        "key": "artwork",
        "title": "原画生成",
        "engine": ENGINE_AI,
        "steps": (1,),
        "hint": "LLM 按 hareness/common.yaml 的风格约束，画一张纯绿 #00F700 背景的立绘",
    },
    {
        "n": 2,
        "key": "artwork_ready",
        "title": "原画处理",
        "engine": ENGINE_LOCAL,
        "steps": (2, 3),
        "hint": "本机两步一起跑：抠掉绿幕、裁到刚好围住角色、出 512 / 256；再把挑好的"
                "原画补回精确的 #00F700 并留出一圈白边，得到视频模型要吃的那张图",
    },
    {
        "n": 3,
        "key": "video",
        "title": "视频生成",
        "engine": ENGINE_AI,
        "steps": (4,),
        "hint": "拿 512 参考图调 Seedance，每个动作 × 每个模型一个 3~5 秒视频",
    },
    {
        "n": 4,
        "key": "frames",
        "title": "视频处理",
        "engine": ENGINE_LOCAL,
        "steps": (5,),
        "hint": "截帧 → 压平背景 → 抠绿幕 → 裁剪 → 整段对齐 → 出 512 / 256；挑好了再转录到 origin",
    },
)


def stage_of(number: Any) -> Dict[str, Any]:
    """The stage a number names. The page's numbers are stages, not steps."""
    try:
        wanted = int(number)
    except (TypeError, ValueError):
        raise ValueError("阶段必须是 1-4: " + str(number))
    for stage in STAGES:
        if stage["n"] == wanted:
            return stage
    raise ValueError("阶段必须是 1-4: " + str(number))


def stage_steps(number: Any) -> Tuple[int, ...]:
    """``1`` -> ``(1,)``; ``2`` -> ``(2, 3)`` - the steps a stage is made of."""
    return tuple(stage_of(number)["steps"])


def stage_states(steps: Mapping[str, Any]) -> Dict[str, Any]:
    """The five per-step flags added up into the four stages, for the page to draw.

    A stage is done when every step in it has produced something, and reports the files
    of all of them: 原画处理 is two folders on disk and one pip in the sidebar.
    """
    found: Dict[str, Any] = {}
    for stage in STAGES:
        names = [str(number) for number in stage["steps"]]
        states = [(steps.get(name) or {}) for name in names]
        found[str(stage["n"])] = {
            "ok": bool(states) and all(state.get("ok") for state in states),
            "files": sum(int(state.get("files") or 0) for state in states),
            "dirs": [state.get("dir") for state in states],
        }
    return found


DEFAULT_CLIPS: Tuple[str, ...] = video.motion_names()
DEFAULT_SIZES: Tuple[int, ...] = (512, 256)

# How much of the video-input canvas the character fills. The rest is the flat green
# margin that stops the video model from cropping his topknot or his feet off.
DEFAULT_FILL = 0.7

# The first frames of a clip are the reference image the model was handed, not new
# drawings of the character, so they are dropped when the clip is cut up.
DEFAULT_SKIP_FRAMES = 2

# Frames are compared against each other as thumbnails when the loop is checked: the
# question is where the cycle closes, and 96px is plenty to see a pose come round again.
LOOP_THUMB = 96


# --------------------------------------------------------------------------------------
# Run folders
# --------------------------------------------------------------------------------------


def stage_number(stage: Any) -> int:
    """``3`` / ``"3"`` / ``"stage3"`` -> ``3``, or a clear complaint."""
    try:
        number = int(stage)
    except (TypeError, ValueError):
        raise ValueError("stage must be one of " + ", ".join(str(k) for k in STAGE_DIRS))
    if number not in STAGE_DIRS:
        raise ValueError("stage must be one of " + ", ".join(str(k) for k in STAGE_DIRS))
    return number


@dataclass(frozen=True)
class Run:
    """One run, which lives in two trees at once.

    The stills go under ``resource/image/<day>/<name>`` and the clips under
    ``resource/video/<day>/<name>``: same day, same name, two folders. Keeping them
    apart is what keeps ``resource`` readable - a handful of 512px stills and a hundred
    and nineteen frames never end up in the same directory.

    ``key`` is ``<day>/<name>``, which is also how a run is named on the command line
    and in the page URL. Nothing stores an absolute path, so a run key written down
    today still means the same run after the project is copied somewhere else.
    """

    resource: Path
    key: str

    # -- the two folders -----------------------------------------------------------

    @property
    def day(self) -> str:
        return self.key.split("/")[0]

    @property
    def name(self) -> str:
        return self.key.split("/")[-1]

    @property
    def image_dir(self) -> Path:
        """Where the stills live. Stage 1 creates it."""
        return Path(self.resource) / IMAGE_DIR_NAME / self.key

    @property
    def video_dir(self) -> Path:
        """Where the clips live. Stage 3 creates it, which is why stage 1-2 runs that
        were never animated have no half here yet."""
        return Path(self.resource) / VIDEO_DIR_NAME / self.key

    def stage_dir(self, stage: Any) -> Path:
        """The folder one stage writes into, in whichever tree that stage belongs to."""
        number = stage_number(stage)
        base = self.image_dir if STAGE_KINDS[number] == IMAGE_DIR_NAME else self.video_dir
        return base / STAGE_DIRS[number]

    def exists(self) -> bool:
        return self.image_dir.is_dir() or self.video_dir.is_dir()

    def ensure(self) -> "Run":
        """Create both halves. Called before anything is written."""
        self.image_dir.mkdir(parents=True, exist_ok=True)
        self.video_dir.mkdir(parents=True, exist_ok=True)
        return self

    def __str__(self) -> str:
        return self.key

    # -- reading a run out of something the user typed -----------------------------

    @classmethod
    def from_key(cls, resource: Path, key: Any) -> "Run":
        """``20260923/20260923-172725_monster_imp`` -> the run.

        A leading ``resource/``, ``image/`` or ``video/`` is accepted and dropped, so a
        key copied out of a folder path - or out of the page URL - works as-is.
        """
        parts = [part for part in str(key or "").replace("\\", "/").split("/")
                 if part not in ("", ".")]
        while parts and parts[0].lower() in ("resource", IMAGE_DIR_NAME, VIDEO_DIR_NAME):
            parts = parts[1:]
        if len(parts) != 2:
            raise ValueError(
                "run 要写成 <日期>/<名字>，或者直接给 resource/image、resource/video "
                "下面的那个 run 文件夹；收到: " + str(key)
            )
        return cls(resource=Path(resource), key="/".join(parts))

    @classmethod
    def from_path(cls, resource: Path, path: Union[str, Path]) -> "Run":
        """A folder inside either tree -> the run it belongs to.

        Anything that is not under ``resource/image`` or ``resource/video`` is refused:
        the archived runs under ``resource/old`` were written before the trees were
        split and carry the old folder names, so pretending they are runs would quietly
        look in the wrong places. They stay browsable as plain folders.
        """
        resource = Path(resource).resolve()
        target = Path(path).resolve()
        for kind in (IMAGE_DIR_NAME, VIDEO_DIR_NAME):
            base = resource / kind
            try:
                parts = target.relative_to(base).parts
            except ValueError:
                continue
            if len(parts) == 2:
                return cls(resource=resource, key="/".join(parts))
        raise ValueError(
            "不是 resource/image/<日期>/<名字> 也不是 resource/video/<日期>/<名字>："
            + str(path)
        )


def resolve_run(resource: Path, value: Union[str, Path, "Run", None]) -> Run:
    """``<日期>/<名字>``, ``image/<日期>/<名字>``, a folder, or a Run -> a Run.

    One entry point for everything that accepts ``--run``, so the command line and the
    page can hand over whatever they have - a key from a URL, a folder from a file
    dialog - and get the same object back.
    """
    if isinstance(value, Run):
        return value
    text = str(value or "").strip()
    if not text:
        raise ValueError("没有指定 run（写成 <日期>/<名字>）")
    path = Path(text).expanduser()
    if path.is_dir():
        return Run.from_path(resource, path)
    return Run.from_key(resource, text)


def new_run(output_dir: Union[str, Path], name: str, stamp: Optional[str] = None) -> Run:
    """``resource/image/<YYYYMMDD>/<stamp>_<name>`` - where a new run starts."""
    from src.generator import run_day, run_stamp_now

    stamp = str(stamp or run_stamp_now())
    return Run(resource=Path(output_dir), key=run_day(stamp) + "/" + stamp + "_" + name)


def origin_stills() -> List[Path]:
    """origin/image/*.png - the stills somebody looked at and kept.

    This is what stage 3 animates. A run only starts because a picture was picked, so
    the picked picture - not whatever happens to be in this run's own 02_artwork_ready
    - is the input that matters.
    """
    root = load_paths().image_origin
    if not root.is_dir():
        return []
    return sorted(
        item for item in root.iterdir()
        if item.is_file() and item.suffix.lower() in IMAGE_SUFFIXES
    )


def run_row(run: Run) -> Dict[str, Any]:
    """One row of the run list: what each stage has produced, so far.

    Two counts, both of them true: ``steps`` is the five folders on disk and ``stages``
    is the four the page draws (阶段 2 is two of the five). ``done`` counts stages,
    because that is what the sidebar's four pips and the ``runs`` line are about.
    """
    steps: Dict[str, Any] = {}
    for number in STAGE_DIRS:
        folder = run.stage_dir(number)
        files = sum(1 for entry in folder.rglob("*") if entry.is_file()) if folder.is_dir() else 0
        steps[str(number)] = {"dir": project_path(folder), "ok": files > 0, "files": files}

    clips: List[str] = []
    models: List[str] = []
    clips_root = run.stage_dir(4)
    if clips_root.is_dir():
        for clip_dir in sorted(entry for entry in clips_root.iterdir() if entry.is_dir()):
            clips.append(clip_dir.name)
            for model_dir in sorted(entry for entry in clip_dir.iterdir() if entry.is_dir()):
                if model_dir.name not in models:
                    models.append(model_dir.name)

    stages = stage_states(steps)
    mtimes = [
        folder.stat().st_mtime
        for folder in (run.image_dir, run.video_dir)
        if folder.is_dir()
    ]
    return {
        "run": run.key,
        "day": run.day,
        "name": run.name,
        "stamp": run.name.split("_")[0],
        "mtime": max(mtimes) if mtimes else 0.0,
        # ``steps`` is what is on disk (five folders); ``stages`` is what the page draws
        # (four of them). Both, because the artifact panel shows a stage's folders and
        # the sidebar shows a stage's pip.
        "steps": steps,
        "stages": stages,
        "done": sum(1 for state in stages.values() if state["ok"]),
        "clips": clips,
        "models": models,
    }


def scan_runs(resource: Union[str, Path], kind: Optional[str] = None) -> List[Dict[str, Any]]:
    """Every run in the work tree, newest first.

    One row per <day>/<name> rather than one per folder, because a run *is* the
    pair - the stills in one tree and the clips in the other - and asking "what have I
    got" is one question. kind narrows it to the runs one tree knows about.
    """
    resource = Path(resource)
    names = [kind] if kind in (IMAGE_DIR_NAME, VIDEO_DIR_NAME) else [IMAGE_DIR_NAME, VIDEO_DIR_NAME]
    found: Dict[str, List[str]] = {}
    for name in names:
        base = resource / name
        if not base.is_dir():
            continue
        for day in base.iterdir():
            if not (day.is_dir() and len(day.name) == 8 and day.name.isdigit()):
                continue
            for folder in day.iterdir():
                if not (folder.is_dir() and folder.name[:8].isdigit()):
                    continue
                found.setdefault(day.name + "/" + folder.name, []).append(name)

    rows: List[Dict[str, Any]] = []
    for key in sorted(found, reverse=True):
        row = run_row(Run(resource=resource, key=key))
        row["has"] = sorted(found[key])
        rows.append(row)
    return rows


def project_path(path: Optional[Union[str, Path]], root: Optional[Path] = None) -> Optional[str]:
    """A path as it is written down: relative to the project root, forward slashes.

    Every path this module records in a metadata file goes through here. An absolute
    path in a sidecar makes the file useless the moment the project is copied, or read
    by somebody who cloned it somewhere else - which is exactly what the metadata is
    for.
    """
    if path is None:
        return None
    return relative_to_root(Path(path), root or active_root())


def default_key_color(common_path: Optional[Path] = None) -> str:
    """The project-wide backdrop colour, read from ``hareness/common.yaml``.

    The prompt and the keyer both have to agree on one colour code, so this is the one
    place that decides it. Anything unparseable falls back to the module constant.
    """
    value = None
    try:
        from src.harness import load_common

        common = load_common(
            Path(common_path) if common_path else PROJECT_ROOT / "hareness" / "common.yaml"
        )
        value = common.pipeline_value("background.key_color", None)
    except Exception as exc:  # a broken harness file must not stop the keyer
        log.debug("could not read the key colour from common.yaml: %s", exc)
    parsed = chroma.parse_color(value)
    return chroma.format_color(parsed) if parsed else chroma.DEFAULT_KEY_COLOR


def _clean_stage_dir(path: Path, keep_dirs: Sequence[str] = ()) -> None:
    """Empty a stage folder of the files this module owns, and nothing else.

    Only PNGs and the numeric ``512`` / ``256`` size folders are removed, so a re-run
    clears its own previous output while hand-curated sub folders survive.
    """
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    for entry in path.iterdir():
        if entry.is_dir():
            if entry.name in keep_dirs:
                continue
            if entry.name.isdigit():
                shutil.rmtree(entry)
            continue
        if entry.suffix.lower() == ".png":
            entry.unlink()


def _prepare_dir(path: Path, keep_dirs: Sequence[str] = ()) -> Path:
    path = Path(path)
    if path.parent == path:
        raise ValueError("refusing to clear the filesystem root: " + str(path))
    _clean_stage_dir(path, keep_dirs)
    return path


# --------------------------------------------------------------------------------------
# Stage 2 / stage 5 - the shared sprite preparation
# --------------------------------------------------------------------------------------


def clip_stats(paths: Sequence[Path], alpha_threshold: int = 8) -> Dict[str, Any]:
    """How steady a clip is, measured on the frames the model produced.

    These are the numbers that decide whether a clip works as a game sprite, and they are
    read off the frames exactly as the model drew them. Alignment (see
    :func:`src.postprocess.align_frames`) shifts every frame of a clip by one and the same
    offset, so the relative differences measured here survive it untouched - but measuring
    before it is what keeps "the model itself holds the character still" a statement about
    the model rather than about the code.

    A walk cycle whose character changes height by 20% pops. One whose centre wanders
    across the canvas is not an in-place animation at all. One whose last frame does not
    resemble its first cannot loop, so the frame the cycle *does* close on is searched
    for as well: that is the frame a looping Unity clip should be cut at.
    """
    from PIL import Image

    heights: List[int] = []
    widths: List[int] = []
    centres: List[float] = []
    thumbs: List[Any] = []
    empty = 0
    touching = 0

    for path in paths:
        image = Image.open(path).convert("RGBA")
        box = image.getchannel("A").point(
            lambda value: 255 if value > alpha_threshold else 0
        ).getbbox()
        thumbs.append(image.resize((LOOP_THUMB, LOOP_THUMB), Image.BILINEAR))
        if box is None:
            empty += 1
            heights.append(0)
            widths.append(0)
            continue
        widths.append(box[2] - box[0])
        heights.append(box[3] - box[1])
        centres.append((box[0] + box[2]) / 2.0)
        if box[0] <= 0 or box[1] <= 0 or box[2] >= image.width or box[3] >= image.height:
            # The art reaches the border of the frame, so the model cut a sliver of the
            # character off - a topknot or a heel. That frame cannot be used as it is.
            touching += 1

    facts: Dict[str, Any] = {
        "frames": len(paths),
        "frames_empty": empty,
        "frames_touching_edge": touching,
        "height_min": min(heights) if heights else 0,
        "height_max": max(heights) if heights else 0,
        "width_min": min(widths) if widths else 0,
        "width_max": max(widths) if widths else 0,
    }
    if heights and max(heights) > 0:
        facts["height_spread_pct"] = round(
            100.0 * (max(heights) - min(heights)) / max(heights), 1
        )
    if centres:
        facts["centre_x_first"] = round(centres[0], 1)
        facts["centre_x_last"] = round(centres[-1], 1)
        facts["drift_px"] = round(centres[-1] - centres[0], 1)

    if len(paths) >= 2:
        changes = [thumb_change(thumbs[0], thumb) for thumb in thumbs]
        facts["first_vs_last_changed_pct"] = changes[-1]
        # A cycle cannot close sooner than a quarter of the way in, or the "match" would
        # just be the frame next door.
        floor = max(2, len(paths) // 4)
        window = changes[floor:]
        if window:
            best = min(range(len(window)), key=lambda index: window[index])
            facts["loop_best_frame"] = floor + best + 1
            facts["loop_best_changed_pct"] = window[best]
    return facts


def thumb_change(left, right, threshold: int = 24) -> float:
    """Share of two frame thumbnails that differs, ignoring single-count codec noise."""
    from PIL import ImageChops

    diff = ImageChops.difference(left.convert("RGBA"), right.convert("RGBA")).convert("L")
    values = list(diff.getdata())
    if not values:
        return 0.0
    return round(100.0 * sum(1 for value in values if value > threshold) / float(len(values)), 2)
def prepare_sprites(
    items: Sequence[Path],
    out_dir: Path,
    *,
    key_color: Optional[str] = None,
    sizes: Sequence[int] = DEFAULT_SIZES,
    flatten: bool = False,
    flatten_tolerance: float = chroma.FLATTEN_TOLERANCE,
    tolerance: Optional[float] = None,
    noise_floor: Optional[float] = None,
    trim: bool = True,
    trim_padding: int = 0,
    align: bool = True,
    keep_dirs: Sequence[str] = (),
) -> Dict[str, Any]:
    """Turn pictures on a flat backdrop into cropped, game-sized sprites.

    This is the body of stage 2 *and* stage 5. ``items`` is either the artwork the LLM
    returned or the frames cut out of a clip, and from here on there is no difference:

    * ``flatten`` snaps everything near the backdrop to a single colour code first.
      Codec noise spreads a flat backdrop over a band of +-20, and collapsing that band
      is what turns the noise ring around the character into clean transparency;
    * the backdrop is keyed out using the border colour that is really in the file (see
      :mod:`src.chroma`), not the one the prompt asked for;
    * the canvas is cropped to the art, so a 512px file means a 512px *character*;
    * the whole clip is pasted onto one shared canvas - the union of every frame's
      bounds - with each frame left where the model drew it, so the sprite neither
      jitters nor sways;
    * the delivery copies go into ``512/`` and ``256/``.
    """
    from src.postprocess import align_frames, make_strip_sheet, scale_frames

    items = [Path(item) for item in items]
    if not items:
        raise ValueError("nothing to prepare")
    out_dir = _prepare_dir(out_dir, keep_dirs)

    report: Dict[str, Any] = {
        "frames": len(items),
        "key_color": key_color,
        "flattened_px": None,
        "background_removed": 0,
        "already_transparent": 0,
        "not_keyed": [],
        "cropped": 0,
        "canvas": None,
        "sizes": {},
        "sheet": None,
        "stats": None,
    }

    picked: List[Path] = []
    for item in items:
        target = out_dir / item.name
        shutil.copyfile(item, target)
        picked.append(target)

    if flatten:
        flatten_hits = 0
        for path in picked:
            info = chroma.flatten_backdrop(path, None, flatten_tolerance)
            flatten_hits += int(info["pixels"])
        report["flattened_px"] = flatten_hits

    for path in picked:
        summary = chroma.key_image(
            path, key_color, tolerance=tolerance, noise_floor=noise_floor
        )
        if summary["keyed"]:
            report["background_removed"] += 1
        elif chroma.bbox_of_alpha(path) is not None:
            # It already carries a real alpha channel: a cutout, not a green-screen
            # render. There is nothing to key, and keying it would eat the art.
            report["already_transparent"] += 1
            report["not_keyed"].append({
                "file": path.name,
                "already_transparent": True,
                "reason": summary.get("reason") or "already has an alpha channel",
            })
        else:
            report["not_keyed"].append({
                "file": path.name,
                "already_transparent": False,
                "reason": summary.get("reason"),
            })

    # Measured here, before anything is cropped or aligned: from this point on every
    # frame is pasted at the centre of a shared canvas, which would hide the drift, and
    # cropped to its own art, which would hide a sliver the model cut off.
    report["stats"] = clip_stats(picked)

    # Measured here, once the backdrop is gone and before anything crops or aligns: past
    # this line the canvas stops being the frame the character was drawn on - crop shrinks
    # it and alignment pastes every frame onto a bigger shared one - so the share of it the
    # character covers would end up measuring the canvas instead of the character.
    report["visible_share"] = round(chroma.visible_ratio(picked[0]), 4)

    if align:
        # 对齐要在裁剪之前做。裁过的帧只剩"刚好包住自己"这一点信息，模型把它画在画面
        # 哪儿已经丢了 —— 那时再"对齐"只能是按包围盒中心或左上角摆，等于凭空给角色
        # 加一个每帧都不一样的位移（实测一个出拳动作会被推得前后摆 102px）。
        # 对齐本身就把画布收到所有帧的并集上，所以这一趟就等于裁剪；--no-trim 时
        # crop=False，画布就是帧本来的大小，一帧都不动。
        canvas = align_frames(picked, padding=trim_padding if trim else 0, crop=trim)
        report["canvas"] = list(canvas) if canvas else None
        if trim and canvas:
            report["cropped"] = len(picked)
    elif trim:
        for path in picked:
            if chroma.crop_to_alpha(path, padding=trim_padding):
                report["cropped"] += 1

    for side in sizes:
        report["sizes"][str(side)] = len(scale_frames(picked, int(side)))

    if len(picked) > 1:
        strip = make_strip_sheet(picked, out_dir / "sprite_sheet.png")
        report["sheet"] = project_path(strip) if strip else None
        video.contact_sheet(picked, out_dir / "contact_sheet.png")

    return report


def write_json(path: Path, payload: Dict[str, Any]) -> Path:
    import json

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def _relocate(src: Path, dest: Path) -> List[Path]:
    """Move the loose files one stage produced into its own sub folder."""
    src, dest = Path(src), Path(dest)
    if src.resolve() == dest.resolve():
        return []
    dest.mkdir(parents=True, exist_ok=True)
    moved: List[Path] = []
    for entry in sorted(src.iterdir()):
        if entry.is_dir():
            continue
        target = dest / entry.name
        if target.exists():
            target.unlink()
        shutil.move(str(entry), str(target))
        moved.append(target)
    return moved

def import_artwork(paths: Sequence[Path], run: Run) -> List[Path]:
    """Copy artwork that came from outside the run folder into ``01_artwork``.

    Bring-your-own artwork - a still from ``origin/image``, a redo of an earlier run -
    is copied in rather than referenced, so the run folder stays self-contained and the
    later stages can be re-run from that folder alone.
    """
    dest = run.stage_dir(1)
    dest.mkdir(parents=True, exist_ok=True)
    copied: List[Path] = []
    for path in paths:
        path = Path(path)
        if not path.is_file():
            raise SystemExit("artwork not found: " + str(path))
        target = dest / path.name
        if target.resolve() != path.resolve():
            shutil.copyfile(path, target)
        copied.append(target)
    return copied


# --------------------------------------------------------------------------------------
# Stage 1 - the artwork
# --------------------------------------------------------------------------------------


def stage1_artwork(
    spec_id: str,
    settings: Settings,
    *,
    count: Optional[int] = None,
    style: Optional[str] = None,
    dry_run: bool = False,
    stamp: Optional[str] = None,
) -> Dict[str, Any]:
    """Stage 1 - the LLM draws the character on the flat green screen.

    Keying, cropping and the delivery sizes are deliberately switched off here. The raw
    green-screen render is what stage 2 consumes, and it is the only copy that still
    carries the backdrop the model actually produced - which is exactly what the keyer
    has to measure.
    """
    from src.generator import GenerateOptions, generate_spec
    from src.harness import load_common, load_specs, load_styles, resolve_style

    specs = load_specs(settings.characters_dir)
    if spec_id not in specs:
        raise SystemExit(
            "unknown spec '" + str(spec_id) + "'. Available: " + ", ".join(sorted(specs))
        )
    spec = specs[spec_id]
    styles = load_styles(settings.styles_dir)
    common = load_common(settings.common_path)
    chosen = resolve_style(spec, styles, style)

    options = GenerateOptions(
        count=count,
        output_dir=settings.output_dir,
        dry_run=dry_run,
        cutout=False,
        trim=False,
        sizes=[],
    )
    produced = generate_spec(spec, chosen, settings, options, common=common, run_stamp=stamp)
    run_dir = Path(produced.run_dir)
    run = Run.from_path(settings.output_dir, run_dir)
    artwork = _relocate(run_dir, run.stage_dir(1))
    return {
        "stage": 1,
        "spec": spec_id,
        "style": getattr(chosen, "id", None),
        "run": run.key,
        "run_dir": project_path(run_dir),
        "artwork": [project_path(path) for path in artwork if path.suffix.lower() == ".png"],
        "calls": produced.calls,
        "failures": list(produced.failures),
    }


# --------------------------------------------------------------------------------------
# Stage 2 - the artwork, ready to use
# --------------------------------------------------------------------------------------


def stage2_artwork_ready(
    run: Run,
    *,
    items: Optional[Sequence[Path]] = None,
    key_color: Optional[str] = None,
    sizes: Sequence[int] = DEFAULT_SIZES,
    tolerance: Optional[float] = None,
    trim: bool = True,
    trim_padding: int = 0,
) -> Dict[str, Any]:
    """Stage 2 - key the green out of the artwork, crop it, write the delivery sizes.

    No frame alignment here: there is only one picture, and its own bounds *are* the
    canvas. The 512px copy this writes is what stage 3 pads onto green.
    """
    if items is None:
        source = run.stage_dir(1)
        if not source.is_dir():
            raise SystemExit("阶段 1 还没跑：找不到 " + project_path(source))
        items = sorted(source.glob("*.png"))
    else:
        items = import_artwork(items, run)
    items = [Path(item) for item in items]
    if not items:
        raise SystemExit("没有可处理的图：" + project_path(run.stage_dir(1)))

    report = prepare_sprites(
        items,
        run.stage_dir(2),
        key_color=key_color,
        sizes=sizes,
        flatten=False,
        tolerance=tolerance,
        trim=trim,
        trim_padding=trim_padding,
        align=False,
    )
    report["stage"] = 2
    report["run"] = run.key
    return report


# --------------------------------------------------------------------------------------
# Stage 3 - the video input
# --------------------------------------------------------------------------------------


def stage3_video_input(
    run: Run,
    *,
    items: Optional[Sequence[Path]] = None,
    key_color: Optional[str] = None,
    fill: float = DEFAULT_FILL,
    side: Optional[int] = None,
    presets: Sequence[str] = (),
    extra_prompt: str = "",
    preset_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Stage 3 - fill the art's transparency with flat green and open a margin.

    The input is the **512px** copy stage 2 wrote: big enough for the video model to
    read the shapes, small enough to stay faithful to the drawing. Filling the
    transparency here instead of trusting the prompt is what makes the backdrop one
    exact colour code - the model is shown a picture that already has the green in it,
    and what it returns is a copy of that canvas.

    The margin is meant to survive: the character is scaled down to ``fill`` of the
    canvas and centred, so a band of flat green is left on all four sides. Words asking
    for a margin do not work, because the model copies the framing of the reference
    image instead - so the margin has to be *in* the image.

    A still from ``origin/image`` can be handed in with ``items``, which is how a
    promoted artwork gets animated; otherwise the 512px copy of this same run is used.

    ``presets`` and ``extra_prompt`` are the person's own instructions for the video
    model. They are not used here - stage 4 writes the prompt - but they are *recorded*
    here, because this is the step the page asks about them on, and stage 4 then finds
    them again without being told twice.
    """
    ready = run.stage_dir(2) / "512"
    if items is None:
        if ready.is_dir():
            items = sorted(ready.glob("*.png"))
        else:
            # Nothing prepared inside this run: animate what was kept. This is the usual
            # way in - the run folder is only where the clip lands.
            items = origin_stills()
            if not items:
                raise SystemExit(
                    "阶段 2 还没跑：" + project_path(ready)
                    + "，origin/image 里也没有原稿；用 items / --source 指定一张"
                )
    items = [Path(item) for item in items]
    if not items:
        raise SystemExit("没有 512px 素材：" + project_path(ready))

    out_dir = _prepare_dir(run.stage_dir(3))
    color = key_color or chroma.DEFAULT_KEY_COLOR
    results = []
    for item in items:
        info = chroma.composite_on_backdrop(item, out_dir / item.name, color, fill, side)
        results.append(info)
        log.info("  %s -> canvas %s on %s", item.name, info["canvas"], info["color"])
    from src import prompts as prompt_presets

    chosen = prompt_presets.resolve(
        prompt_presets.preset_dir_of(
            preset_dir if preset_dir is not None else preset_folder()
        ),
        list(presets),
    )
    recorded = prompt_presets.write_selection(
        out_dir / prompt_presets.SELECTION_NAME, chosen, extra_prompt
    )
    log.info("  prompt presets: %s",
             ", ".join(preset.name for preset in chosen) or "(none)")
    return {
        "stage": 3,
        "run": run.key,
        "fill": fill,
        "key_color": color,
        "items": results,
        "outputs": [project_path(info["output"]) for info in results],
        "presets": [preset.name for preset in chosen],
        "extra_prompt": str(extra_prompt or "").strip(),
        "prompts": project_path(recorded),
    }


def preset_folder(harness: Optional[Path] = None) -> Path:
    """Where the prompt presets live: ``<harness>/prompts``.

    Takes the harness folder, not the preset folder - the one caller that has anything to
    pass here is the CLI, and what it has is ``settings.harness_dir``. Passing it either
    way is safe: :func:`src.prompts.preset_dir_of` sorts it out, and
    :func:`stage3_video_input` runs its ``preset_dir`` argument through the same helper.
    """
    from src import prompts as prompt_presets

    return prompt_presets.preset_dir(Path(harness) if harness is not None else load_paths().harness)


def recorded_prompt(run: Run) -> Dict[str, Any]:
    """What stage 3 recorded about the prompt, or an empty selection.

    Stage 4 falls back to this so a clip rendered by ``stage4 --run ...`` alone still
    carries the style instructions that were picked on the page.
    """
    from src import prompts as prompt_presets

    return prompt_presets.read_selection(run.stage_dir(3) / prompt_presets.SELECTION_NAME)


# --------------------------------------------------------------------------------------
# Stage 4 - the video
# --------------------------------------------------------------------------------------


def _child_jobs(
    root: Path,
    clips: Optional[Sequence[str]] = None,
    models: Optional[Sequence[str]] = None,
) -> List[Tuple[str, str, Path]]:
    """Every ``<clip>/<model>`` folder under one stage directory.

    ``clips`` and ``models`` are filters; ``None`` means "whatever is on disk".
    """
    jobs: List[Tuple[str, str, Path]] = []
    if not root.is_dir():
        return jobs
    for clip_dir in sorted(entry for entry in root.iterdir() if entry.is_dir()):
        if clips and clip_dir.name not in clips:
            continue
        for model_dir in sorted(entry for entry in clip_dir.iterdir() if entry.is_dir()):
            if models and model_dir.name not in models:
                continue
            jobs.append((clip_dir.name, model_dir.name, model_dir))
    return jobs


def _video_jobs(
    run: Run,
    clips: Optional[Sequence[str]] = None,
    models: Optional[Sequence[str]] = None,
) -> List[Tuple[str, str, Path]]:
    """Every cut stage 4 has already written a ``source.mp4`` for."""
    return _child_jobs(run.stage_dir(4), clips, models)


def _cut_jobs(
    run: Run,
    clips: Optional[Sequence[str]] = None,
    models: Optional[Sequence[str]] = None,
) -> List[Tuple[str, str, Path]]:
    """Every cut stage 5 has already turned into frames.

    Picking frames is a stage 5 job, so it walks stage 5's folders and not stage 4's:
    a run whose frames were copied in from another machine, or whose ``source.mp4``
    has since been cleaned up, stays editable.
    """
    return _child_jobs(run.stage_dir(5), clips, models)


def stage4_video(
    run: Run,
    *,
    settings: Settings,
    clips: Sequence[str] = DEFAULT_CLIPS,
    models: Sequence[str] = video.VIDEO_MODELS,
    key_color: Optional[str] = None,
    facing: str = "left",
    seconds: int = video.DEFAULT_SECONDS,
    resolution: str = video.DEFAULT_RESOLUTION,
    ratio: Optional[str] = None,
    references: Optional[Dict[str, Path]] = None,
    timeout: float = 300.0,
    wait_s: float = 1800.0,
    poll_s: float = 15.0,
    force: bool = False,
    extra_prompt: str = "",
) -> Dict[str, Any]:
    """Stage 4 - one Seedance clip per clip name, per model.

    A clip that is already on disk is left alone unless ``force`` is set, so re-running
    the pipeline to re-cut the frames never pays for the videos twice.

    The prompt for each clip is written to ``04_video/<clip>/prompt.txt`` the first time
    and read back from there afterwards, so editing that file is how a clip's direction
    gets tuned. When ``extra_prompt`` is passed the file is rewritten instead, because
    somebody just asked for a different prompt on the page. ``references`` may override the still used for one clip
    (``{"walk": path}``); an override is padded onto green the same way stage 3 does it,
    so a hand-picked reference cannot arrive without its margin.
    """
    import requests

    color = key_color or chroma.DEFAULT_KEY_COLOR
    api_key = settings.require_key()
    # Read stage 3's output, never clear it: re-rendering a clip must not throw away the
    # canvas the earlier clips were rendered from.
    inputs = run.stage_dir(3)
    inputs.mkdir(parents=True, exist_ok=True)
    overrides: Dict[str, Path] = {}
    for clip, path in (references or {}).items():
        path = Path(path)
        if not path.is_file():
            raise SystemExit("reference for '" + clip + "' not found: " + str(path))
        target = inputs / (clip + "_" + path.name)
        chroma.composite_on_backdrop(path, target, color, DEFAULT_FILL, None)
        overrides[clip] = target

    defaults = sorted(inputs.glob("*.png"))
    if not defaults and not overrides:
        raise SystemExit(
            "阶段 3 还没跑：没有可做动画的输入图（" + project_path(inputs) + "）"
        )
    fallback = defaults[0] if defaults else None
    ignored: List[str] = [path.name for path in defaults[1:]]
    if ignored:
        # One clip animates one picture. With several on disk the rest are left out
        # silently, which looks like the pipeline worked when half the art was never
        # animated - say which is which instead.
        log.warning(
            "stage 3 holds %d pictures (%s): every clip animates %s and ignores %s",
            len(defaults),
            ", ".join(path.name for path in defaults),
            fallback.name if fallback else "(none)",
            ", ".join(ignored),
        )
        log.warning("  pass --reference <clip>=<file> to animate one of the others")

    session = requests.Session()
    results: List[Dict[str, Any]] = []
    for clip in clips:
        reference = overrides.get(clip) or fallback
        if reference is None:
            raise SystemExit("no reference image for clip '" + clip + "'")
        clip_dir = run.stage_dir(4) / clip
        clip_dir.mkdir(parents=True, exist_ok=True)
        prompt_path = clip_dir / "prompt.txt"
        if prompt_path.is_file() and not str(extra_prompt or "").strip():
            prompt = prompt_path.read_text(encoding="utf-8")
        else:
            prompt = video.build_prompt(clip, facing=facing, key_color=color,
                                        extra=extra_prompt)
            prompt_path.write_text(prompt, encoding="utf-8")

        for model in models:
            out_dir = clip_dir / model
            mp4 = out_dir / "source.mp4"
            if mp4.is_file() and not force:
                log.info("%s / %s: already rendered - skipped (--force redoes it)",
                         clip, model)
                results.append({"clip": clip, "model": model, "ok": True, "skipped": True,
                                "source": project_path(mp4)})
                continue
            out_dir.mkdir(parents=True, exist_ok=True)
            log.info("%s / %s: rendering from %s", clip, model, Path(reference).name)
            try:
                blob, record = video.generate_clip(
                    settings.base_url, api_key, model, prompt, reference,
                    seconds=seconds, resolution=resolution, ratio=ratio,
                    timeout=timeout, wait_s=wait_s, poll_s=poll_s, session=session,
                    on_note=lambda note: log.info("      still rendering (%s)", note),
                )
            except Exception as exc:
                log.error("%s / %s failed: %s", clip, model, str(exc)[:300])
                results.append({"clip": clip, "model": model, "ok": False,
                                "error": str(exc)[:400]})
                continue
            mp4.write_bytes(blob)
            write_json(out_dir / "meta.json", relativize({
                "clip": clip,
                "model": model,
                "reference": project_path(reference),
                "prompt": prompt,
                "seconds": seconds,
                "resolution": resolution,
                "ratio": ratio or "(follows the reference image)",
                "task": record,
                "bytes": len(blob),
            }))
            log.info("  %s (%d bytes) -> %s", clip, len(blob), mp4)
            results.append({"clip": clip, "model": model, "ok": True, "skipped": False,
                            "source": project_path(mp4), "bytes": len(blob)})
    return {
        "stage": 4,
        "run": run.key,
        "extra_prompt": str(extra_prompt or "").strip(),
        "clips": list(clips),
        "models": list(models),
        "inputs": [project_path(path) for path in defaults],
        "inputs_used": project_path(fallback) if fallback else None,
        "inputs_ignored": ignored,
        "runs": results,
    }


# --------------------------------------------------------------------------------------
# Stage 5 - the frames
# --------------------------------------------------------------------------------------


def stage5_frames(
    run: Run,
    *,
    clips: Optional[Sequence[str]] = None,
    models: Optional[Sequence[str]] = None,
    key_color: Optional[str] = None,
    sizes: Sequence[int] = DEFAULT_SIZES,
    skip_frames: int = DEFAULT_SKIP_FRAMES,
    flatten: bool = True,
    flatten_tolerance: float = chroma.FLATTEN_TOLERANCE,
    trim: bool = True,
    trim_padding: int = 0,
    tolerance: Optional[float] = None,
) -> Dict[str, Any]:
    """Stage 5 - cut each clip into frames and prepare them exactly like stage 2.

    ``flatten`` is on here and off in stage 2 because a video has been through a codec:
    the encoder leaves +-20 of noise on a flat backdrop, so the frames never contain one
    clean green. Collapsing that noise onto the single colour the border actually
    carries is what turns the haze around the character into clean transparency instead
    of leaving a soft ring of it.

    The first ``skip_frames`` frames are dropped: the model treats the still it was
    given as the opening frames, so they are the reference image rather than new
    drawings of the character.

    By default every clip that stage 4 produced is cut, whatever it was called.
    """
    jobs = _video_jobs(run, clips, models)
    if not jobs:
        raise SystemExit(
            "阶段 4 还没跑：下面没有 source.mp4：" + project_path(run.stage_dir(4))
        )

    results: List[Dict[str, Any]] = []
    for clip, model, source_dir in jobs:
        out_dir = run.stage_dir(5) / clip / model
        log.info("%s / %s: cutting frames", clip, model)
        frames, meta = video.extract_frames(source_dir / "source.mp4",
                                            out_dir / "source_frames")
        if skip_frames:
            frames = frames[skip_frames:]
        report = prepare_sprites(
            frames,
            out_dir,
            key_color=key_color,
            sizes=sizes,
            flatten=flatten,
            flatten_tolerance=flatten_tolerance,
            tolerance=tolerance,
            trim=trim,
            trim_padding=trim_padding,
            align=True,
            keep_dirs=("source_frames",),
        )
        report.update({
            "stage": 5,
            "clip": clip,
            "model": model,
            "source": project_path(source_dir / "source.mp4"),
            "fps": video.read_fps(meta),
            "frame_size": list(meta.get("size") or meta.get("source_size") or []),
            "skipped_frames": skip_frames,
        })
        payload = relativize(report)
        write_json(out_dir / "meta.json", payload)
        results.append(payload)
        stats = report.get("stats") or {}
        log.info("  %d frames  height spread %s%%  drift %spx  edge %s  loop %s%%",
                 report["frames"], stats.get("height_spread_pct"), stats.get("drift_px"),
                 stats.get("frames_touching_edge"), stats.get("first_vs_last_changed_pct"))
    return {"stage": 5, "run": run.key, "clips": results}


# --------------------------------------------------------------------------------------
# Picking the frames worth importing
# --------------------------------------------------------------------------------------

# The chosen frames live next to the full cut, under this name. Keeping both is the
# point: stage 5 is a record of what the clip actually contained, and this is the
# animation that goes into the game. Nothing is deleted from the full cut, so a frame
# dropped today can be brought back by selecting it again.
#
# The name itself is decided in :mod:`src.naming`, with every other name; re-exported
# here because this is the module the stage code reads.
SELECT_DIR = naming.SELECT_DIR


def cut_frames(cut_dir: Path) -> List[Path]:
    """The frames of a cut, in order - the numbered PNGs, not the sheets.

    The sheets sit in the same folder and are PNGs like the frames are, so globbing
    every PNG would count two frames too many and disagree with ``meta.json``. Which
    names count as a sheet, and what order the rest play in, is decided once in
    :func:`src.naming.cut_frames` - including for a folder whose frames are called
    ``1.png .. 12.png``, which a plain name sort would play 1, 10, 11, 12, 2.
    """
    return naming.cut_frames(Path(cut_dir))


def _retire(folder: Path, label: str) -> Optional[Path]:
    """Move a folder into ``temp/trash`` instead of deleting it.

    "删掉 kept/" used to ``rmtree`` it. What sits in there is somebody's afternoon of
    picking frames, and one click plus one stray Enter is not a good enough reason for
    it to be gone for good - nothing kept off a clip is big enough to be worth really
    deleting. So it is moved out of the run and into the scratch tree: out of the way,
    still readable tomorrow, and ``temp`` is git-ignored so no run collects leftovers.
    """
    if not folder.is_dir():
        return None
    root = load_paths().ensure_scratch() / "trash"
    root.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = root / ("%s_%s" % (label, stamp))
    step = 1
    while dest.exists():
        step += 1
        dest = root / ("%s_%s_%d" % (label, stamp, step))
    shutil.move(str(folder), str(dest))
    log.info("kept set moved aside -> %s", dest)
    return dest


def select_frames(
    run: Run,
    *,
    clip: str,
    model: str,
    keep: Sequence[int],
    sizes: Sequence[int] = DEFAULT_SIZES,
    key_color: Optional[str] = None,
    tolerance: Optional[float] = None,
    trim_padding: int = 0,
    clear: bool = False,
) -> Dict[str, Any]:
    """Keep the chosen frames of one cut, prepared exactly like stage 5 prepared all of them.

    A five second clip is around a hundred and twenty frames, and a good many of them
    are not worth importing: the pose repeats, the model wobbles, the cycle is closed
    long before the end. This writes the chosen ones to ``kept/`` beside the full cut,
    keyed, trimmed, pasted on one canvas so the sprite does not jitter, and copied to
    ``512/`` and ``256/`` - so the two sets sit side by side, both importable, and
    neither is a guess about the other.

    ``keep`` is indices into the full cut, in the order they should play. The canvas is
    rebuilt for the chosen frames alone, because the chosen frames *are* the animation:
    the extremes that decided the canvas of the full cut may not be in it any more.
    """
    cut_dir = run.stage_dir(5) / clip / model
    if not cut_dir.is_dir():
        raise SystemExit("这个切好的帧目录不存在：" + project_path(cut_dir))
    out_dir = cut_dir / SELECT_DIR

    if clear:
        retired = _retire(out_dir, "%s_%s_%s_kept" % (run.name, clip, model))
        return {
            "stage": 5, "clip": clip, "model": model, "kept": 0,
            "cleared": True,
            "retired": project_path(retired) if retired else None,
        }

    frames = cut_frames(cut_dir)
    if not frames:
        raise SystemExit("这个目录里没有帧：" + project_path(cut_dir))

    picked: List[Path] = []
    for index in keep:
        number = int(index)
        if number < 0 or number >= len(frames):
            raise SystemExit(
                "第 %d 帧不在这段里（一共 %d 帧）" % (number, len(frames))
            )
        if frames[number] not in picked:
            picked.append(frames[number])
    if not picked:
        raise SystemExit("一帧都没选：--frames 是空的")

    log.info("%s / %s: keeping %d of %d frames", clip, model, len(picked), len(frames))
    # ``flatten`` is off: these frames have been keyed already, and their background is
    # transparent rather than green, so there is nothing left to collapse onto a code.
    report = prepare_sprites(
        picked,
        out_dir,
        key_color=key_color,
        sizes=sizes,
        flatten=False,
        tolerance=tolerance,
        trim=True,
        trim_padding=trim_padding,
        align=True,
    )

    # The full cut's meta carries the clip's frame rate and what was skipped; the kept
    # set is the same clip, so it keeps those too and the player reads the same numbers.
    full_meta: Dict[str, Any] = {}
    try:
        import json

        full_meta = json.loads((cut_dir / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        full_meta = {}

    report.update({
        "stage": 5,
        "clip": clip,
        "model": model,
        "kept": len(picked),
        "cleared": False,
        "full_frames": len(frames),
        "selected_from": project_path(cut_dir),
        "selected_frames": [item.name for item in picked],
        "fps": full_meta.get("fps"),
        "skipped_frames": full_meta.get("skipped_frames"),
    })
    payload = relativize(report)
    write_json(out_dir / "meta.json", payload)
    stats = report.get("stats") or {}
    log.info(
        "  %d frames  canvas %s  height spread %s%%  drift %spx  loop %s%%",
        report["frames"], report["canvas"], stats.get("height_spread_pct"),
        stats.get("drift_px"), stats.get("first_vs_last_changed_pct"),
    )
    return payload


# --------------------------------------------------------------------------------------
# The whole pipeline
# --------------------------------------------------------------------------------------


def run_pipeline(
    settings: Settings,
    *,
    spec_id: Optional[str] = None,
    source: Optional[Sequence[Path]] = None,
    name: Optional[str] = None,
    run: Optional[Union["Run", str, Path]] = None,
    stamp: Optional[str] = None,
    from_stage: int = 1,
    to_stage: int = 5,
    clips: Sequence[str] = DEFAULT_CLIPS,
    models: Sequence[str] = video.VIDEO_MODELS,
    sizes: Sequence[int] = DEFAULT_SIZES,
    fill: float = DEFAULT_FILL,
    facing: str = "left",
    key_color: Optional[str] = None,
    count: Optional[int] = None,
    style: Optional[str] = None,
    seconds: int = video.DEFAULT_SECONDS,
    resolution: str = video.DEFAULT_RESOLUTION,
    references: Optional[Dict[str, Path]] = None,
    skip_frames: int = DEFAULT_SKIP_FRAMES,
    tolerance: Optional[float] = None,
    trim: bool = True,
    flatten: bool = True,
    flatten_tolerance: float = chroma.FLATTEN_TOLERANCE,
    force: bool = False,
    presets: Sequence[str] = (),
    extra_prompt: str = "",
) -> Dict[str, Any]:
    """Run the steps ``from_stage`` .. ``to_stage`` and write ``run.json``.

    ``from_stage``/``to_stage`` are *steps* - the five folders - not the four stages the
    page shows. The two line up through :data:`STAGES`, and 阶段 2 is the reason the
    upper bound exists: 原画处理 is steps 2 and 3, and ``--to`` is what keeps a re-key of
    the art from carrying on into the video model and spending money.

    Three ways in, all ending in the same pair of folders:

    * ``spec_id`` - the LLM draws the character first (step 1);
    * ``source`` - bring your own still, for instance one promoted to ``origin/image``,
      and start at step 2;
    * ``run`` plus ``from_stage`` - carry on from where a previous run stopped.
    """
    color = key_color or default_key_color(settings.common_path)
    stages: Dict[str, Any] = {}

    if run is None and not (spec_id and from_stage <= 1):
        label = name or (Path(source[0]).stem if source else "run")
        run = new_run(settings.output_dir, label, stamp)
    if run is not None:
        run = resolve_run(settings.output_dir, run)

    if from_stage <= 1 and spec_id:
        stages["1_artwork"] = stage1_artwork(
            spec_id, settings, count=count, style=style, stamp=stamp
        )
        run = Run(resource=Path(settings.output_dir), key=stages["1_artwork"]["run"])
    if run is None:
        raise SystemExit("nothing to do: pass a spec id, some artwork, or a run folder")

    if from_stage <= 2 <= to_stage:
        stages["2_artwork_ready"] = stage2_artwork_ready(
            run, items=source, key_color=color, sizes=sizes, tolerance=tolerance,
            trim=trim,
        )
    if from_stage <= 3 <= to_stage:
        stages["3_video_input"] = stage3_video_input(
            run, key_color=color, fill=fill, presets=presets, extra_prompt=extra_prompt,
            preset_dir=preset_folder(settings.harness_dir),
        )
    if from_stage <= 4 <= to_stage:
        stages["4_video"] = stage4_video(
            run, settings=settings, clips=clips, models=models, key_color=color,
            facing=facing, seconds=seconds, resolution=resolution,
            references=references, force=force, extra_prompt=extra_prompt,
        )
    if from_stage <= 5 <= to_stage:
        stages["5_frames"] = stage5_frames(
            run, clips=clips, models=list(models), key_color=color,
            sizes=sizes, skip_frames=skip_frames, tolerance=tolerance,
            trim=trim, flatten=flatten, flatten_tolerance=flatten_tolerance,
        )

    report = relativize({
        "run": run.key,
        "image_dir": project_path(run.image_dir),
        "video_dir": project_path(run.video_dir),
        "spec": spec_id,
        "from_stage": from_stage,
        "to_stage": to_stage,
        "clips": list(clips),
        "models": list(models),
        "key_color": color,
        "sizes": [int(side) for side in sizes],
        "fill": fill,
        "presets": list(presets),
        "extra_prompt": str(extra_prompt or "").strip(),
        "stages": stages,
    })
    write_json(run.image_dir / "run.json", report)
    return report


def _as_names(value: Any) -> List[str]:
    """A YAML list or a comma-separated string -> a list of names."""
    if value in (None, ""):
        return []
    items = value if isinstance(value, (list, tuple)) else str(value).replace(";", ",").split(",")
    names: List[str] = []
    for item in items:
        name = str(item).strip()
        if name and name not in names:
            names.append(name)
    return names


def video_defaults(common_path: Optional[Path] = None) -> Dict[str, Any]:
    """The video-stage settings, read from ``hareness/common.yaml``.

    Which clips get generated, from which models, how long a clip is and how much of
    the reference canvas the character fills all live in the same shared file as the
    colours and the sizes. That way the prompt, the keyer and the video stage cannot
    quietly disagree about the backdrop or the framing.
    """
    values: Dict[str, Any] = {
        "clips": list(DEFAULT_CLIPS),
        "models": list(video.VIDEO_MODELS),
        "facing": "left",
        "seconds": video.DEFAULT_SECONDS,
        "resolution": video.DEFAULT_RESOLUTION,
        "skip_frames": DEFAULT_SKIP_FRAMES,
        "fill": DEFAULT_FILL,
        "flatten_tolerance": chroma.FLATTEN_TOLERANCE,
        "tolerance": None,
    }
    try:
        from src.harness import load_common

        common = load_common(
            Path(common_path) if common_path else PROJECT_ROOT / "hareness" / "common.yaml"
        )
    except Exception as exc:
        log.debug("could not read the video defaults from common.yaml: %s", exc)
        return values

    def pick(key: str, cast):
        raw = common.pipeline_value(key, None)
        if raw in (None, ""):
            return None
        try:
            return cast(raw)
        except (TypeError, ValueError):
            log.warning("common.yaml: %s is not a usable value (%r) - ignored", key, raw)
            return None

    clips = _as_names(common.pipeline_value("video.clips", None))
    models = _as_names(common.pipeline_value("video.models", None))
    facing = common.pipeline_value("video.facing", None)
    if clips:
        values["clips"] = clips
    if models:
        values["models"] = models
    if str(facing or "").strip():
        values["facing"] = "right" if str(facing).strip().lower() == "right" else "left"
    for key, cast in (("video.seconds", int), ("video.skip_frames", int)):
        parsed = pick(key, cast)
        if parsed is not None:
            values[key.split(".")[-1]] = parsed
    for key, cast in (
        ("video.resolution", str),
        ("video.flatten_tolerance", float),
        ("video_input.fill", float),
    ):
        parsed = pick(key, cast)
        if parsed is not None:
            values[key.split(".")[-1]] = parsed

    # The keying tolerance belongs to the background block, not the video block: it is
    # the same knob for the artwork in stage 2 and the frames in stage 5, and it is the
    # one common.yaml documents next to ``background.key_color``.
    parsed = pick("background.tolerance", float)
    if parsed is not None:
        values["tolerance"] = parsed
    return values
