"""Turn prompt specs into PNG files on disk."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .api import (
    ApiError,
    BaseClient,
    GeneratedImage,
    GenerationResult,
    client_for,
    generate_reference_with_retry,
    generate_with_retry,
    sniff_mime,
)
from .config import Settings
from .harness import (
    CommonBlock,
    HarnessError,
    PromptSpec,
    StylePreset,
    build_negative,
    build_request,
    load_common,
    plan_jobs,
    resolve_style,
)
from .postprocess import DEFAULT_KEY_COLOR, process_sprite, resize_max_side
from .paths import IMAGE_DIR_NAME, relative_to_root, relativize_paths

log = logging.getLogger(__name__)

_BULKY_KEYS = {"data", "b64_json"}

# Output layout: resource/image/<YYYYMMDD>/<run stamp>_<spec id>/<spec id>_<nn>.png
# Two levels under the tree, so the resource folder stays readable: one folder per
# day, and inside it one folder per generation run ("when it was made + what was
# made"). Stills go under resource/image/; the clips the video stages make go under
# resource/video/ with the very same <day>/<name>, so a run is two folders rather
# than one folder holding six kinds of file.
RUN_STAMP_FORMAT = "%Y%m%d-%H%M%S"
RUN_DAY_FORMAT = "%Y%m%d"


def run_stamp_now() -> str:
    """Timestamp used as the folder prefix for one generation run."""
    return datetime.now().strftime(RUN_STAMP_FORMAT)


def run_day(stamp: str) -> str:
    """The day folder a run stamp belongs to (``20260921-225916`` -> ``20260921``)."""
    digits = "".join(char for char in str(stamp) if char.isdigit())
    return digits[:8] or datetime.now().strftime(RUN_DAY_FORMAT)


def run_dir_for(output_dir: Path, spec_id: str, stamp: str) -> Path:
    """``resource/image/<day>/<stamp>_<spec id>`` - where one run's stills land."""
    return (
        Path(output_dir) / IMAGE_DIR_NAME / run_day(stamp) / (str(stamp) + "_" + spec_id)
    )


@dataclass
class GenerateOptions:
    """Per-run options coming from the CLI."""

    count: Optional[int] = None
    model: Optional[str] = None
    api_style: Optional[str] = None
    aspect_ratio: Optional[str] = None
    image_size: Optional[str] = None
    style: Optional[str] = None
    output_dir: Optional[Path] = None
    dry_run: bool = False
    # ``None`` -> take the value from hareness/common.yaml (pipeline block), so the
    # shared standard stays the single source of truth. CLI flags only override it.
    cutout: Optional[bool] = None
    key_color: Optional[str] = None
    cutout_tolerance: Optional[int] = None
    trim: Optional[bool] = None
    trim_padding: Optional[int] = None
    # Empty list = no extra sizes; None = take them from hareness/common.yaml.
    sizes: Optional[List[int]] = None
    # 0 = no cap; None = take the cap from hareness/common.yaml.
    max_source_px: Optional[int] = None
    write_metadata: bool = True
    # Reference art for this run: None/[] -> use whatever the spec declares.
    reference: Optional[List[Path]] = None
    # "edit" (redraw the reference) or "copy" (the reference *is* the asset).
    reference_mode: Optional[str] = None


def _flag(value: Any, default: bool) -> bool:
    """YAML booleans sometimes arrive as quoted strings - normalise them."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on")


def _number(value: Any, default: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


@dataclass
class CutoutPlan:
    """Effective background-removal settings: CLI flag > common.yaml > built-in default."""

    enabled: bool = True
    key_color: Optional[str] = DEFAULT_KEY_COLOR
    tolerance: int = 48
    despill: bool = True
    trim: bool = True
    trim_padding: int = 0
    alpha_threshold: int = 8


def resolve_cutout(options: "GenerateOptions", common: Optional[CommonBlock] = None) -> CutoutPlan:
    """Merge the CLI flags with the ``pipeline`` block of ``hareness/common.yaml``."""
    block = common if common is not None else CommonBlock(enabled=False)
    plan = CutoutPlan()
    plan.enabled = (
        bool(options.cutout)
        if options.cutout is not None
        else _flag(block.pipeline_value("background.enabled", True), True)
    )
    plan.key_color = (
        options.key_color
        if options.key_color is not None
        else (block.pipeline_value("background.key_color", DEFAULT_KEY_COLOR) or DEFAULT_KEY_COLOR)
    )
    plan.tolerance = (
        _number(options.cutout_tolerance, 48)
        if options.cutout_tolerance is not None
        else _number(block.pipeline_value("background.tolerance", 48), 48)
    )
    plan.despill = _flag(block.pipeline_value("background.despill", True), True)
    plan.trim = (
        _flag(options.trim, True)
        if options.trim is not None
        else _flag(block.pipeline_value("crop.trim", True), True)
    )
    plan.trim_padding = (
        _number(options.trim_padding, 0)
        if options.trim_padding is not None
        else _number(block.pipeline_value("crop.trim_padding", 0), 0)
    )
    plan.alpha_threshold = _number(block.pipeline_value("crop.alpha_threshold", 8), 8)
    return plan


def _sides(value: Any) -> Tuple[int, ...]:
    """Parse ``[512, 256]`` / ``"512, 256"`` into a tuple of positive ints."""
    if value in (None, ""):
        return ()
    items = value if isinstance(value, (list, tuple)) else str(value).replace(";", ",").split(",")
    sides: List[int] = []
    for item in items:
        number = _number(item, 0)
        if number > 0 and number not in sides:
            sides.append(number)
    return tuple(sides)


@dataclass
class SourcePlan:
    """Upper bound for the pixels the model is allowed to hand back."""

    enabled: bool = True
    max_side: int = 1024


def resolve_source(options: "GenerateOptions", common: Optional[CommonBlock] = None) -> SourcePlan:
    """Merge ``--max-px`` with the ``pipeline.source`` block of ``common.yaml``."""
    block = common if common is not None else CommonBlock(enabled=False)
    plan = SourcePlan()
    if options.max_source_px is not None:
        plan.max_side = _number(options.max_source_px, 1024)
        plan.enabled = plan.max_side > 0
        return plan
    plan.enabled = _flag(block.pipeline_value("source.enabled", True), True)
    plan.max_side = _number(block.pipeline_value("source.max_side", 1024), 1024)
    if plan.max_side <= 0:
        plan.enabled = False
    return plan


@dataclass
class SizePlan:
    """Fixed delivery sizes written next to the full-resolution crop."""

    enabled: bool = True
    max_sides: Tuple[int, ...] = (512, 256)
    allow_upscale: bool = True
    resample: str = "lanczos"


def resolve_sizes(options: "GenerateOptions", common: Optional[CommonBlock] = None) -> SizePlan:
    """Merge ``--sizes`` with the ``pipeline.sizes`` block of ``common.yaml``."""
    block = common if common is not None else CommonBlock(enabled=False)
    plan = SizePlan()
    if options.sizes is not None:
        plan.max_sides = _sides(list(options.sizes))
        plan.enabled = bool(plan.max_sides)
    else:
        plan.enabled = _flag(block.pipeline_value("sizes.enabled", True), True)
        plan.max_sides = _sides(block.pipeline_value("sizes.max_sides", [512, 256]))
    plan.allow_upscale = _flag(block.pipeline_value("sizes.allow_upscale", True), True)
    plan.resample = str(block.pipeline_value("sizes.resample", "lanczos"))
    return plan


def resolve_references(
    spec: PromptSpec,
    options: "GenerateOptions",
    settings: Settings,
) -> List[Path]:
    """The reference art one spec is drawn from; ``--reference`` beats the spec file.

    Paths written in the spec are relative to the project root (the folder that
    holds ``hareness/``), which is where the project keeps its master art.
    """
    requested: List[Any] = list(options.reference or []) or list(spec.references)
    root = Path(settings.harness_dir).resolve().parent
    resolved: List[Path] = []
    for item in requested:
        path = Path(str(item)).expanduser()
        if not path.is_absolute():
            path = root / path
        path = path.resolve()
        if not path.is_file():
            raise HarnessError(
                "Reference image not found: " + str(path) + " (spec '" + spec.id + "')"
            )
        if path not in resolved:
            resolved.append(path)
    return resolved


_COPY_MODES = {"copy", "as-is", "as_is", "direct", "original"}
_EDIT_MODES = {"edit", "redraw", "img2img", "image2image"}


def resolve_reference_mode(
    spec: PromptSpec,
    options: "GenerateOptions",
    common: Optional[CommonBlock] = None,
) -> str:
    """How to use the reference art: ``edit`` (redraw) or ``copy`` (use as-is)."""
    raw = (
        options.reference_mode
        or spec.reference_mode
        or (common.reference_mode if common is not None else "")
        or "edit"
    )
    mode = str(raw).strip().lower() or "edit"
    if mode in _COPY_MODES:
        return "copy"
    if mode in _EDIT_MODES:
        return "edit"
    raise HarnessError("Unknown reference_mode '" + str(raw) + "'. Use 'edit' or 'copy'.")


def _as_reference_result(path: Path) -> GenerationResult:
    """Wrap the reference file itself as the "model output" of a copy run."""
    data = Path(path).read_bytes()
    image = GeneratedImage(
        data=data, mime_type=sniff_mime(data) or "image/png", source=str(path)
    )
    return GenerationResult(
        images=[image],
        model="(reference art)",
        endpoint="local:" + str(path),
        api_style="local",
        request={"reference": str(path)},
    )


@dataclass
class JobReport:
    spec_id: str
    saved: List[Path] = field(default_factory=list)
    trimmed: List[Path] = field(default_factory=list)
    failures: List[str] = field(default_factory=list)
    prompts: List[str] = field(default_factory=list)
    calls: int = 0
    # Folder this run wrote into; the animation step reads the 512px reference from it.
    run_dir: Optional[Path] = None

    @property
    def ok(self) -> bool:
        return not self.failures


def _slim(value: Any, limit: int = 400) -> Any:
    """Keep metadata readable by trimming embedded base64 blobs."""
    if isinstance(value, dict):
        slimmed: Dict[str, Any] = {}
        for key, item in value.items():
            if key in _BULKY_KEYS and isinstance(item, str) and len(item) > limit:
                slimmed[key] = "<base64 " + str(len(item)) + " chars>"
            else:
                slimmed[key] = _slim(item, limit)
        return slimmed
    if isinstance(value, (list, tuple)):
        return [_slim(item, limit) for item in value]
    if isinstance(value, str) and len(value) > 4000:
        return value[:4000] + " ...<truncated>"
    return value


def _resolve_setting(
    override: Optional[str], spec_value: Optional[str], style_value: Optional[str]
) -> Optional[str]:
    """Resolve aspect-ratio / image-size, where "none" disables the defaults."""
    if override is None:
        return spec_value or style_value
    if str(override).strip().lower() in ("", "none", "default", "-"):
        return None
    return override


def _save_image(data: bytes, out_dir: Path, stem: str, extension: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / (stem + "." + extension)
    counter = 2
    while path.exists():
        path = out_dir / (stem + "_" + str(counter) + "." + extension)
        counter += 1
    path.write_bytes(data)
    return path


def _append_manifest(out_dir: Path, record: Dict[str, Any]) -> None:
    manifest = Path(out_dir) / "manifest.jsonl"
    with manifest.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def generate_spec(
    spec: PromptSpec,
    style: Optional[StylePreset],
    settings: Settings,
    options: GenerateOptions,
    client: Optional[BaseClient] = None,
    common: Optional[CommonBlock] = None,
    run_stamp: Optional[str] = None,
) -> JobReport:
    """Render every planned variation of one spec."""
    report = JobReport(spec_id=spec.id)
    common = common if common is not None else load_common(settings.common_path)
    jobs = plan_jobs(spec, options.count)
    model = options.model or spec.model or settings.model
    api_style = options.api_style or spec.api_style or settings.api_style
    aspect_ratio = _resolve_setting(
        options.aspect_ratio,
        spec.aspect_ratio,
        (style.aspect_ratio if style else None) or common.default_for("aspect_ratio"),
    )
    image_size = _resolve_setting(
        options.image_size,
        spec.image_size,
        (style.image_size if style else None) or common.default_for("image_size"),
    )
    cutout = resolve_cutout(options, common)
    sizes = resolve_sizes(options, common)
    source = resolve_source(options, common)
    references = resolve_references(spec, options, settings)
    reference_mode = resolve_reference_mode(spec, options, common)
    stamp = run_stamp or run_stamp_now()
    out_dir = run_dir_for(options.output_dir or settings.output_dir, spec.id, stamp)
    report.run_dir = out_dir

    client = client or client_for(settings, api_style)
    log.info(
        "%s: %s job(s) via %s (%s)",
        spec.id,
        len(jobs),
        api_style,
        model,
    )
    if references:
        log.info(
            "%s: reference art %s (%s mode)",
            spec.id,
            ", ".join(path.name for path in references),
            reference_mode,
        )

    for position, variation in enumerate(jobs, start=1):
        # The lock only goes in when reference art is actually attached, so the
        # model is never told to copy an image it cannot see.
        bundle = build_request(
            spec,
            style,
            common,
            variation,
            reference_lock=common.reference_lock if references else "",
        )
        prompt = bundle.prompt
        report.prompts.append(bundle.as_text())

        if options.dry_run:
            log.info("[dry-run] %s #%s\n%s", spec.id, position, bundle.as_text())
            continue

        if references and reference_mode == "copy":
            # "copy": the reference art *is* the standee. Nothing is drawn, so the
            # style cannot drift; the file only goes through keying / cropping /
            # the 512-256 delivery copies like any other render.
            result = _as_reference_result(references[0])
            log.info("%s #%s: using %s as-is", spec.id, position, references[0].name)
        else:
            report.calls += 1
            try:
                if references:
                    result = generate_reference_with_retry(
                        settings,
                        prompt,
                        references,
                        api_style=api_style,
                        model=model,
                        aspect_ratio=aspect_ratio,
                        image_size=image_size,
                        system=bundle.system or None,
                        max_side=source.max_side if source.enabled else None,
                    )
                else:
                    result = generate_with_retry(
                        client,
                        prompt,
                        model=model,
                        aspect_ratio=aspect_ratio,
                        image_size=image_size,
                        system=bundle.system or None,
                        max_side=source.max_side if source.enabled else None,
                    )
            except ApiError as exc:
                message = str(exc)[:400]
                log.error("%s #%s failed: %s", spec.id, position, message)
                report.failures.append(message)
                continue

        for image in result.images:
            stem = spec.id + "_" + str(len(report.saved) + 1).zfill(2)
            path = _save_image(image.data, out_dir, stem, image.extension)
            report.saved.append(path)

            processed: Dict[str, Any] = {}
            if cutout.enabled:
                processed = process_sprite(
                    path,
                    key_color=cutout.key_color,
                    tolerance=cutout.tolerance,
                    trim=cutout.trim,
                    padding=cutout.trim_padding,
                    despill=cutout.despill,
                    alpha_threshold=cutout.alpha_threshold,
                )
                if processed.get("trimmed"):
                    report.trimmed.append(path)

            scaled: Dict[str, Any] = {}
            if sizes.enabled:
                for side in sizes.max_sides:
                    written = resize_max_side(
                        path,
                        side,
                        allow_upscale=sizes.allow_upscale,
                        resample=sizes.resample,
                    )
                    if written:
                        scaled[str(side)] = list(written)

            record: Dict[str, Any] = {
                "spec": spec.id,
                "name": spec.name,
                "category": spec.category,
                "file": path.name,
                "folder": out_dir.name,
                "path": relative_to_root(path),
                "created": datetime.now().isoformat(timespec="seconds"),
                "model": model,
                "api_style": result.api_style,
                "endpoint": result.endpoint,
                "style": style.id if style else None,
                "aspect_ratio": aspect_ratio,
                "image_size": image_size,
                "tags": spec.tags,
                "variation": {k: v for k, v in variation.items() if not str(k).startswith("_")},
                "prompt": prompt,
                "system_prompt": bundle.system,
                "negative": build_negative(spec, style, variation, common),
                "attempts": result.attempts,
                "elapsed_s": round(result.elapsed, 2),
                "sha1": image.sha1,
                "bytes": len(image.data),
                "reference_mode": reference_mode if references else None,
                "references": [relative_to_root(path) for path in references],
                "key_color": cutout.key_color if cutout.enabled else None,
                "cutout": {
                    "enabled": cutout.enabled,
                    "tolerance": cutout.tolerance,
                    "despill": cutout.despill,
                    "trim": cutout.trim,
                    "trim_padding": cutout.trim_padding,
                    "alpha_threshold": cutout.alpha_threshold,
                },
                "scaled_sizes": scaled,
                "source_cap_px": source.max_side if source.enabled else None,
                "trimmed_to": processed.get("trimmed"),
                "final_size": processed.get("size"),
                "model_text": (result.text or "")[:2000],
            }
            log.info("saved %s (%.1f KB)", path, len(image.data) / 1024.0)

            if options.write_metadata and settings.write_metadata:
                metadata = dict(record)
                metadata["request"] = _slim(result.request)
                metadata["response"] = _slim(result.response)
                # One last sweep: the request body carries the file names it uploaded,
                # and a sidecar that names a drive letter is a sidecar that stops being
                # true the moment the project is copied.
                path.with_suffix(".json").write_text(
                    json.dumps(relativize_paths(metadata), ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                _append_manifest(
                    Path(options.output_dir or settings.output_dir),
                    relativize_paths(record),
                )

    return report


def generate_specs(
    specs: List[PromptSpec],
    styles: Dict[str, StylePreset],
    settings: Settings,
    options: GenerateOptions,
    common: Optional[CommonBlock] = None,
) -> List[JobReport]:
    """Render several specs, reusing one HTTP session per api style."""
    common = common if common is not None else load_common(settings.common_path)
    if not common.is_empty:
        log.info("common block loaded from %s", common.path)

    stamp = run_stamp_now()
    clients: Dict[str, BaseClient] = {}
    reports: List[JobReport] = []
    for spec in specs:
        style = resolve_style(spec, styles, options.style)
        style_key = (options.api_style or spec.api_style or settings.api_style or "images").lower()
        client = clients.get(style_key)
        if client is None:
            client = client_for(settings, style_key)
            clients[style_key] = client
        reports.append(
            generate_spec(
                spec, style, settings, options, client=client, common=common, run_stamp=stamp
            )
        )
    return reports
