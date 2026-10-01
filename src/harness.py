"""Prompt harness: reads style presets and character specs from ``hareness/``.

A *style preset* (``hareness/style/*.yaml``) holds the shared look of an asset
family: a prefix, a quality suffix and the things to avoid.

A *spec* (``hareness/characters/*.yaml``) describes one asset: the prompt
template plus optional per-variation overrides. Templates are plain sentences
with ``{placeholder}`` slots, filled from the matching variation mapping.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import yaml

log = logging.getLogger(__name__)


class HarnessError(RuntimeError):
    """Raised when a harness file is missing or malformed."""


class _SafeDict(dict):
    """``format_map`` helper that leaves unknown placeholders untouched."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _render(template: str, context: Dict[str, Any]) -> str:
    try:
        return template.format_map(_SafeDict(context))
    except (ValueError, IndexError):
        log.warning("Could not fill every placeholder in: %s", template[:80])
        return template


def _unwrap(text: str) -> str:
    """Fold the soft line breaks of a YAML block scalar into single lines."""
    paragraphs = re.split(r"\n\s*\n", str(text))
    return "\n\n".join(
        " ".join(line.strip() for line in paragraph.splitlines() if line.strip())
        for paragraph in paragraphs
    ).strip()


def _tidy(text: str) -> str:
    lines = [line.rstrip() for line in str(text).splitlines()]
    result: List[str] = []
    for line in lines:
        if not line and result and not result[-1]:
            continue
        result.append(line)
    return "\n".join(result).strip()


def _read_yaml(path: Path) -> Dict[str, Any]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8-sig"))
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise HarnessError(str(path) + ": expected a YAML mapping at the top level.")
    return data


def _as_list(value: Any) -> List[Any]:
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _as_reference_list(value: Any) -> List[str]:
    """``reference: art.png`` / ``references: [a.png, b.png]`` -> list of paths."""
    items: List[Any] = []
    for item in _as_list(value):
        if isinstance(item, (list, tuple)):
            items.extend(item)
        elif isinstance(item, str) and ("\n" in item or ";" in item):
            items.extend(part for part in item.replace(";", "\n").splitlines())
        else:
            items.append(item)
    return [str(item).strip() for item in items if str(item).strip()]


def _normalize_variations(value: Any) -> List[Dict[str, Any]]:
    variations: List[Dict[str, Any]] = []
    for item in _as_list(value):
        if isinstance(item, dict):
            variations.append({str(k): v for k, v in item.items()})
        elif item not in (None, ""):
            variations.append({"variant": str(item)})
    return variations


STYLE_BASE_NAMES = ("_common.yaml", "_common.yml", "_base.yaml", "_base.yml")


def _join_blocks(*blocks: str) -> str:
    """Join prompt fragments with a blank line, skipping empty ones."""
    return "\n\n".join(block.strip() for block in blocks if block and block.strip())


def _merge_terms(base: str, extra: str, remove: Iterable[str] = ()) -> str:
    """Merge two comma lists without duplicates, dropping the removed terms."""
    dropped = {str(item).strip().lower() for item in remove}
    collected: List[str] = []
    seen: set = set(dropped)
    for source in (base, extra):
        for item in str(source or "").split(","):
            item = item.strip()
            if item and item.lower() not in seen:
                seen.add(item.lower())
                collected.append(item)
    return ", ".join(collected)


@dataclass
class StylePreset:
    """Shared look-and-feel for a family of assets."""

    id: str
    name: str = ""
    prefix: str = ""
    suffix: str = ""
    negative: str = ""
    aspect_ratio: Optional[str] = None
    image_size: Optional[str] = None
    path: Optional[Path] = None
    raw: Dict[str, Any] = field(default_factory=dict)
    inherit: bool = True
    negative_remove: List[str] = field(default_factory=list)
    base_path: Optional[Path] = None

    @classmethod
    def from_dict(
        cls, data: Dict[str, Any], path: Optional[Path] = None
    ) -> "StylePreset":
        return cls(
            id=str(data.get("id") or (path.stem if path else "")),
            name=str(data.get("name") or ""),
            prefix=str(data.get("prompt_prefix") or data.get("prefix") or "").strip(),
            suffix=str(data.get("quality_suffix") or data.get("suffix") or "").strip(),
            negative=str(data.get("negative") or "").strip(),
            aspect_ratio=data.get("aspect_ratio"),
            image_size=data.get("image_size"),
            path=path,
            raw=data,
            inherit=bool(data.get("inherit_common", True)),
            negative_remove=[str(item).strip() for item in _as_list(data.get("negative_remove"))],
        )

    def with_base(self, base: Optional["StylePreset"]) -> "StylePreset":
        """Fold the shared ``_common.yaml`` style layer in front of this preset."""
        if base is None or not self.inherit:
            return self
        return StylePreset(
            id=self.id,
            name=self.name,
            prefix=_join_blocks(base.prefix, self.prefix),
            suffix=_join_blocks(base.suffix, self.suffix),
            negative=_merge_terms(base.negative, self.negative, self.negative_remove),
            aspect_ratio=self.aspect_ratio or base.aspect_ratio,
            image_size=self.image_size or base.image_size,
            path=self.path,
            raw=self.raw,
            inherit=self.inherit,
            negative_remove=list(self.negative_remove),
            base_path=base.path,
        )


@dataclass
class PromptSpec:
    """One asset described in ``hareness/characters/``."""

    id: str
    name: str = ""
    category: str = "character"
    style: str = ""
    template: str = ""
    negative: str = ""
    aspect_ratio: Optional[str] = None
    image_size: Optional[str] = None
    model: Optional[str] = None
    api_style: Optional[str] = None
    count: Optional[int] = None
    variations: List[Dict[str, Any]] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    notes: str = ""
    # Reference art this asset is drawn *from*. Paths written here are resolved
    # against the project root; an empty list means "text to image".
    references: List[str] = field(default_factory=list)
    # "edit" -> the model redraws the reference art (the default)
    # "copy" -> the reference file *is* the asset, only post-processing runs
    reference_mode: str = ""
    path: Optional[Path] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def summary(self) -> str:
        return self.name or self.id


def load_style_base(style_dir: Path) -> Optional[StylePreset]:
    """Read the shared style layer (``style/_common.yaml``) if it exists."""
    for name in STYLE_BASE_NAMES:
        path = Path(style_dir) / name
        if path.is_file():
            preset = StylePreset.from_dict(_read_yaml(path), path)
            preset.inherit = False
            return preset
    return None


def load_styles(style_dir: Path) -> Dict[str, StylePreset]:
    """Load every ``*.yaml`` / ``*.yml`` in the style folder.

    Each preset is merged with the shared style base, so a new preset only has
    to describe what makes it different.
    """
    styles: Dict[str, StylePreset] = {}
    base = load_style_base(style_dir)
    for path in sorted(Path(style_dir).glob("*.y*ml")):
        if path.name.startswith("_"):
            continue
        preset = StylePreset.from_dict(_read_yaml(path), path).with_base(base)
        if preset.id:
            styles[preset.id] = preset
    return styles


def load_specs(characters_dir: Path) -> Dict[str, PromptSpec]:
    """Load every character/item/environment spec in the folder."""
    specs: Dict[str, PromptSpec] = {}
    for path in sorted(Path(characters_dir).glob("*.y*ml")):
        if path.name.startswith("_"):
            continue
        data = _read_yaml(path)
        spec = PromptSpec(
            id=str(data.get("id") or path.stem),
            name=str(data.get("name") or ""),
            category=str(data.get("category") or "character"),
            style=str(data.get("style") or ""),
            template=str(data.get("prompt") or data.get("template") or "").strip(),
            negative=str(data.get("negative") or "").strip(),
            aspect_ratio=data.get("aspect_ratio"),
            image_size=data.get("image_size"),
            model=data.get("model"),
            api_style=data.get("api_style"),
            count=data.get("count"),
            variations=_normalize_variations(data.get("variations")),
            tags=[str(tag) for tag in _as_list(data.get("tags"))],
            notes=str(data.get("notes") or "").strip(),
            references=_as_reference_list(
                data.get("references") or data.get("reference") or data.get("source_images")
            ),
            reference_mode=str(data.get("reference_mode") or data.get("source_mode") or "")
            .strip()
            .lower(),
            path=path,
            raw=data,
        )
        if not spec.template:
            raise HarnessError(str(path) + ": the 'prompt' field is empty.")
        specs[spec.id] = spec
    return specs


def resolve_style(
    spec: PromptSpec, styles: Dict[str, StylePreset], override: Optional[str] = None
) -> Optional[StylePreset]:
    """Find the preset named by the spec (or by ``override``)."""
    style_id = override or spec.style
    if not style_id:
        return None
    preset = styles.get(style_id)
    if preset is None:
        log.warning(
            "%s: unknown style '%s' (available: %s)",
            spec.id,
            style_id,
            ", ".join(sorted(styles)) or "none",
        )
    return preset


def plan_jobs(spec: PromptSpec, count: Optional[int] = None) -> List[Dict[str, Any]]:
    """Expand a spec into concrete variations to render.

    ``--count`` wins; otherwise the spec's ``count`` is used; otherwise the
    number of declared variations (at least one).
    """
    variations = list(spec.variations) or [{}]
    if count is not None and int(count) > 0:
        total = int(count)
    elif spec.count:
        total = int(spec.count)
    else:
        total = len(variations)

    jobs: List[Dict[str, Any]] = []
    for index in range(total):
        variation = dict(variations[index % len(variations)])
        variation["_index"] = index + 1
        variation["_total"] = total
        jobs.append(variation)
    return jobs


@dataclass
class CommonBlock:
    """Shared text that is sent with *every* request (``hareness/common.yaml``)."""

    enabled: bool = True
    version: Any = None
    preamble: str = ""
    requirements: str = ""
    negative: str = ""
    # Sent instead of nothing whenever reference art is attached: it tells the
    # model that the attached image - not this text - defines the look.
    reference_lock: str = ""
    reference_mode: str = "edit"
    send_as_system: bool = False
    defaults: Dict[str, Any] = field(default_factory=dict)
    pipeline: Dict[str, Any] = field(default_factory=dict)
    reuse: Dict[str, Any] = field(default_factory=dict)
    path: Optional[Path] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any], path: Optional[Path] = None) -> "CommonBlock":
        defaults = data.get("defaults")
        pipeline = data.get("pipeline")
        reuse = data.get("reuse")
        return cls(
            enabled=bool(data.get("enabled", True)),
            version=data.get("version"),
            preamble=str(data.get("preamble") or "").strip(),
            requirements=str(data.get("requirements") or "").strip(),
            negative=str(data.get("negative") or "").strip(),
            reference_lock=str(data.get("reference_lock") or "").strip(),
            reference_mode=str(
                data.get("reference_mode")
                or (pipeline.get("reference_mode") if isinstance(pipeline, dict) else "")
                or "edit"
            )
            .strip()
            .lower(),
            send_as_system=bool(data.get("send_as_system", False)),
            defaults={str(k): v for k, v in defaults.items()} if isinstance(defaults, dict) else {},
            pipeline={str(k): v for k, v in pipeline.items()} if isinstance(pipeline, dict) else {},
            reuse={str(k): v for k, v in reuse.items()} if isinstance(reuse, dict) else {},
            path=path,
        )

    @property
    def is_empty(self) -> bool:
        return not (self.preamble or self.requirements or self.negative)

    def default_for(self, key: str) -> Optional[str]:
        value = self.defaults.get(key)
        if value in (None, "", "none", "null"):
            return None
        return str(value)

    def pipeline_value(self, dotted: str, default: Any = None) -> Any:
        """Read a dotted key from the ``pipeline`` block, e.g. ``crop.trim``."""
        node: Any = self.pipeline
        for part in str(dotted).split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        if node in (None, "", "none", "null"):
            return default
        return node

    def pipeline_section(self, name: str) -> Dict[str, Any]:
        """Return one ``pipeline`` sub-block (``background`` / ``crop`` / ``scale``)."""
        value = self.pipeline.get(name)
        return dict(value) if isinstance(value, dict) else {}


def load_common(path: Path) -> CommonBlock:
    """Read ``hareness/common.yaml``; a missing file means "no common block"."""
    path = Path(path)
    if not path.is_file():
        log.info("No common block at %s - prompts will be sent without shared text.", path)
        return CommonBlock(enabled=False)
    return CommonBlock.from_dict(_read_yaml(path), path)


@dataclass
class PromptBundle:
    """Everything that goes into one API call for a single variation."""

    prompt: str = ""
    system: str = ""
    negative: str = ""

    def as_text(self) -> str:
        if self.system:
            return "[system]\n" + self.system + "\n\n[user prompt]\n" + self.prompt
        return self.prompt

def build_negative(
    spec: PromptSpec,
    style: Optional[StylePreset] = None,
    variation: Optional[Dict[str, Any]] = None,
    common: Optional[CommonBlock] = None,
) -> str:
    """Merge common / spec / style / variation negatives without duplicates."""
    collected: List[str] = []
    seen: set = set()
    sources = [
        common.negative if common and common.enabled else "",
        spec.negative,
        style.negative if style else "",
        (variation or {}).get("negative"),
    ]
    for source in sources:
        for item in str(source or "").split(","):
            item = item.strip()
            if item and item.lower() not in seen:
                seen.add(item.lower())
                collected.append(item)
    return ", ".join(collected)


def build_request(
    spec: PromptSpec,
    style: Optional[StylePreset] = None,
    common: Optional[CommonBlock] = None,
    variation: Optional[Dict[str, Any]] = None,
    reference_lock: str = "",
) -> PromptBundle:
    """Assemble the prompt (and optional system instruction) for one variation.

    ``reference_lock`` is passed by the caller when it is about to attach
    reference art; it lands right after the style layer so that it outranks the
    written style description and the written character description.
    """
    common = common or CommonBlock()
    use_common = bool(common.enabled) and not common.is_empty

    context: Dict[str, Any] = dict(variation or {})
    context.setdefault("name", spec.raw.get("title") or spec.name)
    context.setdefault("id", spec.id)
    context.setdefault("category", spec.category)
    context.setdefault("tags", ", ".join(spec.tags))

    system = ""
    pieces: List[str] = []
    if use_common and common.preamble:
        if common.send_as_system:
            system = common.preamble.strip()
        else:
            pieces.append(common.preamble.strip())
    if style and style.prefix:
        pieces.append(style.prefix)
    if reference_lock.strip():
        pieces.append(reference_lock.strip())
    pieces.append(_unwrap(_render(spec.template, context)))
    if style and style.suffix:
        pieces.append(style.suffix)
    if use_common and common.requirements:
        pieces.append(common.requirements.strip())

    prompt = "\n".join(piece for piece in pieces if piece)
    negative = build_negative(spec, style, variation, common if use_common else None)
    if negative:
        prompt += "\n\nAvoid (do not draw): " + negative
    return PromptBundle(prompt=_tidy(prompt), system=system, negative=negative)


def build_prompt(
    spec: PromptSpec,
    style: Optional[StylePreset] = None,
    variation: Optional[Dict[str, Any]] = None,
    common: Optional[CommonBlock] = None,
) -> str:
    """Assemble the final prompt text sent to the image model."""
    return build_request(spec, style, common, variation).prompt


def iter_specs(specs: Dict[str, PromptSpec], ids: Iterable[str]) -> List[PromptSpec]:
    """Resolve ids (case-insensitive) preserving the requested order."""
    lookup = {key.lower(): value for key, value in specs.items()}
    resolved: List[PromptSpec] = []
    for requested in ids:
        spec = lookup.get(str(requested).lower())
        if spec is None:
            raise HarnessError(
                "Unknown spec '" + str(requested) + "'. Available: " + ", ".join(sorted(specs))
            )
        resolved.append(spec)
    return resolved
