"""Generate continuous animation frames from a processed character portrait.

This is the second half of the asset pipeline. The first half
(:mod:`src.generator`) draws the *portrait* of a character, keys it and crops it.
The 512 px copy of that portrait then becomes the **reference image** that every
animation frame is drawn from, which is what keeps the character identical
between the standee and the animation:

    portrait -> post-process -> 512px reference
                                    |
                                    v
              one img2img request per frame -> key -> trim
                                    |
                                    v
              align the whole clip -> 512 / 256 copies

One frame per request, never a sheet:

  * the 512 px portrait is always the first reference (identity lock),
  * the frames already drawn are passed after it (motion continuity),
  * the pose text for this frame comes from ``hareness/animation.yaml``.

Alignment matters. Every frame is rendered separately, so the model never puts
the character in exactly the same place twice. :func:`align_frames` re-pastes
each frame of a clip onto one shared canvas anchored on the feet, and the 512 /
256 copies scale the whole clip by a single factor, so the sprite does not
jitter when Unity flips through the frames.

Animation is opt-in: a spec that does not declare ``animations:`` never produces
frames unless it is asked for explicitly on the command line.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import yaml

from .api import ApiError, generate_reference_with_retry
from .config import Settings
from .generator import (
    GenerateOptions,
    resolve_cutout,
    resolve_sizes,
    resolve_source,
    run_stamp_now,
)
from .harness import (
    CommonBlock,
    HarnessError,
    PromptBundle,
    PromptSpec,
    StylePreset,
    build_negative,
    load_common,
    resolve_style,
)
from .postprocess import align_frames, make_strip_sheet, process_sprite, scale_frames

log = logging.getLogger(__name__)

# Frames live in ``<run folder>/anim/<clip>/`` next to the portrait they came from.
ANIM_DIR_NAME = "anim"

# ``animations: all`` (and a bare ``--anim``) expand to whatever animation.yaml lists.
ALL_KEYWORDS = {"all", "default", "defaults", "yes", "true", "on", "*"}

POSES_PER_CLIP = 4


def _oneline(text: Any) -> str:
    """Collapse a YAML block scalar into one tidy line."""
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _as_names(value: Any) -> List[str]:
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple)):
        items = value
    else:
        items = str(value).replace(";", ",").split(",")
    return [str(item).strip().lower() for item in items if str(item).strip()]


@dataclass
class ClipPlan:
    """One animation (idle / walk / hit ...) from ``hareness/animation.yaml``."""

    id: str
    name: str = ""
    frames: int = POSES_PER_CLIP
    loop: bool = False
    motion: str = ""
    frames_text: List[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        return self.name or self.id

    @property
    def declared_frames(self) -> int:
        return len(self.frames_text) or max(1, int(self.frames or 1))

    def poses(self, override: Optional[int] = None) -> List[str]:
        """The pose text of every frame, expanded or trimmed to the wanted count."""
        total = max(1, int(override)) if override else self.declared_frames
        texts = list(self.frames_text) or ["Frame {i} of {n} of this cycle."]
        return [
            _oneline(text).replace("{i}", str(index)).replace("{n}", str(total))
            for index, text in enumerate(
                (texts[index % len(texts)] for index in range(total)), start=1
            )
        ]


@dataclass
class AnimationLibrary:
    """The clip library: what animations exist and how they are drawn."""

    clips: Dict[str, ClipPlan] = field(default_factory=dict)
    default_clips: List[str] = field(default_factory=list)
    continuity: str = ""
    negative: str = ""
    path: Optional[Path] = None

    @property
    def available(self) -> List[str]:
        return sorted(self.clips)

    def resolve(self, requested: Optional[Sequence[str]] = None) -> List[ClipPlan]:
        """Turn a list of clip names into plans (``all`` / ``default`` -> the defaults)."""
        names = _as_names(requested)
        if not names or any(name in ALL_KEYWORDS for name in names):
            names = list(self.default_clips) or list(self.clips)
        plans: List[ClipPlan] = []
        for name in names:
            clip = self.clips.get(name)
            if clip is None:
                raise HarnessError(
                    "Unknown animation '" + name + "'. Available: "
                    + (", ".join(self.available) or "none")
                    + " (see " + str(self.path or "hareness/animation.yaml") + ")"
                )
            if clip.id not in [plan.id for plan in plans]:
                plans.append(clip)
        return plans


def load_animation_library(path: Path) -> AnimationLibrary:
    """Read ``hareness/animation.yaml``; a missing file means "no clips"."""
    path = Path(path)
    if not path.is_file():
        log.info("No animation library at %s - animation generation is off.", path)
        return AnimationLibrary(path=path)

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
    except yaml.YAMLError as exc:
        raise HarnessError(str(path) + ": could not read the animation library (" + str(exc) + ")")
    if not isinstance(data, dict):
        raise HarnessError(str(path) + ": expected a YAML mapping at the top level.")

    clips: Dict[str, ClipPlan] = {}
    for name, raw in (data.get("clips") or {}).items():
        raw = raw if isinstance(raw, dict) else {}
        texts = [
            _oneline(item) for item in (raw.get("frames_text") or []) if _oneline(item)
        ]
        clips[str(name)] = ClipPlan(
            id=str(name),
            name=str(raw.get("name") or ""),
            frames=int(raw.get("frames") or len(texts) or POSES_PER_CLIP),
            loop=bool(raw.get("loop", False)),
            motion=str(raw.get("motion") or ""),
            frames_text=texts,
        )

    return AnimationLibrary(
        clips=clips,
        default_clips=_as_names(data.get("default_clips")),
        continuity=str(data.get("continuity") or "").strip(),
        negative=str(data.get("negative") or "").strip(),
        path=path,
    )


def declared_clips(spec: PromptSpec) -> List[str]:
    """The clips a spec opts into with ``animations:``; empty means "no animation"."""
    raw = spec.raw.get("animations")
    if raw is None or raw == "" or raw is False:
        return []
    if raw is True:
        return ["all"]
    return _as_names(raw)


@dataclass
class AnimOptions(GenerateOptions):
    """Everything ``gen`` knows, plus the animation-only switches.

    ``clips`` decides *which* clips are drawn:

    * ``None``  - follow each spec's ``animations:`` list (opt-in, the default),
    * ``[]``    - draw nothing,
    * ``[...]`` - draw exactly these, whatever the spec says.
    """

    clips: Optional[List[str]] = None
    frames: Optional[int] = None
    # Explicit portrait PNG for this run (the 'anim --from' switch). Note that
    # ``GenerateOptions.reference`` is a different thing: the source art a
    # *spec* is drawn from.
    reference_png: Optional[Path] = None
    run_dir: Optional[Path] = None
    run_dirs: Dict[str, Path] = field(default_factory=dict)
    history: int = 1
    align: bool = True
    sheet: bool = True


@dataclass
class ClipReport:
    """What happened while drawing one clip."""

    spec_id: str
    clip_id: str
    frames: List[Path] = field(default_factory=list)
    failures: List[str] = field(default_factory=list)
    prompts: List[str] = field(default_factory=list)
    canvas: Optional[Tuple[int, int]] = None
    sizes: Dict[str, List[Path]] = field(default_factory=dict)
    sheet: Optional[Path] = None
    endpoint: str = ""
    skipped: str = ""

    @property
    def calls(self) -> int:
        return len(self.prompts)

    @property
    def ok(self) -> bool:
        return bool(self.frames) and not self.failures


def locate_reference(
    spec_id: str,
    output_dir: Path,
    *,
    explicit: Optional[Path] = None,
    run_dir: Optional[Path] = None,
) -> Tuple[Path, Path]:
    """Find the portrait a clip should be drawn from.

    Returns ``(reference png, run folder)``. The 512 px copy is preferred, which
    is exactly the "use the 512px image as the reference" rule of the pipeline.
    Runs live at ``resource/image/<day>/<stamp>_<spec id>/`` relative to the
    resource root (which is what ``output_dir`` points at), so the search walks
    down three levels. The shallower ``<day>/<stamp>_<spec id>`` and flat
    ``<stamp>_<spec id>`` layouts are still searched so portraits drawn before
    the day folders existed keep working. Setting ``output_dir`` directly to
    ``resource/image`` instead of ``resource`` is therefore also fine.
    """
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise HarnessError("Reference image not found: " + str(path))
        parent = path.parent
        root = parent.parent if parent.name.isdigit() else parent
        return path, root

    base = Path(run_dir) if run_dir else None
    if base is None:
        root = Path(output_dir)
        pattern = "*_" + spec_id
        folders = []
        for level in ("*", "*/*", "*/*/*"):
            folders.extend(root.glob(level + "/" + pattern))
        candidates = sorted(
            (item for item in folders if item.is_dir()),
            key=lambda item: item.name,
        )
        if not candidates:
            raise HarnessError(
                "No run folder for '"
                + spec_id
                + "' under "
                + str(output_dir)
                + ". Run 'python main.py gen "
                + spec_id
                + "' first, or point at a file with --from <png>."
            )
        base = candidates[-1]

    for folder in ("512", "256"):
        hits = sorted((base / folder).glob("*.png"))
        if hits:
            return hits[0], base

    hits = sorted(base.glob("*.png"))
    if hits:
        log.info("%s: no 512px copy under %s - using %s as the reference.", spec_id, base, hits[0].name)
        return hits[0], base

    raise HarnessError("No PNG portrait found in " + str(base))


def _clip_block(clip: ClipPlan, index: int, total: int) -> str:
    lines = [
        "ANIMATION CLIP: " + clip.summary + " (" + clip.id + ").",
        "This clip is " + str(total) + " frames long; you are drawing frame "
        + str(index) + " of " + str(total) + ".",
    ]
    if clip.motion:
        lines.append(_oneline(clip.motion))
    lines.append(
        "The frames form one seamless loop." if clip.loop
        else "This is a one-shot reaction: it plays once, then the character returns to the idle stance."
    )
    return "\n".join(lines)


def build_frame_request(
    spec: PromptSpec,
    clip: ClipPlan,
    library: AnimationLibrary,
    common: Optional[CommonBlock],
    style: Optional[StylePreset],
    *,
    index: int,
    total: int,
    pose: str,
    hint: str = "",
) -> PromptBundle:
    """Assemble the prompt for exactly one animation frame."""
    common = common or CommonBlock()
    use_common = bool(common.enabled) and not common.is_empty

    pieces: List[str] = []
    if use_common and common.preamble:
        pieces.append(common.preamble.strip())
    if style and style.prefix:
        pieces.append(style.prefix)
    if use_common and common.reference_lock:
        pieces.append(common.reference_lock.strip())
    if library.continuity:
        pieces.append(library.continuity.strip())

    title = _oneline(spec.raw.get("title") or "")
    if title:
        pieces.append("Character: " + title + ". It is the character shown in the reference images.")

    pieces.append(_clip_block(clip, index, total))
    pieces.append("THIS FRAME: " + pose)
    if hint:
        pieces.append("Character notes, keep these true in every frame: " + hint)
    if use_common and common.requirements:
        pieces.append(common.requirements.strip())

    prompt = "\n\n".join(piece for piece in pieces if piece)
    negative = build_negative(spec, style, {"negative": library.negative}, common)
    if negative:
        prompt += "\n\nAvoid (do not draw): " + negative
    return PromptBundle(prompt=prompt.strip(), system="", negative=negative)


def _unique_path(folder: Path, stem: str, extension: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (stem + "." + extension)
    counter = 2
    while path.exists():
        path = folder / (stem + "_" + str(counter) + "." + extension)
        counter += 1
    return path


def generate_clip(
    spec: PromptSpec,
    clip: ClipPlan,
    style: Optional[StylePreset],
    settings: Settings,
    options: AnimOptions,
    library: AnimationLibrary,
    *,
    reference: Path,
    run_dir: Path,
    common: Optional[CommonBlock] = None,
) -> ClipReport:
    """Draw every frame of one clip, then align, scale and describe it."""
    report = ClipReport(spec_id=spec.id, clip_id=clip.id)
    cutout = resolve_cutout(options, common)
    sizes = resolve_sizes(options, common)
    source = resolve_source(options, common)
    model = options.model or spec.model or settings.model
    api_style = options.api_style or spec.api_style or settings.api_style
    aspect_ratio = (
        options.aspect_ratio
        or spec.aspect_ratio
        or (style.aspect_ratio if style else None)
        or (common.default_for("aspect_ratio") if common else None)
    )
    image_size = (
        options.image_size
        or spec.image_size
        or (style.image_size if style else None)
        or (common.default_for("image_size") if common else None)
    )
    hint = _oneline(spec.raw.get("animation_hint") or "")
    poses = clip.poses(options.frames)
    total = len(poses)
    clip_dir = Path(run_dir) / ANIM_DIR_NAME / clip.id

    log.info(
        "%s/%s: %s frame(s) from %s via %s (%s)",
        spec.id, clip.id, total, reference.name, api_style, model,
    )

    frames: List[Path] = []
    history: List[bytes] = []

    for index, pose in enumerate(poses, start=1):
        bundle = build_frame_request(
            spec, clip, library, common, style,
            index=index, total=total, pose=pose, hint=hint,
        )
        report.prompts.append(bundle.as_text())

        if options.dry_run:
            log.info("[dry-run] %s/%s frame %s\n%s", spec.id, clip.id, index, bundle.as_text())
            continue

        # portrait first (who the character is), then the frames drawn so far (where
        # the motion is), so this frame continues from the previous one.
        references: List[Any] = [reference] + history[-max(0, int(options.history)) :]
        try:
            result = generate_reference_with_retry(
                settings,
                bundle.prompt,
                references,
                api_style=api_style,
                model=model,
                aspect_ratio=aspect_ratio,
                image_size=image_size,
                system=bundle.system or None,
                max_side=source.max_side if source.enabled else None,
            )
        except ApiError as exc:
            message = "frame " + str(index) + ": " + str(exc)[:300]
            log.error("%s/%s failed: %s", spec.id, clip.id, message)
            report.failures.append(message)
            continue

        if not result.images:
            message = "frame " + str(index) + ": the platform returned no image (" + (result.text or "empty")[:120] + ")"
            log.error("%s/%s %s", spec.id, clip.id, message)
            report.failures.append(message)
            continue

        report.endpoint = result.endpoint
        image = result.images[0]
        stem = clip.id + "_" + str(len(frames) + 1).zfill(2)
        path = _unique_path(clip_dir, stem, image.extension)
        path.write_bytes(image.data)

        if cutout.enabled:
            process_sprite(
                path,
                key_color=cutout.key_color,
                tolerance=cutout.tolerance,
                trim=cutout.trim,
                padding=cutout.trim_padding,
                despill=cutout.despill,
                alpha_threshold=cutout.alpha_threshold,
            )
        frames.append(path)
        history.append(path.read_bytes())
        log.info("%s/%s frame %s/%s saved -> %s", spec.id, clip.id, index, total, path.name)

    report.frames = frames
    if not frames:
        if options.dry_run:
            report.skipped = "dry run"
        return report

    if options.align:
        canvas = align_frames(frames, padding=cutout.trim_padding)
        if canvas:
            report.canvas = canvas
            log.info("%s/%s: aligned %s frames onto a %sx%s canvas", spec.id, clip.id, len(frames), canvas[0], canvas[1])
        else:
            log.warning("%s/%s: could not align the frames - they keep their own bounds.", spec.id, clip.id)

    if sizes.enabled:
        for side in sizes.max_sides:
            written = scale_frames(
                frames,
                side,
                allow_upscale=sizes.allow_upscale,
                resample=sizes.resample,
            )
            if written:
                report.sizes[str(side)] = written
                log.info("%s/%s: wrote %s frame(s) at %spx", spec.id, clip.id, len(written), side)

    if options.sheet and len(frames) > 1:
        report.sheet = make_strip_sheet(frames, clip_dir / (clip.id + "_sheet.png"))
        if report.sheet:
            log.info("%s/%s: strip sheet -> %s", spec.id, clip.id, report.sheet.name)

    if options.write_metadata and settings.write_metadata:
        record: Dict[str, Any] = {
            "kind": "animation",
            "spec": spec.id,
            "clip": clip.id,
            "clip_name": clip.name,
            "loop": clip.loop,
            "frames": len(frames),
            "files": [path.name for path in frames],
            "reference": str(reference),
            "folder": str(clip_dir),
            "created": datetime.now().isoformat(timespec="seconds"),
            "model": model,
            "api_style": api_style,
            "endpoint": report.endpoint,
            "aspect_ratio": aspect_ratio,
            "image_size": image_size,
            "align": bool(options.align),
            "canvas": list(report.canvas) if report.canvas else None,
            "sizes": {key: [path.name for path in value] for key, value in report.sizes.items()},
            "sheet": report.sheet.name if report.sheet else None,
            "history": int(options.history),
            "cutout": {
                "enabled": cutout.enabled,
                "key_color": cutout.key_color,
                "tolerance": cutout.tolerance,
                "despill": cutout.despill,
                "trim": cutout.trim,
                "trim_padding": cutout.trim_padding,
            },
            "source_cap_px": source.max_side if source.enabled else None,
            "prompts": report.prompts,
            "failures": report.failures,
        }
        clip_dir.mkdir(parents=True, exist_ok=True)
        (clip_dir / (clip.id + ".json")).write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    return report


def generate_animations(
    specs: Sequence[PromptSpec],
    styles: Dict[str, StylePreset],
    settings: Settings,
    options: AnimOptions,
    common: Optional[CommonBlock] = None,
    library: Optional[AnimationLibrary] = None,
) -> List[ClipReport]:
    """Draw the requested clips for every spec, reusing one HTTP session."""
    common = common if common is not None else load_common(settings.common_path)
    library = library if library is not None else load_animation_library(settings.animation_path)
    if not library.clips:
        raise HarnessError(
            "No animation clips defined in " + str(settings.animation_path)
            + " - cannot generate animation frames."
        )

    output_dir = Path(options.output_dir or settings.output_dir)
    reports: List[ClipReport] = []
    for spec in specs:
        style = resolve_style(spec, styles, options.style)
        if options.clips:
            requested: List[str] = list(options.clips)
        elif options.clips is None:
            requested = declared_clips(spec)
        else:
            requested = []

        if not requested:
            log.info(
                "%s: no animation requested - add 'animations:' to the spec or pass --clips.",
                spec.id,
            )
            reports.append(ClipReport(spec_id=spec.id, clip_id="", skipped="not requested"))
            continue

        try:
            clips = library.resolve(requested)
        except HarnessError as exc:
            log.error("%s: %s", spec.id, exc)
            reports.append(ClipReport(spec_id=spec.id, clip_id="", skipped=str(exc)))
            continue

        try:
            reference, run_dir = locate_reference(
                spec.id,
                output_dir,
                explicit=options.reference_png,
                run_dir=options.run_dirs.get(spec.id) or options.run_dir,
            )
        except HarnessError as exc:
            log.error("%s: %s", spec.id, exc)
            reports.append(ClipReport(spec_id=spec.id, clip_id="", skipped=str(exc)))
            continue

        for clip in clips:
            reports.append(
                generate_clip(
                    spec,
                    clip,
                    style,
                    settings,
                    options,
                    library,
                    reference=reference,
                    run_dir=run_dir,
                    common=common,
                )
            )
    return reports


def clip_summary_line(report: ClipReport) -> str:
    """One tidy line for the CLI summary."""
    if report.skipped and not report.frames:
        return "{:<26} {:<8} skipped ({})".format(report.spec_id, report.clip_id or "-", report.skipped[:60])
    return "{:<26} {:<8} frames={:<3} failures={:<3} calls={}".format(
        report.spec_id, report.clip_id, len(report.frames), len(report.failures), report.calls
    )
