"""Where this project keeps its files - the single place that decides.

Modules used to work out their own paths, and that only held together as long as the
process happened to be started from the project root. This module resolves them once,
by walking up from this file until it finds the folder that holds ``hareness/``.

Every folder can also be overridden with an environment variable, which is what lets
the GUI point at a different resource folder without anybody editing code:

    ART_PROJECT_ROOT   the project folder itself
    ART_RESOURCE_DIR   generated runs              (default: <root>/resource)
    ART_HARNESS_DIR    specs, styles, common.yaml  (default: <root>/hareness)
    ART_ORIGIN_DIR     the master artwork          (default: <root>/origin)
    ART_SCRATCH_DIR    throwaway work              (default: <root>/temp)
    ART_ENV_FILE       the .env holding the API key (default: <root>/.env)

Nothing here imports the rest of ``src``: this is the bottom of the dependency chain,
and ``src.config`` is built on top of it.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Union

ENV_ROOT = ("ART_PROJECT_ROOT",)
ENV_RESOURCE = ("ART_RESOURCE_DIR", "OUTPUT_DIR")
ENV_HARNESS = ("ART_HARNESS_DIR", "HARNESS_DIR")
ENV_ORIGIN = ("ART_ORIGIN_DIR",)
ENV_SCRATCH = ("ART_SCRATCH_DIR",)
ENV_ENV_FILE = ("ART_ENV_FILE",)

RESOURCE_DIR_NAME = "resource"
HARNESS_DIR_NAME = "hareness"
ORIGIN_DIR_NAME = "origin"
ENV_FILE_NAME = ".env"

# Scratch space: probe output, one-off scripts, a keyed sample to look at, the log of a
# server started in the background. None of it is part of a run, all of it is disposable,
# and it is git-ignored - the point is that it has ONE place to go, so neither the
# project root nor a run folder collects leftovers. Nothing in the pipeline writes here:
# a run is reproducible from ``resource`` alone.
SCRATCH_DIR_NAME = "temp"

# Both trees are split in two, and both halves are named the same way, so "the stills"
# and "the clips" are the only two things anybody has to remember:
#
#   resource/image/<day>/<run>/   what the image model drew, and the keyed versions
#   resource/video/<day>/<run>/   the clip a video model made, and the frames cut from it
#   origin/image/<name>           the stills that were picked and kept
#   origin/video/<name>/          the frame sequences that were picked and kept
#
# ``resource`` is the work in progress - everything in it can be regenerated. ``origin``
# is curated by hand: it is the only folder whose contents are meant to be permanent,
# and it is what the video stages read from.
IMAGE_DIR_NAME = "image"
VIDEO_DIR_NAME = "video"

# The two roots the GUI also lists, and the archive from before the trees were split.
KEPT_KEY = "kept"
OLD_KEY = "old"
OLD_DIR_NAME = "old"

# A folder is the project root when it holds all of these.
PROJECT_MARKERS = ("hareness", "main.py")

# The image formats the project reads. The pipeline itself only ever writes PNG, but
# ``origin/image`` is filled by hand as often as by the promote button - somebody drops
# in a JPEG they were sent - so the reading side accepts what a person might put there.
# One list, because "which files count as artwork" has to mean the same thing in the
# promote button, in stage 3's picker and in the stills stage 3 animates.
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif")


def is_image(path: Union[str, Path]) -> bool:
    """Does this look like a still the project should read?"""
    return Path(path).suffix.lower() in IMAGE_SUFFIXES


def _looks_like_project(folder: Path) -> bool:
    try:
        return all((folder / marker).exists() for marker in PROJECT_MARKERS)
    except OSError:
        return False


def find_project_root(start: Optional[Union[str, Path]] = None) -> Path:
    """The folder that holds ``hareness/`` and ``main.py``.

    Walks up from ``start`` (this file by default) so that ``src`` can be imported and
    used whatever the current directory is. Falls back to the folder above ``src`` if
    nothing matches, which keeps it working if the tree is ever trimmed down.
    """
    here = Path(start).resolve() if start is not None else Path(__file__).resolve()
    candidates = [here] if here.is_dir() else []
    candidates.extend(here.parents)
    for candidate in candidates:
        if _looks_like_project(candidate):
            return candidate
    return Path(__file__).resolve().parents[1]


PROJECT_ROOT = find_project_root()


@dataclass(frozen=True)
class ProjectPaths:
    """The folders the project reads and writes."""

    root: Path
    harness: Path
    resource: Path
    origin: Path
    scratch: Path
    env_file: Path

    def describe(self) -> Dict[str, str]:
        """Where each folder really is, as an absolute path.

        For tooltips and log lines that have to name a real place on this machine; the
        page itself shows :meth:`describe_relative`.
        """
        return {
            "root": str(self.root),
            "harness": str(self.harness),
            "resource": str(self.resource),
            "origin": str(self.origin),
            "scratch": str(self.scratch),
            "env_file": str(self.env_file),
        }

    def describe_relative(self) -> Dict[str, str]:
        """The same folders written the way the page shows them, relative to the root.

        The console must not print a machine-specific path: the project is meant to be
        copied around and open sourced, so a screenshot of the top bar has to read the
        same on every machine. ``root`` is the folder's own name rather than ``.``,
        because that chip answers "which copy of the project am I looking at"; the
        absolute path is still there, one hover away. A folder that genuinely sits
        outside the root (an ``ART_RESOURCE_DIR`` pointing elsewhere) comes back
        absolute - that is the honest answer, not a bug.
        """
        return {
            "root": self.root.name,
            "harness": relative_to_root(self.harness, self.root),
            "resource": relative_to_root(self.resource, self.root),
            "origin": relative_to_root(self.origin, self.root),
            "scratch": relative_to_root(self.scratch, self.root),
            "env_file": relative_to_root(self.env_file, self.root),
        }

    def ensure_scratch(self) -> Path:
        """``temp`` - create it if it is not there yet, and hand it back.

        The folder is not part of the tree, so it is the one path here that may simply
        not exist; anything that wants to drop a scratch file asks for it through this
        rather than making its own.
        """
        self.scratch.mkdir(parents=True, exist_ok=True)
        return self.scratch

    @property
    def image_resource(self) -> Path:
        """``resource/image`` - the stills: what was drawn, and the keyed versions."""
        return self.resource / IMAGE_DIR_NAME

    @property
    def video_resource(self) -> Path:
        """``resource/video`` - the clips and the frame sequences cut from them."""
        return self.resource / VIDEO_DIR_NAME

    @property
    def image_origin(self) -> Path:
        """``origin/image`` - the stills that were kept, and the input for stage 3."""
        return self.origin / IMAGE_DIR_NAME

    @property
    def video_origin(self) -> Path:
        """``origin/video`` - the frame sequences that were kept."""
        return self.origin / VIDEO_DIR_NAME

    def work_root(self, kind: str) -> Path:
        """``"image"`` / ``"video"`` -> the half of ``resource`` that holds that tree."""
        if kind == IMAGE_DIR_NAME:
            return self.image_resource
        if kind == VIDEO_DIR_NAME:
            return self.video_resource
        raise ValueError("kind must be 'image' or 'video', not " + repr(kind))

    def kept_root(self, kind: str) -> Path:
        """``"image"`` / ``"video"`` -> the half of ``origin`` that holds that tree."""
        if kind == IMAGE_DIR_NAME:
            return self.image_origin
        if kind == VIDEO_DIR_NAME:
            return self.video_origin
        raise ValueError("kind must be 'image' or 'video', not " + repr(kind))

    def browse_roots(self) -> Dict[str, Path]:
        """The folders the GUI is allowed to list.

        Four names the front end can ask for: the two halves of ``resource`` where the
        pipeline writes, and the two halves of ``origin`` it reads from. Asking by name
        means the browser never has to send a path back, and the layout can move
        without the page knowing.
        """
        return {
            IMAGE_DIR_NAME: self.image_resource,
            VIDEO_DIR_NAME: self.video_resource,
            KEPT_KEY: self.origin,
            OLD_KEY: self.resource / OLD_DIR_NAME,
        }


def _pick(env: Mapping[str, str], names: Sequence[str], default: Path) -> Path:
    for name in names:
        value = env.get(name)
        if value is not None and str(value).strip() != "":
            return Path(str(value)).expanduser()
    return default


def load_paths(env: Optional[Mapping[str, str]] = None) -> ProjectPaths:
    """Resolve every project folder, letting the environment override each one."""
    source: Mapping[str, str] = os.environ if env is None else env
    root = _pick(source, ENV_ROOT, PROJECT_ROOT).resolve()
    return ProjectPaths(
        root=root,
        harness=_pick(source, ENV_HARNESS, root / HARNESS_DIR_NAME).resolve(),
        resource=_pick(source, ENV_RESOURCE, root / RESOURCE_DIR_NAME).resolve(),
        origin=_pick(source, ENV_ORIGIN, root / ORIGIN_DIR_NAME).resolve(),
        scratch=_pick(source, ENV_SCRATCH, root / SCRATCH_DIR_NAME).resolve(),
        env_file=_pick(source, ENV_ENV_FILE, root / ENV_FILE_NAME).resolve(),
    )


_ACTIVE_ROOT: Optional[Path] = None


def active_root() -> Path:
    """The project root actually in force, with the environment taken into account.

    ``PROJECT_ROOT`` is where this file lives; this is where the *project* is. They are
    the same thing until somebody sets ``ART_PROJECT_ROOT``, and it is this one that
    paths written into metadata files are made relative to.
    """
    global _ACTIVE_ROOT
    if _ACTIVE_ROOT is None:
        _ACTIVE_ROOT = load_paths().root
    return _ACTIVE_ROOT


def ensure_importable() -> Path:
    """Put the project root on ``sys.path`` so ``import src.x`` works from anywhere.

    The GUI can be started as ``python -m src.ui`` from any directory; this is what
    makes that work instead of only ``python main.py`` from the project root.
    """
    root = str(PROJECT_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    return PROJECT_ROOT


def resolve_inside(base: Union[str, Path], candidate: Union[str, Path]) -> Path:
    """Join ``candidate`` onto ``base`` and refuse anything that escapes it.

    The GUI is handed paths by the browser, so every one of them is checked here before
    it is read or written: a relative path is taken from ``base``, an absolute one is
    only accepted when it is already inside ``base``.
    """
    base_path = Path(base).resolve()
    raw = Path(str(candidate))
    target = (raw if raw.is_absolute() else base_path / raw).resolve()
    if target != base_path and base_path not in target.parents:
        raise ValueError("path is outside " + str(base_path) + ": " + str(candidate))
    return target


def relative_to_root(path: Union[str, Path], root: Optional[Path] = None) -> str:
    """``<root>/resource/image/...`` -> ``resource/image/...``, always with ``/``.

    The GUI never sends absolute paths back, and no metadata file records one: both are
    relative to the project root, which is what keeps a saved view, a log line or a
    sidecar meaningful after the project is copied somewhere else.
    """
    base = active_root() if root is None else Path(root)
    target = Path(path).resolve()
    try:
        return target.relative_to(base).as_posix()
    except ValueError:
        return str(target)


def _looks_like_a_path(text: str) -> bool:
    """Cheap guard before asking the filesystem about a string."""
    if not text or len(text) > 4096 or "\n" in text:
        return False
    return Path(text).is_absolute()


def relativize_paths(value: Any, root: Optional[Path] = None) -> Any:
    """Rewrite every absolute path inside a structure into a project-relative one.

    Reports and metadata are assembled from a dozen helpers and each of them records
    the files it touched, so "remember to store relative paths" is a rule that would be
    broken eventually - by a new field, or by a string some helper happened to build.
    Walking the finished value instead means nothing absolute reaches a sidecar or a
    manifest by accident, and the whole project stays movable: clone it anywhere, on
    any platform, and the paths inside it still point at the right files.
    """
    base = Path(root) if root else active_root()
    if isinstance(value, dict):
        return {key: relativize_paths(item, base) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [relativize_paths(item, base) for item in value]
    if isinstance(value, str) and _looks_like_a_path(value):
        try:
            return Path(value).resolve().relative_to(base).as_posix()
        except (ValueError, OSError):
            return value
    return value


def describe_env() -> Dict[str, Any]:
    """The overrides that are actually in force, for the GUI's settings panel."""
    found: Dict[str, Any] = {}
    for names in (ENV_ROOT, ENV_RESOURCE, ENV_HARNESS, ENV_ORIGIN, ENV_ENV_FILE):
        for name in names:
            if os.environ.get(name):
                found[name] = os.environ[name]
    return found
