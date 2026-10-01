"""Prompt presets - the extra instructions a person keeps, read from local files.

Stage 3 prepares the first frame for the video model and stage 4 writes one prompt per
clip. Between the two sits this module: a folder of small files (``hareness/prompts`` by
default) where ``common`` is always applied and the others are picked per run. That is
what makes "the same house rules plus this clip's twist" a choice on the page instead of
an edit to the source code - the built-in prompt in :mod:`src.video` still carries the
load-bearing parts (fixed camera, flat backdrop, loop line), and everything here lands
*after* it as extra instruction.

A file's name is the preset's name, so ``walk_side.xml`` is picked as ``walk_side``.
Several formats are read, because the file a person already has is the right file:

* ``.txt`` / ``.md`` - used exactly as written;
* ``.xml`` - the text of the elements, in document order, so ``<prompt><rule>...</rule>
  </prompt>`` and a bare document both work; a ``title`` attribute names it;
* ``.yaml`` / ``.yml`` - the ``text:`` key when there is one, the raw text otherwise;
* ``.json`` - a string, or an object with a ``text`` key.

Files starting with ``_`` or ``.`` are ignored, and so is ``README`` - the folder can
document itself without becoming a preset.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

log = logging.getLogger(__name__)

PRESET_DIR_NAME = "prompts"
COMMON_NAME = "common"
SUFFIXES = (".txt", ".md", ".xml", ".yaml", ".yml", ".json")
IGNORED_STEMS = ("readme", "index")


@dataclass(frozen=True)
class Preset:
    """One file in the preset folder, read."""

    name: str
    path: Path
    text: str
    title: str = ""

    @property
    def label(self) -> str:
        return self.title or self.name

    @property
    def is_common(self) -> bool:
        return self.name.lower() == COMMON_NAME

    def describe(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "title": self.label,
            "text": self.text,
            "chars": len(self.text),
            "common": self.is_common,
        }


def preset_dir(harness: Path, name: str = PRESET_DIR_NAME) -> Path:
    return Path(harness) / name


def preset_dir_of(folder: Path) -> Path:
    """Whatever was passed in -> the folder the presets are actually in.

    Two callers name this folder two different ways: the CLI has the harness, the server
    has the folder itself. Taking either one is what stops a caller from handing over
    ``hareness`` and getting "no prompt preset called 'walk_side' in .../hareness" back,
    which is a confusing way to say "look one level deeper".
    """
    base = Path(folder)
    if base.name.lower() == PRESET_DIR_NAME:
        return base
    nested = base / PRESET_DIR_NAME
    if nested.is_dir():
        return nested
    # Nothing there yet. Name the folder the caller probably meant, so the error message
    # points at ``.../prompts`` rather than at the harness around it.
    return nested


def _squash(text: str) -> str:
    """An element's text as one line - XML is indented, the prompt is not."""
    return " ".join(str(text).split())


def _xml_text(path: Path) -> str:
    from xml.etree import ElementTree

    root = ElementTree.parse(str(path)).getroot()
    title = (root.get("title") or "").strip()
    chunks: List[str] = []
    for element in root.iter():
        if element.text and element.text.strip():
            chunks.append(_squash(element.text))
        if element.tail and element.tail.strip():
            chunks.append(_squash(element.tail))
    return ("\n".join(chunks), title)


def _yaml_text(path: Path) -> str:
    try:
        import yaml

        with open(path, encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except Exception:
        return ""
    if isinstance(data, dict):
        for key in ("text", "prompt", "body"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""
    if isinstance(data, str):
        return data.strip()
    return ""


def read_preset(path: Path) -> Optional[Preset]:
    """Read one file into a preset, or ``None`` when there is nothing usable in it."""
    path = Path(path)
    suffix = path.suffix.lower()
    title = ""
    try:
        if suffix == ".xml":
            text, title = _xml_text(path)
        elif suffix in (".yaml", ".yml"):
            text = _yaml_text(path)
            if not text:
                text = path.read_text(encoding="utf-8", errors="replace").strip()
        elif suffix == ".json":
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
            if isinstance(data, str):
                text = data.strip()
            elif isinstance(data, dict):
                text = str(data.get("text") or data.get("prompt") or "").strip()
                title = str(data.get("title") or "").strip()
            else:
                text = ""
        else:
            text = path.read_text(encoding="utf-8", errors="replace").strip()
    except Exception as exc:  # a hand-edited file will eventually be malformed
        log.warning("%s: could not read preset (%s)", path.name, exc)
        return None
    return Preset(name=path.stem, path=path, text=text, title=title)


def list_presets(folder: Path) -> List[Preset]:
    """Every preset in ``folder``, ``common`` first and the rest alphabetical."""
    folder = Path(folder)
    if not folder.is_dir():
        return []
    found: List[Preset] = []
    for path in sorted(folder.iterdir(), key=lambda p: p.name.lower()):
        if not path.is_file() or path.suffix.lower() not in SUFFIXES:
            continue
        if path.name.startswith(("_", ".")) or path.stem.lower() in IGNORED_STEMS:
            continue
        preset = read_preset(path)
        if preset is not None and preset.text:
            found.append(preset)
    found.sort(key=lambda preset: (not preset.is_common, preset.name.lower()))
    return found


def resolve(folder: Path, names: Optional[Iterable[str]]) -> List[Preset]:
    """The presets called ``names``, ``common`` first, in the order they were asked for.

    ``common`` is added whether or not it was asked for: it is the house style, the one
    file that has to hold for every clip, and a page that let it be unticked would be
    offering a way to quietly drop the style lock.
    """
    available = list_presets(folder)
    by_name = {preset.name.lower(): preset for preset in available}
    wanted = [str(name).strip() for name in (names or []) if str(name).strip()]
    picked: List[Preset] = [preset for preset in available if preset.is_common]
    for name in wanted:
        preset = by_name.get(name.lower())
        if preset is None:
            known = ", ".join(sorted(preset2.name for preset2 in available))
            raise ValueError(
                "no prompt preset called '" + name + "' in " + str(folder)
                + ("; there is: " + known if known else "; the folder is empty")
            )
        if preset not in picked:
            picked.append(preset)
    return picked


def combine(presets: Sequence[Preset], extra: str = "") -> str:
    """Every block joined, the hand-typed one last, empty when there is nothing."""
    blocks = [preset.text.strip() for preset in presets if preset.text.strip()]
    if str(extra or "").strip():
        blocks.append(str(extra).strip())
    return "\n\n".join(blocks)


def available_names(folder: Path) -> List[str]:
    return [preset.name for preset in list_presets(folder)]

# --------------------------------------------------------------------------------------
# What a run was told
# --------------------------------------------------------------------------------------

# The file stage 3 leaves in ``01_video_input/`` so stage 4 writes the same prompt. The
# presets are chosen once, on the video-input step, and the video step picks them up
# again from here - which is what makes "the same house rules" survive a re-run of one
# stage without anybody typing them out a second time.
SELECTION_NAME = "prompts.json"


def write_selection(path: Path, presets: Sequence[Preset], extra: str = "") -> Path:
    """Record which presets a run was set up with, next to the video input."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "presets": [preset.name for preset in presets],
        "titles": [preset.label for preset in presets],
        "extra": str(extra or "").strip(),
        "text": combine(presets, extra),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def read_selection(path: Path) -> Dict[str, Any]:
    """The recorded selection, or an empty one when this run never had any."""
    empty: Dict[str, Any] = {"presets": [], "titles": [], "extra": "", "text": ""}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty
    if not isinstance(data, dict):
        return empty
    payload = dict(empty)
    payload.update({key: data.get(key) or payload[key] for key in payload})
    return payload

