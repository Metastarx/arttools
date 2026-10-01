"""Command line interface: ``python main.py <command> [options]``."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .api import ApiError, client_for, generate_with_retry, list_models
from .config import API_STYLES, AUTH_STYLES, IMAGE_MODELS, Settings, load_settings
from .generator import (
    GenerateOptions,
    generate_specs,
    resolve_cutout,
    resolve_reference_mode,
    resolve_sizes,
    resolve_source,
)
from .animation import (
    AnimOptions,
    ClipReport,
    clip_summary_line,
    declared_clips,
    generate_animations,
    load_animation_library,
)
from .harness import (
    HarnessError,
    PromptSpec,
    build_request,
    iter_specs,
    load_common,
    load_specs,
    load_styles,
    plan_jobs,
    resolve_style,
)
from .postprocess import process_sprite, resize_max_side

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(message)s"


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


def _setup_logging(verbose: int) -> None:
    level = logging.WARNING
    if verbose == 1:
        level = logging.INFO
    elif verbose >= 2:
        level = logging.DEBUG
    logging.basicConfig(level=level, format=LOG_FORMAT, datefmt="%H:%M:%S")
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def _common_flags() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--env", dest="env_file", help="path to a .env file (default: <project>/.env)")
    parser.add_argument("--key", help="API key (overrides RELAY_API_KEY)")
    parser.add_argument("--base-url", dest="base_url", help="relay origin, e.g. http://token.wd.com")
    parser.add_argument(
        "--model",
        help="image model to call (gpt-image-2.5-sunburst | gpt-image-2 | gpt-image-2.5-flare | any model on your platform)",
    )
    parser.add_argument("--api-style", dest="api_style", choices=API_STYLES, help="images (GPT image models), openai or gemini calling convention")
    parser.add_argument("--auth-style", dest="auth_style", choices=AUTH_STYLES, help="how the key is sent")
    parser.add_argument("--timeout", type=float, help="per-request timeout in seconds")
    parser.add_argument("--retries", type=int, dest="max_retries", help="extra attempts after the first try (default 2)")
    parser.add_argument("--retry-backoff", dest="retry_backoff", type=float, help="seconds between attempts")
    parser.add_argument("--proxy", help="http(s) proxy, e.g. http://127.0.0.1:7890")
    parser.add_argument("--out", dest="out_dir", help="output folder (default: resource/)")
    parser.add_argument("--extra-json", dest="extra_json", help="JSON object merged into the request body")
    parser.add_argument("-v", "--verbose", action="count", default=0, help="-v for progress, -vv for debug")
    return parser


def _add_generation_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--count", type=int, help="how many images per spec (default: spec 'count' or its variations)")
    parser.add_argument("--style", help="override the style preset (see 'list')")
    parser.add_argument(
        "--aspect-ratio", dest="aspect_ratio", help="e.g. 1:1, 16:9, 3:4, or none to send no aspect ratio"
    )
    parser.add_argument(
        "--image-size", dest="image_size", help="e.g. 1024x1024 (images style) or 1K/2K/4K (gemini), or none"
    )
    parser.add_argument("--dry-run", action="store_true", help="print the prompts without calling the API")
    parser.add_argument(
        "--cutout", dest="cutout", action="store_true", default=None,
        help="force background removal on (default: common.yaml pipeline.background.enabled)",
    )
    parser.add_argument("--no-cutout", dest="cutout", action="store_false", help="keep the raw render")
    parser.add_argument(
        "--key-color", "--cutout-color", dest="key_color", default=None,
        help="background colour to remove, e.g. #00FF00 or auto (default: common.yaml pipeline.background.key_color)",
    )
    parser.add_argument(
        "--cutout-tolerance", dest="cutout_tolerance", type=int, default=None,
        help="keying tolerance (default: common.yaml pipeline.background.tolerance)",
    )
    parser.add_argument(
        "--trim", dest="trim", action="store_true", default=None,
        help="force cropping to the visible asset bounds (default: common.yaml pipeline.crop.trim)",
    )
    parser.add_argument("--no-trim", dest="trim", action="store_false", help="keep the full canvas")
    parser.add_argument(
        "--trim-padding", dest="trim_padding", type=int, default=None,
        help="transparent margin kept after trimming (default: common.yaml pipeline.crop.trim_padding)",
    )
    parser.add_argument(
        "--max-px", dest="max_source_px", type=int, default=None,
        help="cap the pixels the model may return, longest side in px (default: common.yaml pipeline.source.max_side)",
    )
    parser.add_argument(
        "--no-max-px", dest="max_source_px", action="store_const", const=0,
        help="accept whatever resolution the model returns",
    )
    parser.add_argument(
        "--sizes", dest="sizes", default=None,
        help="extra copies into <size>/ sub folders, longest side in px, e.g. 512,256 (default: common.yaml pipeline.sizes)",
    )
    parser.add_argument(
        "--no-sizes", dest="sizes", action="store_const", const="",
        help="only keep the full-resolution crop",
    )
    parser.add_argument("--no-metadata", dest="write_metadata", action="store_false", help="skip .json sidecars")
    parser.add_argument(
        "--reference", dest="reference", action="append", default=None,
        help="source art to draw this asset from, e.g. --reference origin/image/main.png "
        "(repeatable; overrides the spec's own reference:)",
    )
    parser.add_argument(
        "--reference-mode", dest="reference_mode", choices=("edit", "copy"), default=None,
        help="edit = the model redraws the reference (default); "
        "copy = the reference file is the asset, post-processing only",
    )


def _add_animation_flags(parser: argparse.ArgumentParser) -> None:
    """Switches shared by 'gen --anim' and the standalone 'anim' command."""
    parser.add_argument(
        "--frames", type=int, default=None,
        help="override the frame count of every clip (default: hareness/animation.yaml)",
    )
    parser.add_argument(
        "--history", type=int, default=1,
        help="how many earlier frames are handed back as reference for continuity (default 1)",
    )
    parser.add_argument(
        "--no-align", dest="align", action="store_false", default=True,
        help="keep each frame's own bounds instead of re-anchoring the clip on one canvas",
    )
    parser.add_argument(
        "--no-sheet", dest="sheet", action="store_false", default=True,
        help="skip the horizontal strip sheet written next to the frames",
    )


def _add_anim_trigger(parser: argparse.ArgumentParser) -> None:
    """The opt-in switch: without it 'gen' never draws animation frames."""
    parser.add_argument(
        "--anim", dest="anim", nargs="?", const="default", default=None,
        help="also draw animation frames from the 512px portrait, e.g. --anim or --anim idle,walk",
    )


def build_parser() -> argparse.ArgumentParser:
    common = _common_flags()
    parser = argparse.ArgumentParser(
        prog="python main.py",
        description="Generate game art assets with GPT image models through a third-party relay: green-screen keying, transparent background, auto crop.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", parents=[common], help="show the available specs and style presets")

    show = sub.add_parser("show", parents=[common], help="print the assembled prompt(s)")
    show.add_argument("ids", nargs="*", help="spec ids (default: all)")
    show.add_argument("--count", type=int)
    show.add_argument("--style")
    show.add_argument("--no-common", dest="no_common", action="store_true", help="hide the shared common block")

    gen = sub.add_parser("gen", parents=[common], help="generate one or more specs")
    _add_generation_flags(gen)
    _add_animation_flags(gen)
    _add_anim_trigger(gen)
    gen.add_argument("ids", nargs="+", help="spec ids, e.g. hero_knight_2d monster_slime")

    gen_all = sub.add_parser("gen-all", parents=[common], help="generate every spec in the harness")
    _add_generation_flags(gen_all)
    _add_animation_flags(gen_all)
    _add_anim_trigger(gen_all)

    anim = sub.add_parser(
        "anim",
        parents=[common],
        help="draw animation frames from the 512px reference of an existing portrait",
    )
    anim.add_argument("ids", nargs="+", help="spec ids that already have a portrait under resource/")
    anim.add_argument(
        "--clips", default=None,
        help="clips to draw, e.g. idle,walk,hit (default: the spec's own animations: list)",
    )
    anim.add_argument("--run", default=None, help="read the reference from this run folder instead of the newest one")
    anim.add_argument("--from", dest="from_png", default=None, help="explicit reference PNG")
    _add_animation_flags(anim)
    _add_generation_flags(anim)

    probe = sub.add_parser("probe", parents=[common], help="check the key, base url and model list")
    probe.add_argument("--try-generate", dest="try_generate", action="store_true", help="also render one test image")

    sub.add_parser(
        "common", parents=[common], help="show the shared text that is sent with every request"
    )

    cut = sub.add_parser("cutout", parents=[common], help="key a flat background out of existing PNGs")
    cut.add_argument("paths", nargs="+", help="files and/or folders")
    cut.add_argument(
        "--key-color", "--color", dest="key_color", default=None,
        help="background colour to remove (default: common.yaml pipeline.background.key_color)",
    )
    cut.add_argument("--tolerance", type=int, default=None, help="keying tolerance (default: common.yaml)")
    cut.add_argument("--trim", dest="trim", action="store_true", default=None, help="crop to the asset bounds")
    cut.add_argument("--no-trim", dest="trim", action="store_false")
    cut.add_argument(
        "--trim-padding", dest="trim_padding", type=int, default=None,
        help="transparent margin kept after trimming",
    )
    cut.add_argument(
        "--sizes", dest="sizes", default=None,
        help="extra copies at a fixed longest side, e.g. 512,256 (default: common.yaml pipeline.sizes)",
    )
    cut.add_argument("--no-sizes", dest="sizes", action="store_const", const="")

    stage1 = sub.add_parser(
        "stage1", parents=[common], help="1/5 draw the character on a flat green screen"
    )
    stage1.add_argument("ids", nargs="+", help="spec id, e.g. hero_idle")
    stage1.add_argument("--count", type=int, help="how many variations to draw")
    stage1.add_argument("--style", help="override the style preset")
    stage1.add_argument("--dry-run", dest="dry_run", action="store_true",
                        help="print the prompt without calling the API")
    _add_stage_flags(stage1)

    stage2 = sub.add_parser(
        "stage2", parents=[common], help="2/5 key the green out, crop, write 512/256"
    )
    stage2.add_argument("paths", nargs="*",
                        help="green-screen artwork (default: 01_artwork/ of the run)")
    _add_stage_flags(stage2)

    stage3 = sub.add_parser(
        "stage3", parents=[common], help="3/5 fill with flat green and open a margin"
    )
    stage3.add_argument(
        "--source", action="append", default=None,
        help="artwork to animate (default: origin/image, or this run's 512px still)",
    )
    _add_stage_flags(stage3)

    stage4 = sub.add_parser(
        "stage4", parents=[common], help="4/5 render one Seedance clip per clip, per model"
    )
    _add_stage_flags(stage4)

    stage5 = sub.add_parser(
        "stage5", parents=[common], help="5/5 cut the clips into frames, key them, write 512/256"
    )
    _add_stage_flags(stage5)

    select = sub.add_parser(
        "select", parents=[common],
        help="keep only the frames worth importing, next to the full cut",
    )
    select.add_argument(
        "--frames", default=None,
        help="frames to keep, by number from 0, e.g. 0-40,45,50-70; empty means all of them",
    )
    select.add_argument(
        "--clear", action="store_true", help="drop the kept set and keep the full cut only",
    )
    _add_stage_flags(select)

    run = sub.add_parser("run", parents=[common], help="run the five stages end to end")
    run.add_argument("ids", nargs="*", help="spec id to draw (stage 1)")
    run.add_argument("--source", nargs="*", default=None,
                     help="green-screen artwork to start from instead of drawing")
    run.add_argument("--from", dest="from_stage", type=int, default=1, choices=(1, 2, 3, 4, 5),
                     help="first step to run (default 1)")
    run.add_argument("--to", dest="to_stage", type=int, default=5, choices=(1, 2, 3, 4, 5),
                     help="last step to run (default 5). \u2014\u2014from 2 --to 3 is the whole"
                          " of 阶段 2 原画处理 and stops before the video model is called")
    run.add_argument("--count", type=int)
    run.add_argument("--style")
    _add_stage_flags(run)

    # The prompt presets belong to the three commands that talk to the video model:
    # stage 3 picks them and records them, stage 4 uses them, and `run` does both.
    _add_prompt_flags(stage3)
    _add_prompt_flags(stage4)
    _add_prompt_flags(run)

    promote = sub.add_parser(
        "promote", parents=[common],
        help="keep a still, or a cut of frames, by copying it into origin/",
    )
    promote.add_argument(
        "source", help="a PNG under resource/image, or a cut under resource/video"
    )
    promote.add_argument(
        "--name", default=None,
        help="name to keep it under (default: <entity>_<clip>, from the source)",
    )
    promote.add_argument("--entity", default=None, help="the character, when the name should not say")
    promote.add_argument("--clip", default=None, help="the animation, when the name should not say")
    promote.add_argument("--force", action="store_true",
                         help="replace what is already in origin/")

    # ``normalize`` is ``promote`` for something that is already in origin - the same
    # renaming, the same 512 / 256 copies, the same sheets - so a folder somebody copied
    # in by hand comes out indistinguishable from one the pipeline wrote.
    normalize = sub.add_parser(
        "normalize", parents=[common],
        help="put a cut that is already in origin/ into the canonical shape, in place",
    )
    normalize.add_argument("source", help="a folder under origin/video")
    normalize.add_argument("--name", default=None, help="rename it to this")
    normalize.add_argument("--entity", default=None, help="the character, when the name should not say")
    normalize.add_argument("--clip", default=None, help="the animation, when the name should not say")
    normalize.add_argument("--force", action="store_true",
                           help="replace a folder this one would be renamed onto")

    runs = sub.add_parser(
        "runs", parents=[common], help="list the runs under resource/image and resource/video"
    )
    runs.add_argument("--json", dest="as_json", action="store_true",
                      help="print the listing as JSON")

    ui = sub.add_parser(
        "ui", parents=[common], help="open the local web console for the five stages"
    )
    ui.add_argument("--host", default=None, help="bind address (default: 127.0.0.1)")
    ui.add_argument("--port", type=int, default=None, help="port (default: 8765)")
    ui.add_argument(
        "--no-browser", dest="no_browser", action="store_true",
        help="do not open a browser window (the address is still printed)",
    )
    return parser


def _parse_extra_json(text: Optional[str]) -> Dict[str, Any]:
    if not text:
        return {}
    raw = text
    if raw.startswith("@"):
        raw = Path(raw[1:]).read_text(encoding="utf-8-sig")
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise SystemExit("--extra-json is not valid JSON: " + str(exc))
    if not isinstance(parsed, dict):
        raise SystemExit("--extra-json must be a JSON object.")
    return parsed


def _parse_sizes(text: Optional[str]) -> Optional[List[int]]:
    """``"512,256"`` -> ``[512, 256]``; ``""`` -> ``[]`` (turn the step off)."""
    if text is None:
        return None
    sides: List[int] = []
    for piece in str(text).replace(";", ",").split(","):
        piece = piece.strip()
        if not piece:
            continue
        try:
            value = int(float(piece))
        except ValueError:
            raise SystemExit("--sizes expects whole numbers, e.g. --sizes 512,256")
        if value > 0 and value not in sides:
            sides.append(value)
    return sides


def _parse_clips(text: Optional[str]) -> Optional[List[str]]:
    """``"idle,walk"`` -> ``["idle", "walk"]``; ``None`` -> follow the spec."""
    if text is None:
        return None
    return [
        piece.strip().lower()
        for piece in str(text).replace(";", ",").split(",")
        if piece.strip()
    ]


def _settings_from_args(args: argparse.Namespace) -> Settings:
    overrides: Dict[str, Any] = {}
    if getattr(args, "key", None):
        overrides["api_key"] = args.key
    if getattr(args, "base_url", None):
        overrides["base_url"] = args.base_url
    if getattr(args, "model", None):
        overrides["model"] = args.model
    if getattr(args, "api_style", None):
        overrides["api_style"] = args.api_style
    if getattr(args, "auth_style", None):
        overrides["auth_style"] = args.auth_style
    if getattr(args, "timeout", None):
        overrides["timeout"] = args.timeout
    if getattr(args, "max_retries", None) is not None:
        overrides["max_retries"] = args.max_retries
    if getattr(args, "retry_backoff", None) is not None:
        overrides["retry_backoff"] = args.retry_backoff
    if getattr(args, "proxy", None):
        overrides["proxy"] = args.proxy
    if getattr(args, "out_dir", None):
        overrides["output_dir"] = Path(args.out_dir).expanduser().resolve()
    extra = _parse_extra_json(getattr(args, "extra_json", None))
    if extra:
        overrides["extra_payload"] = extra

    env_file = Path(args.env_file).expanduser() if getattr(args, "env_file", None) else None
    return load_settings(env_file, **overrides)


def _load_harness(settings: Settings) -> tuple:
    specs = load_specs(settings.characters_dir)
    styles = load_styles(settings.styles_dir)
    common = load_common(settings.common_path)
    if not specs:
        raise SystemExit("No specs found in " + str(settings.characters_dir))
    return specs, styles, common


def cmd_list(args: argparse.Namespace, settings: Settings) -> int:
    specs, styles, common = _load_harness(settings)

    print("style presets (" + str(len(styles)) + ")")
    for preset in styles.values():
        if preset.base_path:
            tag = "继承 " + preset.base_path.name
        elif preset.inherit:
            tag = "独立风格（没有 _common.yaml）"
        else:
            tag = "独立风格（inherit_common: false）"
        print("  {:<20} {:<28} {}".format(preset.id, preset.name, tag))

    print("\nspecs (" + str(len(specs)) + ")")
    for spec in specs.values():
        print(
            "  {:<26} {:<12} style={:<18} variations={:<3} {}".format(
                spec.id,
                spec.category,
                spec.style or "-",
                len(spec.variations) or 1,
                spec.name,
            )
        )

    print(
        "\ncommon : "
        + ("on" if not common.is_empty else "off (file missing)")
        + "  ->  "
        + str(settings.common_path)
        + "   send_as_system="
        + str(common.send_as_system)
    )

    print("\nbase url : " + settings.base_url)
    print("endpoint : " + settings.url_for())
    print("model    : " + settings.model + " (" + settings.api_style + ")")
    print("api key  : " + settings.masked_key)
    print("harness  : " + str(settings.harness_dir))
    print("output   : " + str(settings.output_dir))
    return 0


def cmd_show(args: argparse.Namespace, settings: Settings) -> int:
    specs, styles, common = _load_harness(settings)
    selected: List[PromptSpec] = iter_specs(specs, args.ids) if args.ids else list(specs.values())
    used_common = common if not getattr(args, "no_common", False) else None

    for spec in selected:
        style = resolve_style(spec, styles, args.style)
        print("=" * 78)
        print(spec.id + "  |  " + (spec.name or "(unnamed)"))
        print("category: " + spec.category + "   style: " + (style.id if style else "(none)"))
        aspect = (
            spec.aspect_ratio
            or (style.aspect_ratio if style else None)
            or common.default_for("aspect_ratio")
        )
        print("aspect ratio: " + (aspect or "model default"))
        mode = resolve_reference_mode(spec, GenerateOptions(), common)
        print(
            "reference art: "
            + (", ".join(spec.references) + "  (mode: " + mode + ")" if spec.references else "(none - text to image)")
        )
        if spec.notes:
            print("notes: " + spec.notes)
        jobs = plan_jobs(spec, args.count)
        for index, variation in enumerate(jobs, start=1):
            bundle = build_request(
                spec,
                style,
                used_common,
                variation,
                reference_lock=(common.reference_lock if spec.references else ""),
            )
            print("\n--- variation " + str(index) + " of " + str(len(jobs)) + " ---")
            print(bundle.as_text())
    return 0


def cmd_common(args: argparse.Namespace, settings: Settings) -> int:
    common = load_common(settings.common_path)
    print("file           : " + str(settings.common_path))
    print("exists         : " + str(Path(settings.common_path).is_file()))
    print("enabled        : " + str(common.enabled))
    print("version        : " + str(common.version))
    print("send_as_system : " + str(common.send_as_system))
    print("defaults       : " + json.dumps(common.defaults, ensure_ascii=False))
    print("\n--- pipeline (给代码用的参数) ---")
    print(json.dumps(common.pipeline, ensure_ascii=False, indent=2) if common.pipeline else "(empty)")

    plan = resolve_cutout(GenerateOptions(), common)
    print("\n--- 生效规则 (effective) ---")
    print("cutout         : " + str(plan.enabled))
    print("key color      : " + str(plan.key_color))
    print("tolerance      : " + str(plan.tolerance))
    print("despill        : " + str(plan.despill))
    print("trim           : " + str(plan.trim) + "  padding=" + str(plan.trim_padding))
    print("alpha threshold: " + str(plan.alpha_threshold))
    source_plan = resolve_source(GenerateOptions(), common)
    print(
        "source cap     : "
        + (str(source_plan.max_side) + " px (最长边)" if source_plan.enabled else "off")
    )
    size_plan = resolve_sizes(GenerateOptions(), common)
    print(
        "sizes          : "
        + (", ".join(str(side) for side in size_plan.max_sides) if size_plan.enabled else "off")
        + "  (最长边，副本进 <size>/ 子目录，允许放大="
        + str(size_plan.allow_upscale)
        + ", "
        + size_plan.resample
        + ")"
    )
    scale = common.pipeline_section("scale")
    if scale:
        print("scale ruler    : 1 单位 = 标准人类可见高度 " + str(scale.get("human_reference_px")) + "px")
        guide = scale.get("size_guide")
        if isinstance(guide, dict):
            for key, value in guide.items():
                print("  {:<18} {} x human".format(key, value))
    if common.reuse.get("tell_new_session"):
        print("\n--- 新对话复制这句 (reuse.tell_new_session) ---")
        print(str(common.reuse.get("tell_new_session")).strip())
    print("\n--- preamble ---")
    print(common.preamble or "(empty)")
    print("\n--- requirements ---")
    print(common.requirements or "(empty)")
    print("\n--- negative ---")
    print(common.negative or "(empty)")
    print("\n--- reference_lock (附了参考图时才会发) ---")
    print(common.reference_lock or "(empty)")
    print("\nreference mode : " + str(common.reference_mode))
    return 0


def cmd_generate(args: argparse.Namespace, settings: Settings, all_specs: bool) -> int:
    specs, styles, common = _load_harness(settings)
    selected = list(specs.values()) if all_specs else iter_specs(specs, args.ids)

    options = GenerateOptions(
        count=args.count,
        model=args.model,
        api_style=args.api_style,
        aspect_ratio=args.aspect_ratio,
        image_size=args.image_size,
        style=args.style,
        output_dir=Path(args.out_dir).expanduser().resolve() if args.out_dir else settings.output_dir,
        dry_run=args.dry_run,
        cutout=args.cutout,
        key_color=args.key_color,
        cutout_tolerance=args.cutout_tolerance,
        trim=args.trim,
        trim_padding=args.trim_padding,
        sizes=_parse_sizes(args.sizes),
        max_source_px=args.max_source_px,
        write_metadata=args.write_metadata,
        reference=[Path(item).expanduser() for item in args.reference] if args.reference else None,
        reference_mode=args.reference_mode,
    )

    if options.dry_run:
        print("dry run - no API calls will be made")
    else:
        settings.require_key()
        print(
            "calling {} via {} ({})".format(
                options.model or settings.model,
                settings.base_url,
                options.api_style or settings.api_style,
            )
        )

    print(
        "common block: "
        + ("on  " + str(settings.common_path) if not common.is_empty else "off")
    )
    print(
        "reference art: "
        + (", ".join(args.reference) if args.reference else "per spec 'reference:' (if any)")
    )
    reports = generate_specs(selected, styles, settings, options, common=common)

    clip_reports: List[ClipReport] = []
    if args.anim is not None:
        # Opt-in: only the specs that asked for animation (or were named on the
        # command line) get frames, and they are drawn from the 512px portrait
        # that was just written.
        anim_options = AnimOptions(**vars(options))
        anim_options.clips = _parse_clips(args.anim)
        anim_options.frames = args.frames
        anim_options.history = args.history
        anim_options.align = args.align
        anim_options.sheet = args.sheet
        anim_options.run_dirs = {
            report.spec_id: report.run_dir for report in reports if report.run_dir
        }
        targets = [spec for spec in selected if _wants_animation(spec, anim_options.clips)]
        if not targets:
            print("\nno spec asked for animation (add 'animations:' to the spec or name clips with --anim idle,walk)")
        else:
            print(
                "\nanimation: "
                + (", ".join(anim_options.clips) if anim_options.clips else "per spec 'animations:'")
                + " for "
                + ", ".join(spec.id for spec in targets)
            )
            clip_reports = generate_animations(
                targets, styles, settings, anim_options, common=common
            )

    total_saved = sum(len(report.saved) for report in reports)
    total_failed = sum(len(report.failures) for report in reports)
    total_failed += sum(len(report.failures) for report in clip_reports)
    print("\nsummary")
    for report in reports:
        print(
            "  {:<26} images={:<3} failures={:<3} calls={}".format(
                report.spec_id, len(report.saved), len(report.failures), report.calls
            )
        )
    for report in clip_reports:
        print("  " + clip_summary_line(report))
    if not options.dry_run:
        print("\n" + str(total_saved) + " image(s) written under " + str(options.output_dir))
    if total_failed:
        print(str(total_failed) + " request(s) failed - re-run with -v for details.", file=sys.stderr)
        return 1
    return 0


def _wants_animation(spec: PromptSpec, clips: Optional[List[str]]) -> bool:
    """A bare ``--anim`` follows each spec's own list; ``--anim idle`` overrides it."""
    if clips:
        return True
    if clips is None:
        return bool(declared_clips(spec))
    return False


def cmd_anim(args: argparse.Namespace, settings: Settings) -> int:
    """Draw animation frames for portraits that already exist."""
    specs, styles, common = _load_harness(settings)
    library = load_animation_library(settings.animation_path)
    if not library.clips:
        raise SystemExit(
            "No animation clips defined in "
            + str(settings.animation_path)
            + " - add a 'clips:' block before running 'anim'."
        )
    selected = iter_specs(specs, args.ids)

    options = AnimOptions(
        model=args.model,
        api_style=args.api_style,
        aspect_ratio=args.aspect_ratio,
        image_size=args.image_size,
        style=args.style,
        output_dir=Path(args.out_dir).expanduser().resolve() if args.out_dir else settings.output_dir,
        dry_run=args.dry_run,
        cutout=args.cutout,
        key_color=args.key_color,
        cutout_tolerance=args.cutout_tolerance,
        trim=args.trim,
        trim_padding=args.trim_padding,
        sizes=_parse_sizes(args.sizes),
        max_source_px=args.max_source_px,
        write_metadata=args.write_metadata,
        reference=[Path(item).expanduser() for item in args.reference] if args.reference else None,
        reference_mode=args.reference_mode,
        clips=_parse_clips(args.clips),
        frames=args.frames,
        reference_png=Path(args.from_png).expanduser() if args.from_png else None,
        run_dir=Path(args.run).expanduser() if args.run else None,
        history=args.history,
        align=args.align,
        sheet=args.sheet,
    )

    print("clips available : " + ", ".join(library.available))
    print("clips requested : " + (", ".join(options.clips) if options.clips else "per spec 'animations:'"))
    print(
        "reference       : "
        + (str(options.reference_png) if options.reference_png else "newest 512px portrait per spec")
    )
    if options.dry_run:
        print("dry run - no API calls will be made")
    else:
        settings.require_key()

    reports = generate_animations(selected, styles, settings, options, common=common)

    frames = sum(len(report.frames) for report in reports)
    failed = sum(len(report.failures) for report in reports)
    print("\nsummary")
    for report in reports:
        print("  " + clip_summary_line(report))
    if not options.dry_run:
        print("\n" + str(frames) + " frame(s) written")
    if failed:
        print(str(failed) + " frame(s) failed - re-run with -v for details.", file=sys.stderr)
        return 1
    return 0


def cmd_probe(args: argparse.Namespace, settings: Settings) -> int:
    print("base url : " + settings.base_url)
    print("endpoint : " + settings.url_for())
    print("api key  : " + settings.masked_key)

    try:
        status, payload, url = list_models(settings)
    except ApiError as exc:
        print("could not reach the platform: " + str(exc))
        return 1

    print("GET " + url + " -> HTTP " + str(status))
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            ids = [str(item.get("id")) for item in data if isinstance(item, dict)]
            print("models   : " + str(len(ids)))
            image_ids = sorted(item for item in ids if "image" in item.lower())
            if image_ids:
                print("image models: " + ", ".join(image_ids[:40]))
            wanted_models = list(dict.fromkeys([args.model or settings.model] + list(IMAGE_MODELS)))
            for wanted in wanted_models:
                print("  has " + str(wanted) + ": " + str(wanted in ids))
        else:
            print("body     : " + json.dumps(payload, ensure_ascii=False)[:600])
    else:
        print("body     : " + str(payload)[:600])

    if status != 200:
        print("the key was rejected (HTTP " + str(status) + "). Check --base-url / RELAY_BASE_URL first.")

    if getattr(args, "try_generate", False) and status == 200:
        client = client_for(settings, args.api_style)
        try:
            result = generate_with_retry(
                client,
                "A flat red apple icon, centered, on a plain white background.",
                model=args.model or settings.model,
                aspect_ratio="1:1",
            )
        except ApiError as exc:
            print("generation FAILED: " + str(exc)[:400])
            return 1
        print(
            "generation OK: "
            + str(len(result.images))
            + " image(s), "
            + str(round(result.elapsed, 1))
            + "s, attempts="
            + str(result.attempts)
        )
        if result.text:
            print("model text: " + result.text[:200])

    return 0 if status == 200 else 1


def _is_size_copy(root: Path, file: Path) -> bool:
    """True for the derived ``<root>/512/...``, ``<root>/256/...`` copies."""
    try:
        parts = file.relative_to(root).parts[:-1]
    except ValueError:
        return False
    return any(part.isdigit() for part in parts)


def cmd_cutout(args: argparse.Namespace, settings: Settings) -> int:
    targets: List[Path] = []
    for raw in args.paths:
        path = Path(raw).expanduser()
        if path.is_dir():
            targets.extend(sorted(p for p in path.rglob("*.png") if not _is_size_copy(path, p)))
        else:
            targets.append(path)

    common = load_common(settings.common_path)
    options = GenerateOptions(
        key_color=args.key_color,
        cutout_tolerance=args.tolerance,
        trim=args.trim,
        trim_padding=args.trim_padding,
        sizes=_parse_sizes(args.sizes),
    )
    plan = resolve_cutout(options, common)
    size_plan = resolve_sizes(options, common)
    print(
        "using key="
        + str(plan.key_color)
        + " tolerance="
        + str(plan.tolerance)
        + " trim="
        + str(plan.trim)
        + " padding="
        + str(plan.trim_padding)
        + " sizes="
        + (", ".join(str(side) for side in size_plan.max_sides) if size_plan.enabled else "off")
        + " (from "
        + str(settings.common_path)
        + ")"
    )

    keyed = 0
    trimmed = 0
    scaled = 0
    for path in targets:
        if not path.is_file():
            print("skipped (not found): " + str(path))
            continue
        summary = process_sprite(
            path,
            key_color=plan.key_color,
            tolerance=plan.tolerance,
            trim=plan.trim,
            padding=plan.trim_padding,
            despill=plan.despill,
            alpha_threshold=plan.alpha_threshold,
        )
        keyed += 1 if summary["keyed"] else 0
        trimmed += 1 if summary["trimmed"] else 0

        if size_plan.enabled:
            for side in size_plan.max_sides:
                written = resize_max_side(
                    path,
                    side,
                    allow_upscale=size_plan.allow_upscale,
                    resample=size_plan.resample,
                )
                scaled += 1 if written else 0
    print(
        "processed "
        + str(len(targets))
        + " file(s), background removed in "
        + str(keyed)
        + ", trimmed "
        + str(trimmed)
        + ", extra sizes written "
        + str(scaled)
    )
    return 0
# --------------------------------------------------------------------------------------
# The five stages
# --------------------------------------------------------------------------------------


def _add_stage_flags(parser: argparse.ArgumentParser) -> None:
    """Flags shared by the stage commands.

    ``None`` is the default for every one of them, which means "use the value in
    hareness/common.yaml" - so the file, not the command line, is where the project
    standard lives.
    """
    parser.add_argument(
        "--key-color", "--color", dest="key_color", default=None,
        help="backdrop colour (default: common.yaml pipeline.background.key_color)",
    )
    parser.add_argument(
        "--tolerance", type=float, default=None,
        help="how close to the backdrop still counts as background"
             " (default: common.yaml pipeline.background.tolerance)",
    )
    parser.add_argument("--sizes", default=None, help="delivery sizes, e.g. 512,256")
    parser.add_argument("--no-sizes", dest="sizes", action="store_const", const="")
    parser.add_argument("--clips", default=None, help="clips, e.g. idle,walk,hit")
    parser.add_argument("--models", nargs="*", default=None, help="Seedance model names")
    parser.add_argument(
        "--fill", type=float, default=None,
        help="share of the video canvas the character takes (default: 0.7)",
    )
    parser.add_argument("--facing", choices=("left", "right"), default=None)
    parser.add_argument("--seconds", type=int, default=None, help="clip length, in seconds")
    parser.add_argument("--resolution", default=None)
    parser.add_argument(
        "--skip-frames", dest="skip_frames", type=int, default=None,
        help="leading frames to drop when cutting a clip (default: 2)",
    )
    parser.add_argument("--flatten-tolerance", dest="flatten_tolerance", type=float, default=None)
    parser.add_argument(
        "--no-flatten", dest="flatten", action="store_false", default=None,
        help="do not snap the backdrop of a video frame to one colour code",
    )
    parser.add_argument(
        "--no-trim", dest="trim", action="store_false", default=None,
        help="keep the full canvas instead of cropping to the visible art",
    )
    parser.add_argument("--run", dest="run_dir", default=None,
                        help="run folder (default: the newest one under the output dir)")
    parser.add_argument("--name", default=None, help="name for a new run folder")
    parser.add_argument("--stamp", default=None,
                        help="timestamp for a new run folder, YYYYMMDD-HHMMSS")
    parser.add_argument(
        "--reference", action="append", default=None, metavar="CLIP=PATH",
        help="use this artwork for one clip, e.g. --reference walk=origin/walk.png",
    )
    parser.add_argument("--force", action="store_true",
                        help="redo clips that are already on disk (costs credits again)")
    parser.add_argument("--wait", type=int, default=1800, help="seconds to wait for one clip")
    parser.add_argument("--poll", type=int, default=15, help="seconds between status checks")


def _add_prompt_flags(parser: argparse.ArgumentParser) -> None:
    """The two ways to tell the video model something that is not in the code.

    Both are about *style consistency*, which is the hard part of this pipeline: the
    built-in prompt in :mod:`src.video` carries the load-bearing instructions, and
    everything here is added after it.
    """
    parser.add_argument(
        "--preset", action="append", default=None, metavar="NAME",
        help="a prompt preset from hareness/prompts, e.g. --preset walk_side"
             " (repeatable; 'common' is always applied)",
    )
    parser.add_argument(
        "--extra-prompt", dest="extra_prompt", default=None,
        help="extra instruction for the video model, appended after the built-in prompt",
    )


def _prompt_selection(
    args: argparse.Namespace, settings: Settings, run: Optional[Any] = None
) -> Tuple[List[str], str, str]:
    """``--preset`` / ``--extra-prompt`` -> ``(names, typed text, whole prompt tail)``.

    With neither flag given, a run's own record is the fallback: the presets are picked
    once, on the video-input step, and the video step finds them again here. That is what
    keeps ``stage4 --run <run>`` - the command the log tells people to paste - from
    quietly rendering without the style instructions that were chosen on the page.

    ``common`` always comes back in ``names``: it is the house file and is not optional.
    """
    from src import prompts as prompt_presets

    names = [str(name).strip() for name in (getattr(args, "preset", None) or [])
             if str(name).strip()]
    extra = str(getattr(args, "extra_prompt", None) or "").strip()
    # ``common`` is applied whether or not anybody asks for it, so naming it is not a
    # statement about this clip. Treating it as one would mean ``--preset common`` -
    # which is what a page that always sends its default ends up passing - silently
    # suppresses the fallback below and drops the presets that were picked earlier.
    said_something = any(
        name.lower() != prompt_presets.COMMON_NAME for name in names
    )
    if not said_something and not extra and run is not None:
        from src import stages

        recorded = stages.recorded_prompt(run)
        names = [str(name) for name in (recorded.get("presets") or [])]
        extra = str(recorded.get("extra") or "")
    folder = prompt_presets.preset_dir(settings.harness_dir)
    try:
        chosen = prompt_presets.resolve(folder, names)
    except ValueError as exc:
        raise SystemExit(str(exc))
    return (
        [preset.name for preset in chosen],
        extra,
        prompt_presets.combine(chosen, extra),
    )


def _newest_run(settings: Settings) -> Optional[Any]:
    """The most recently started run, by day folder and by the stamp in its name.

    Looks in both halves of the work tree and takes the newest of either, so
    ``stage5`` with no ``--run`` finds the clip that was just rendered even though the
    stills it came from live in the other tree.
    """
    from src import stages
    from src.paths import IMAGE_DIR_NAME, VIDEO_DIR_NAME

    resource = Path(settings.output_dir)
    found: List[Tuple[str, str]] = []
    for kind in (IMAGE_DIR_NAME, VIDEO_DIR_NAME):
        base = resource / kind
        if not base.is_dir():
            continue
        for day in base.iterdir():
            if not (day.is_dir() and len(day.name) == 8 and day.name.isdigit()):
                continue
            for run in day.iterdir():
                if run.is_dir() and run.name[:8].isdigit():
                    found.append((day.name, run.name))
    if not found:
        return None
    day, name = sorted(set(found))[-1]
    return stages.Run(resource=resource, key=day + "/" + name)


def _run_of(args: argparse.Namespace, settings: Settings, required: bool = True) -> Any:
    """``--run`` if it was given, otherwise the newest run in the work tree."""
    from src import stages

    value = getattr(args, "run_dir", None)
    if value:
        try:
            return stages.resolve_run(settings.output_dir, value)
        except ValueError as exc:
            raise SystemExit(str(exc))
    found = _newest_run(settings)
    if found is None:
        if not required:
            return None
        raise SystemExit(
            "resource 里还没有 run - 先用 stage1 画一张，或者用 --run <日期>/<名字> 指定"
        )
    print("run        : " + found.key + "  (最新的一个；用 --run 换一个)")
    return found


def _stage_settings(args: argparse.Namespace, settings: Settings) -> Dict[str, Any]:
    """CLI flag > hareness/common.yaml > built-in default, for every stage knob."""
    from src import stages

    values = stages.video_defaults(settings.common_path)
    values["key_color"] = getattr(args, "key_color", None) or stages.default_key_color(
        settings.common_path
    )
    sizes = _parse_sizes(getattr(args, "sizes", None))
    values["sizes"] = list(stages.DEFAULT_SIZES) if sizes is None else sizes
    values["flatten"] = True if getattr(args, "flatten", None) is None else bool(args.flatten)
    values["trim"] = True if getattr(args, "trim", None) is None else bool(args.trim)
    if getattr(args, "clips", None):
        values["clips"] = _parse_clips(args.clips) or list(values["clips"])
    if getattr(args, "models", None):
        values["models"] = [str(name).strip() for name in args.models if str(name).strip()]
    for flag, cast in (
        ("fill", float),
        ("seconds", int),
        ("skip_frames", int),
        ("flatten_tolerance", float),
        ("tolerance", float),
        ("facing", str),
        ("resolution", str),
    ):
        value = getattr(args, flag, None)
        if value is not None:
            values[flag] = cast(value)
    return values


def _reference_map(args: argparse.Namespace) -> Dict[str, Path]:
    """``--reference walk=side.png`` -> ``{"walk": Path("side.png")}``."""
    mapping: Dict[str, Path] = {}
    for item in getattr(args, "reference", None) or []:
        clip, separator, raw = str(item).partition("=")
        if not separator:
            raise SystemExit(
                "--reference wants CLIP=PATH, e.g. --reference walk=origin/walk.png"
            )
        mapping[clip.strip()] = Path(raw).expanduser()
    return mapping


def cmd_stage1(args: argparse.Namespace, settings: Settings) -> int:
    from src import stages

    from src.harness import load_specs

    known = load_specs(settings.characters_dir)
    wanted: List[str] = []
    for raw in args.ids:
        name = str(raw).strip()
        if name not in known:
            raise SystemExit(
                "unknown spec '" + name + "'. Available: " + ", ".join(sorted(known))
            )
        if name not in wanted:
            wanted.append(name)

    # Several specs is several runs, one per character: each drawing is its own thing to
    # judge, and the page offers a list to tick through rather than asking twice.
    for index, spec_id in enumerate(wanted):
        if index:
            print("")
        report = stages.stage1_artwork(
            spec_id, settings, count=args.count, style=args.style,
            dry_run=args.dry_run, stamp=args.stamp,
        )
        if len(wanted) > 1:
            print("spec       : " + spec_id)
        print("run        : " + report["run"])
        print("folder     : " + str(report["run_dir"]))
        print("artwork    : " + str(len(report["artwork"])) + " file(s)")
        for path in report["artwork"]:
            print("  " + path)
        if args.dry_run:
            print("dry run - nothing was drawn")
        elif not report["artwork"]:
            print("nothing was drawn: " + json.dumps(report["failures"], ensure_ascii=False))
        print('next       : python main.py run --from 2 --to 3 --run "'
              + report["run"] + '"   (阶段 2 原画处理)')
    return 0


def cmd_stage2(args: argparse.Namespace, settings: Settings) -> int:
    from src import stages

    values = _stage_settings(args, settings)
    items = [Path(raw).expanduser() for raw in (args.paths or [])]
    if getattr(args, "run_dir", None) or not items:
        run = _run_of(args, settings)
    else:
        run = stages.new_run(settings.output_dir, args.name or items[0].stem, args.stamp)
        print("run        : " + run.key + "  (new)")
    report = stages.stage2_artwork_ready(
        run, items=items or None, key_color=values["key_color"], sizes=values["sizes"],
        tolerance=values["tolerance"], trim=values["trim"],
    )
    print("keyed out  : " + str(report["background_removed"]) + " of " + str(report["frames"]))
    if report["already_transparent"]:
        print("             " + str(report["already_transparent"])
              + " already had a transparent background - cropped as they are")
    for item in report["not_keyed"]:
        if item.get("already_transparent"):
            continue
        print("  NOT keyed: " + item["file"] + " - " + str(item["reason"]))
    print("cropped    : " + str(report["cropped"]) + " file(s)")
    print("sizes      : " + (", ".join(
        str(side) + "px=" + str(count) for side, count in report["sizes"].items()
    ) or "(off)"))
    print('next       : python main.py stage3 --run "' + run.key + '"'
          '   (阶段 2 原画处理的后半步；两步一起跑就是 --from 2 --to 3)')
    return 0


def cmd_stage3(args: argparse.Namespace, settings: Settings) -> int:
    from src import stages

    values = _stage_settings(args, settings)
    items = [Path(raw).expanduser() for raw in (args.source or [])] or None
    if getattr(args, "run_dir", None):
        run = _run_of(args, settings)
    else:
        # No run named: this is the normal way in. Stage 3 animates a still that was
        # picked in the page and promoted to origin/image, so with nothing named that is
        # what it animates, and the clip needs a run of its own to land in.
        if items is None:
            items = stages.origin_stills()
        if not items:
            raise SystemExit(
                "resource 里还没有 run，origin/image 里也没有原稿 —— 先在页面上挑一张"
                "转录到 origin，或者用 --source 指定一张"
            )
        run = stages.new_run(settings.output_dir, args.name or items[0].stem, args.stamp)
        print("run        : " + run.key + "  (new)")
    names, extra, _ = _prompt_selection(args, settings, run)
    print("presets    : " + (", ".join(names) if names else "(none)"))
    report = stages.stage3_video_input(
        run, items=items, key_color=values["key_color"], fill=values["fill"],
        presets=names, extra_prompt=extra,
        preset_dir=stages.preset_folder(settings.harness_dir),
    )
    for item in report["items"]:
        print("  {:<24} canvas {}  character {}  on {}".format(
            Path(item["output"]).name, item["canvas"], item["character"], item["color"]))
    print('next       : python main.py stage4 --run "' + run.key + '"   (阶段 3 视频生成)')
    return 0


def cmd_stage4(args: argparse.Namespace, settings: Settings) -> int:
    from src import stages

    values = _stage_settings(args, settings)
    run = _run_of(args, settings)
    print("clips      : " + ", ".join(values["clips"]))
    print("models     : " + ", ".join(values["models"]))
    names, _, extra = _prompt_selection(args, settings, run)
    print("presets    : " + (", ".join(names) if names else "(none)"))
    report = stages.stage4_video(
        run, settings=settings, clips=values["clips"], models=values["models"],
        key_color=values["key_color"], facing=values["facing"], seconds=values["seconds"],
        resolution=values["resolution"], references=_reference_map(args),
        force=args.force, timeout=settings.timeout, wait_s=args.wait, poll_s=args.poll,
        extra_prompt=extra,
    )
    for job in report["runs"]:
        if job.get("ok"):
            print("  {}  {}{}".format(
                job["clip"], job["model"],
                "  (already there)" if job.get("skipped") else ""))
        else:
            print("  {}  {}  FAILED: {}".format(
                job["clip"], job["model"], str(job.get("error"))[:200]))
    if all(job.get("ok") for job in report["runs"]):
        print('next       : python main.py stage5 --run "' + run.key + '"   (阶段 4 视频处理)')
    return 0


def _parse_frames(text: Optional[str]) -> List[int]:
    """``"0-3,7,10-"`` -> ``[0, 1, 2, 3, 7, 10, 11, ...]``.

    Ranges are written the way the page writes them, which is the way a person writes
    them: ``0-40`` is forty-one frames, and a trailing dash means "to the end".
    """
    def number(piece: str) -> int:
        try:
            return int(piece)
        except ValueError:
            raise SystemExit("非法的帧号：%s —— 写成 --frames “0-40,45,50-70” 这样就好" % piece)

    numbers: List[int] = []
    for piece in str(text or "").replace(";", ",").split(","):
        piece = piece.strip()
        if not piece:
            continue
        if "-" in piece:
            head, _, tail = piece.partition("-")
            head = head.strip()
            if not head:
                continue
            start = number(head)
            stop = number(tail.strip()) if tail.strip() else start
            step = 1 if stop >= start else -1
            numbers.extend(range(start, stop + step, step))
        else:
            numbers.append(number(piece))
    return numbers


def cmd_select(args: argparse.Namespace, settings: Settings) -> int:
    from src import stages

    values = _stage_settings(args, settings)
    run = _run_of(args, settings)
    clips = _parse_clips(args.clips) if args.clips else None
    models = [str(name).strip() for name in args.models] if args.models else None

    jobs = stages._cut_jobs(run, clips, models)
    if not jobs:
        raise SystemExit("这个 run 里没有切好的帧，先跑 stage5")

    wanted = _parse_frames(args.frames)
    total = 0
    for clip, model, _ in jobs:
        frames = stages.cut_frames(run.stage_dir(5) / clip / model)
        if not frames:
            print("  %s/%s: 还没有帧，跳过" % (clip, model))
            continue
        keep = wanted if wanted else list(range(len(frames)))
        report = stages.select_frames(
            run, clip=clip, model=model, keep=keep,
            sizes=values["sizes"], key_color=values["key_color"],
            tolerance=values["tolerance"], clear=args.clear,
        )
        if report.get("cleared"):
            where = report.get("retired")
            print("  %s/%s: 已清掉选用帧%s" % (
                clip, model,
                "（挪到 %s，没删）" % where if where else "（本来就没有）"))
            continue
        total += report["frames"]
        stats = report.get("stats") or {}
        print("  %s/%s: 留下 %d / %d 帧  canvas %s  height spread %s%%  drift %spx" % (
            clip, model, report["frames"], report["full_frames"], report["canvas"],
            stats.get("height_spread_pct"), stats.get("drift_px")))
        print("      " + str(stages.project_path(run.stage_dir(5) / clip / model / stages.SELECT_DIR)))
    if not args.clear:
        print("done       : %d frames kept" % total)
    return 0


def cmd_stage5(args: argparse.Namespace, settings: Settings) -> int:
    from src import stages

    values = _stage_settings(args, settings)
    run = _run_of(args, settings)
    report = stages.stage5_frames(
        run,
        clips=_parse_clips(args.clips) if args.clips else None,
        models=[str(name).strip() for name in args.models] if args.models else None,
        key_color=values["key_color"],
        sizes=values["sizes"],
        skip_frames=values["skip_frames"],
        flatten=values["flatten"],
        flatten_tolerance=values["flatten_tolerance"],
        tolerance=values["tolerance"],
        trim=values["trim"],
    )
    for clip in report["clips"]:
        stats = clip.get("stats") or {}
        print("  {}/{}: {} frames  canvas {}".format(
            clip["clip"], clip["model"], clip["frames"], clip["canvas"]))
        print("      height spread {}%   drift {}px   frames on the edge {}   loop {}%".format(
            stats.get("height_spread_pct"), stats.get("drift_px"),
            stats.get("frames_touching_edge"), stats.get("first_vs_last_changed_pct")))
        if stats.get("loop_best_frame"):
            print("      the cycle closes on frame {} ({}% apart from frame 1)".format(
                stats["loop_best_frame"], stats.get("loop_best_changed_pct")))
        if stats.get("frames_touching_edge"):
            print("      " + str(stats["frames_touching_edge"])
                  + " frame(s) have the character against the edge - the model cropped him")
        if stats.get("frames_empty"):
            print("      " + str(stats["frames_empty"]) + " empty frame(s) - check the keying")
    print("done       : " + str(stages.project_path(run.stage_dir(5))))
    return 0


def cmd_run(args: argparse.Namespace, settings: Settings) -> int:
    from src import stages

    if not args.ids and not args.source and not args.run_dir:
        raise SystemExit(
            "pass a spec id to draw, or --source <green-screen png> to start from art you"
            " already have, or --run <folder> to carry on"
        )
    if args.to_stage < args.from_stage:
        raise SystemExit("--to 比 --from 还早，这样一步也跑不了："
                         "--from {} --to {}".format(args.from_stage, args.to_stage))
    values = _stage_settings(args, settings)
    names, extra, _ = _prompt_selection(args, settings)
    report = stages.run_pipeline(
        settings,
        presets=names, extra_prompt=extra,
        spec_id=args.ids[0] if args.ids else None,
        source=[Path(raw).expanduser() for raw in (args.source or [])] or None,
        name=args.name, run=args.run_dir,
        stamp=args.stamp, from_stage=args.from_stage, to_stage=args.to_stage,
        clips=values["clips"],
        models=values["models"], sizes=values["sizes"], fill=values["fill"],
        facing=values["facing"], key_color=values["key_color"], count=args.count,
        style=args.style, seconds=values["seconds"], resolution=values["resolution"],
        references=_reference_map(args), skip_frames=values["skip_frames"],
        flatten_tolerance=values["flatten_tolerance"], force=args.force,
        tolerance=values["tolerance"], trim=values["trim"], flatten=values["flatten"],
    )
    print("")
    print("run        : " + report["run"])
    print("stills     : " + str(report["image_dir"]))
    print("clips      : " + str(report["video_dir"]))
    print("key colour : " + report["key_color"])
    if report.get("presets"):
        print("presets    : " + ", ".join(report["presets"]))
    frames = report["stages"].get("5_frames", {}).get("clips", [])
    for clip in frames:
        stats = clip.get("stats") or {}
        print("  {}/{}\t{} frames\tdrift {}px\theight {}%\tedge {}\tloop {}%".format(
            clip["clip"], clip["model"], clip["frames"], stats.get("drift_px"),
            stats.get("height_spread_pct"), stats.get("frames_touching_edge"),
            stats.get("first_vs_last_changed_pct")))
    print("report     : " + str(report["image_dir"]) + "/run.json")
    return 0


def _project_paths(settings: Settings) -> Any:
    """The project folders, with --out and --harness taken into account."""
    from dataclasses import replace

    from src.paths import load_paths

    return replace(
        load_paths(),
        resource=Path(settings.output_dir).resolve(),
        harness=Path(settings.harness_dir).resolve(),
    )


def cmd_promote(args: argparse.Namespace, settings: Settings) -> int:
    """promote <path> - take something out of the work tree and keep it.

    Everything under resource can be regenerated, so nothing in it is precious;
    this is the one command that says "this one is", and it is also the switch between
    the two halves of the pipeline - promote a still, and stage 3 has something to
    animate.
    """
    from src import library

    paths = _project_paths(settings)
    source = Path(args.source).expanduser()
    if not source.is_absolute():
        source = paths.root / source
    try:
        result = library.promote(
            paths, source, args.name,
            entity=args.entity, clip=args.clip, overwrite=args.force,
        )
    except library.PromoteConflict as conflict:
        print(str(conflict) + " —— 加 --force 覆盖它", file=sys.stderr)
        return 1
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2
    print("kept       : " + str(result["name"]) + "  (" + str(result["kind"]) + ")")
    print("origin     : " + str(result["target"]))
    print("from       : " + str(result["from"]))
    if result.get("frames"):
        print("frames     : " + str(result["frames"]))
    return 0


def cmd_normalize(args: argparse.Namespace, settings: Settings) -> int:
    """normalize <folder> - tidy a cut that is already in origin.

    The answer to "I copied a folder of frames in from somewhere else": the frames are
    renumbered ``<entity>_<clip>_001.png``, the 512 / 256 copies and the two sheets are
    built beside them, and ``meta.json`` is written - in place, because that is where it
    already is. Reading never needed this; it is only about making the folder tidy.
    """
    from src import library

    paths = _project_paths(settings)
    source = Path(args.source).expanduser()
    if not source.is_absolute():
        source = paths.root / source
    try:
        result = library.normalize(
            paths, source, name=args.name,
            entity=args.entity, clip=args.clip, overwrite=args.force,
        )
    except library.PromoteConflict as conflict:
        print(str(conflict) + " —— 加 --force 覆盖它", file=sys.stderr)
        return 1
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2
    print("normalized : " + str(result["name"]))
    print("origin     : " + str(result["target"]))
    print("frames     : " + str(result["frames"]) + "  " + str(result.get("sizes") or {}))
    if result.get("renamed"):
        print("renamed    : " + ", ".join(result["renamed"]))
    return 0


def cmd_runs(args: argparse.Namespace, settings: Settings) -> int:
    """List the runs in both halves of the work tree, newest first."""
    from src import stages

    rows = stages.scan_runs(Path(settings.output_dir))
    if args.as_json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print("resource 里还没有 run —— 先跑 stage1 画一张，或者 promote 一张原稿")
        return 0
    for row in rows:
        pips = " ".join(
            str(number) if row["stages"][str(number)]["ok"] else "-"
            for number in (1, 2, 3, 4)
        )
        print("  {}   {}   {}/{}".format(pips, row["run"], row["done"], len(stages.STAGES)))
        if row["clips"]:
            print("        clips: " + ", ".join(row["clips"]))
    print("")
    print("四个数字是四个阶段：1 原画生成 / 2 原画处理(第 2、3 步) / 3 视频生成 / 4 视频处理")
    print("resource/image 装 1-2，resource/video 装 3-5；- 表示这个阶段还没产物")
    return 0


def cmd_ui(args: argparse.Namespace, settings: Settings) -> int:
    """``python main.py ui`` - the same pipeline, with a page in front of it.

    The console drives the command line rather than the other way round, so every
    button here ends in a ``python main.py <stage> ...`` that the README already
    documents. ``--out`` and ``--env`` are honoured, which is how the page can be
    pointed at a different resource folder without editing anything.
    """
    from dataclasses import replace

    from .paths import load_paths
    from .ui.server import DEFAULT_HOST, DEFAULT_PORT, serve

    paths = replace(
        load_paths(),
        resource=Path(settings.output_dir).resolve(),
        harness=Path(settings.harness_dir).resolve(),
    )
    serve(
        paths=paths,
        host=args.host or DEFAULT_HOST,
        port=args.port or DEFAULT_PORT,
        open_browser=not args.no_browser,
    )
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    _configure_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(getattr(args, "verbose", 0) or 0)

    settings = _settings_from_args(args)

    try:
        if args.command == "list":
            return cmd_list(args, settings)
        if args.command == "show":
            return cmd_show(args, settings)
        if args.command == "gen":
            return cmd_generate(args, settings, all_specs=False)
        if args.command == "gen-all":
            return cmd_generate(args, settings, all_specs=True)
        if args.command == "anim":
            return cmd_anim(args, settings)
        if args.command == "probe":
            return cmd_probe(args, settings)
        if args.command == "common":
            return cmd_common(args, settings)
        if args.command == "cutout":
            return cmd_cutout(args, settings)
        if args.command == "stage1":
            return cmd_stage1(args, settings)
        if args.command == "stage2":
            return cmd_stage2(args, settings)
        if args.command == "stage3":
            return cmd_stage3(args, settings)
        if args.command == "stage4":
            return cmd_stage4(args, settings)
        if args.command == "stage5":
            return cmd_stage5(args, settings)
        if args.command == "select":
            return cmd_select(args, settings)
        if args.command == "run":
            return cmd_run(args, settings)
        if args.command == "promote":
            return cmd_promote(args, settings)
        if args.command == "normalize":
            return cmd_normalize(args, settings)
        if args.command == "runs":
            return cmd_runs(args, settings)
        if args.command == "ui":
            return cmd_ui(args, settings)
    except HarnessError as exc:
        print("harness error: " + str(exc), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130

    parser.print_help()
    return 2
