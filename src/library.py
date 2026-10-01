"""Moving things out of the work tree and into the kept tree - and naming them.

``resource`` is where the pipeline draws: everything in it is disposable and can be
regenerated from the prompt harness. ``origin`` is the opposite - it holds the handful
of files somebody actually looked at and decided were good, and it is what the video
stages animate.

This module is two things at once, and they are the same thing:

* **promoting** - one still, or one cut of frames, copied into ``origin``;
* **normalising** - and on the way in, put into the canonical shape :mod:`src.naming`
  describes, so that what lands in ``origin`` is already what a game project imports
  and what a person can read off a file name.

Doing both at once is what makes the folder survive somebody copying things into it by
hand. Reading is tolerant (see :func:`list_origin` and :func:`naming.cut_frames`): a
folder that arrived from somewhere else, with any frame names at all, is listed and
played as it is. Writing is strict: the next time it goes through
:func:`normalize` - which is what promoting does - it comes out as
``<entity>_<clip>_001.png`` with the 512 / 256 copies and a sheet beside it.

Nothing is ever overwritten without being asked twice.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from src import naming
from src.paths import (
    IMAGE_DIR_NAME,
    IMAGE_SUFFIXES,
    VIDEO_DIR_NAME,
    ProjectPaths,
    is_image,
    relative_to_root,
    relativize_paths,
)

# The delivery copies a cut writes next to its frames, and rebuilds on the way into
# ``origin``: a game project wants the 512 copy, and a cut that arrived from somewhere
# else will not have one.
COPY_DIRS = ("512", "256")

# Straps that belong with a cut but are not frames. Named after the cut now, so a
# folder of a hundred and nineteen frames is one glance away from being fully
# self-describing; :func:`naming.is_sheet` is what keeps them out of the frame count.
SHEET_SUFFIX = ".png"
CONTACT_SUFFIX = "_contact.png"
META_NAME = "meta.json"

# ``.__naming`` - where frames are parked for the moment it takes to renumber a folder
# in place. A leading dot keeps it out of every folder scan in the project.
STAGING_DIR = ".__naming"

# The raw frames a clip was extracted into, before keying. Large, and reproducible from
# the mp4 by re-running stage 5, so it stays behind.
SKIP_DIRS = ("source_frames",)

SIDECAR_SUFFIX = ".json"
BAD_NAME_CHARS = '<>:"|?*'


def _frame_files(folder: Path) -> List[Path]:
    """The frames in a cut folder, in playing order - whatever they are called.

    Order is read out of the names, not out of a manifest, because a folder somebody
    copied into ``origin`` has no manifest. A name a sheet wears (``sprite_sheet``,
    ``contact_sheet``, ``knight_walk_contact``) is the one thing that disqualifies a
    PNG from being a frame.
    """
    return naming.cut_frames(folder)


class PromoteConflict(Exception):
    """The target name is taken. The page asks once, then calls again with ``overwrite``."""

    def __init__(self, target: Path, paths: ProjectPaths) -> None:
        super().__init__("已经有 " + relative_to_root(target, paths.root) + " 了")
        self.target = target
        self.paths = paths


def valid_name(name: Any) -> str:
    """One name, usable as a file or folder name inside ``origin``.

    A promoted name is something a person types and then has to live with in Unity, so
    it is kept to a single path segment: no slashes (that would write outside the
    folder it was meant for), no characters Windows refuses, no leading dot.
    """
    text = str(name or "").strip().replace("\\", "/").strip("/")
    if not text:
        raise ValueError("请给一个名字")
    if "/" in text or text in (".", ".."):
        raise ValueError("名字里不能有斜杠：" + str(name))
    if any(char in text for char in BAD_NAME_CHARS):
        raise ValueError("名字里有不能用在文件名里的字符：" + str(name))
    if text.startswith("."):
        raise ValueError("名字不能以点开头：" + str(name))
    return text


def kind_of(source: Union[str, Path]) -> str:
    """``"video"`` for a cut of frames, ``"image"`` for a single still.

    Decided by what is on disk rather than by asking, so the page can offer one button
    and the person does not have to know which tree the thing belongs in.
    """
    return VIDEO_DIR_NAME if Path(source).is_dir() else IMAGE_DIR_NAME


def _stamp() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _image_size(path: Path) -> Optional[List[int]]:
    try:
        from PIL import Image

        with Image.open(path) as image:
            return [int(image.width), int(image.height)]
    except Exception:
        return None


def _sidecar_for(image: Path, paths: ProjectPaths) -> Dict[str, Any]:
    """The prompt sidecar the pipeline wrote for this still, if it is still around.

    The artwork sidecar sits in ``01_artwork/`` while the 512px copy that is usually the
    one promoted sits in ``02_artwork_ready/512/``, so the run is found by walking up
    from the file and looking beside every level - not by counting levels, which only
    ever works for one of the two paths. Keeping the prompt with the promoted file is
    the difference between a folder of PNGs and a folder that can be re-drawn.
    """
    candidates = [image.with_suffix(SIDECAR_SUFFIX)]
    node = image
    for _ in range(5):
        node = node.parent
        if node == node.parent:
            break
        candidates.append(node / (image.stem + SIDECAR_SUFFIX))
        for stage in ("01_artwork", "02_artwork_ready"):
            candidates.append(node / stage / (image.stem + SIDECAR_SUFFIX))
    for candidate in candidates:
        if candidate.is_file():
            try:
                return json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
    return {}


def _guess_identity(source: Path, clips: Sequence[str]) -> Tuple[str, Optional[str]]:
    """Read ``<entity>`` / ``<clip>`` off the path, for callers that did not say.

    Three shapes are worth reading, because they are the shapes a cut arrives in:

    * ``resource/.../03_frames/<clip>/<model>/`` - cut out of a run. The clip is right
      there; the character is the run folder two levels up
      (``20260923-172725_monster_imp``), read as an entity with the stamp taken off.
    * ``.../03_frames/<clip>/<model>/kept/`` - the frames somebody picked out of that
      cut. Same character and same animation: ``kept`` is where they are kept, not what
      they are, so it is stepped over instead of being read as a name. Without this,
      promoting a picked set would land in ``origin`` as an animation called "kept".
    * ``origin/video/<entity>_<clip>/`` - already named: the tail of the folder name is
      the clip and the head is the character.

    Anything else keeps its whole name as the entity, which is what leaves a name the
    project has never seen (``knight_attack``, with no ``attack`` in the clip list)
    intact instead of silently cutting it in half.
    """
    node = source
    if node.name == naming.SELECT_DIR:
        node = node.parent
    clip_name = node.parent.name
    stage = node.parent.parent.name
    if stage in ("03_frames", "02_video"):
        run = naming.without_stamp(node.parent.parent.parent.name)
        return naming.entity_of_artwork(run), clip_name
    return naming.split_name(source.name, clips)


def cut_identity(
    paths: ProjectPaths,
    source: Path,
    name: Optional[str] = None,
    entity: Optional[str] = None,
    clip: Optional[str] = None,
) -> Tuple[str, Optional[str], str]:
    """``(entity, clip, stem)`` for one cut, from whatever the caller knows.

    Three callers with three amounts of knowledge: the page knows both halves (it is
    looking at ``03_frames/walk/<model>/``), the command line may know only a name, and
    a folder somebody copied in knows nothing at all. So the halves are taken in that
    order of confidence, and a clip is never invented - a name the project cannot
    recognise keeps its whole self as the entity.
    """
    clips = naming.known_clips(paths)
    if entity:
        entity = valid_name(entity)
        clip = valid_name(clip) if clip else None
    else:
        stem = valid_name(name) if name else ""
        if not stem:
            entity, clip = _guess_identity(Path(source), clips)
            stem = naming.cut_name(entity, clip)
        else:
            entity, clip = naming.split_name(stem, clips)
    stem = naming.cut_name(entity, clip)
    return entity, clip, stem


def _write_frames(
    frames: Sequence[Path], target: Path, stem: str, *, move: bool = False
) -> List[Path]:
    """Put ``frames`` into ``target`` as ``<stem>_001.png`` ...

    ``move`` is for renumbering a folder in place, where the source and the target are
    the same folder and a name can collide with a file that has not been read yet
    (``a_003, a_001, a_002`` would eat one). So the frames are parked in a staging
    folder first - a leading dot keeps it out of every folder scan - and moved onto
    their final names once nothing is left in the way.
    """
    target.mkdir(parents=True, exist_ok=True)
    staging = target / STAGING_DIR
    staged: List[Tuple[Path, Path]] = []
    written: List[Path] = []
    for index, frame in enumerate(frames, 1):
        final = target / naming.frame_name(stem, index, frame.suffix.lower())
        if move:
            staging.mkdir(exist_ok=True)
            parking = staging / ("%04d%s" % (index, frame.suffix.lower()))
            shutil.move(str(frame), str(parking))
            staged.append((parking, final))
        else:
            shutil.copyfile(frame, final)
            written.append(final)
    for parking, final in staged:
        if parking != final:
            parking.replace(final)
        written.append(final)
    shutil.rmtree(staging, ignore_errors=True)
    # A cut that got shorter leaves the tail of the old one behind, named as if the
    # clip were still that long. Frames are renumbered from one on every pass, so
    # anything past the end is stale by definition.
    keep = {path.name for path in written}
    for stale in naming.cut_frames(target):
        if stale.name not in keep:
            stale.unlink()
    return written


def _write_sizes(frames: Sequence[Path], target: Path, sizes: Sequence[Any]) -> Dict[str, int]:
    """The 512 / 256 delivery copies, rebuilt from the frames that are there now.

    Rebuilt rather than copied from the source even when the source has its own copies:
    the copies have to carry the frame numbers the frames carry, and a cut that arrived
    from somewhere else has none to copy.
    """
    from src.postprocess import scale_frames

    counts: Dict[str, int] = {}
    for side in sizes:
        name = str(side)
        if not name.isdigit():
            continue
        folder = target / name
        shutil.rmtree(folder, ignore_errors=True)
        produced = scale_frames(list(frames), int(name))
        if produced:
            counts[name] = len(produced)
    return counts


def _write_sheets(frames: Sequence[Path], target: Path, stem: str) -> Tuple[Optional[Path], Optional[Path]]:
    """Build the two sheets beside the frames, and drop any others.

    A cut copied in from elsewhere often arrives with its own sheet (``atlas_sheet.png``).
    That one is neither a frame nor one of ours, so it is removed: a folder with two
    sprite sheets in it is a folder where nobody knows which one Unity imported.
    """
    from src import video
    from src.postprocess import make_strip_sheet

    sheet = make_strip_sheet(list(frames), target / (stem + SHEET_SUFFIX))
    contact = video.contact_sheet(list(frames), target / (stem + CONTACT_SUFFIX))
    fresh = {path.name for path in (sheet, contact) if path}
    for stale in naming.sheet_files(target):
        if stale.name not in fresh:
            stale.unlink()
    return sheet, contact


def _read_meta(folder: Path) -> Dict[str, Any]:
    try:
        return json.loads((folder / META_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def normalize(
    paths: ProjectPaths,
    source: Union[str, Path],
    *,
    name: Optional[str] = None,
    entity: Optional[str] = None,
    clip: Optional[str] = None,
    sizes: Sequence[Any] = COPY_DIRS,
    overwrite: bool = False,
) -> Dict[str, Any]:
    """Put one cut that is already inside ``origin`` into the canonical shape.

    This is the answer to "I copied a folder of frames in from somewhere else": the
    frames are renumbered ``<entity>_<clip>_001.png``, the 512 / 256 copies and the two
    sheets are built, and ``meta.json`` is written - all in place, because there is
    nowhere else to put it.

    Reading never required any of this (a folder with any frame names at all is listed
    and played as it is); this is only about making it *tidy*, and it is deliberately
    the same code promoting runs, so a cut that arrived by hand and a cut that came out
    of the pipeline end up indistinguishable.
    """
    source = Path(source)
    if not source.is_dir():
        raise ValueError("找不到这个文件夹：" + str(source))
    if source != paths.video_origin and paths.video_origin not in source.parents:
        raise ValueError("只整理 origin/video 下面的帧目录：" + str(source))

    entity, clip, stem = cut_identity(paths, source, name, entity=entity, clip=clip)
    target = paths.video_origin / stem
    if target != source:
        if target.exists() and not overwrite:
            raise PromoteConflict(target, paths)
        if target.exists():
            shutil.rmtree(target)
        source.rename(target)

    frames = _write_frames(naming.cut_frames(target), target, stem, move=True)
    if not frames:
        raise ValueError("这个文件夹里没有帧：" + relative_to_root(target, paths.root))
    counts = _write_sizes(frames, target, sizes)
    sheet, contact = _write_sheets(frames, target, stem)

    payload = relativize_paths(_read_meta(target), paths.root)
    payload.update({
        "kind": VIDEO_DIR_NAME,
        "name": stem,
        "entity": entity,
        "clip": clip,
        "frames": len(frames),
        "sizes": counts,
        "sheet": relative_to_root(sheet, paths.root) if sheet else None,
        "contact": relative_to_root(contact, paths.root) if contact else None,
    })
    payload.setdefault("promoted", _stamp())
    (target / META_NAME).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {
        "kind": VIDEO_DIR_NAME,
        "name": stem,
        "entity": entity,
        "clip": clip,
        "target": relative_to_root(target, paths.root),
        "from": payload.get("promoted_from"),
        "frames": len(frames),
        "sizes": counts,
        "renamed": target != source,
    }


def promote_image(
    paths: ProjectPaths,
    source: Union[str, Path],
    name: Optional[str] = None,
    *,
    overwrite: bool = False,
) -> Dict[str, Any]:
    """Keep one still: ``origin/image/<entity>.png``, plus its prompt sidecar.

    The name is the entity and nothing else. An artwork drawn inside a run is
    ``<spec>_<batch index>`` - ``monster_imp_01`` is the first of the pictures one
    request asked for - and the index is a generation detail like the model name, so it
    stays behind: a character has one still in ``origin``, and stage 3 reads it by name.

    The prompt sidecar travels with the picture, because that is the difference between
    a folder of PNGs and a folder that can be redrawn - and it is what the page shows
    back as "当时的提示词".
    """
    source = Path(source)
    if not source.is_file():
        raise ValueError("找不到这张图：" + str(source))
    if not is_image(source):
        raise ValueError("这不是一张图：" + str(source))

    stem = valid_name(name or naming.entity_of_artwork(source.stem))
    target = paths.image_origin / (stem + source.suffix.lower())
    if target.exists() and not overwrite:
        raise PromoteConflict(target, paths)

    paths.image_origin.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)

    meta = relativize_paths(_sidecar_for(source, paths), paths.root)
    # The sidecar's ``name`` is the character's display name ("小妖（低级怪物…）"); the
    # promoted file's ``name`` is the stem it was kept under. Both are worth having, so
    # the display name moves to ``character`` rather than being overwritten.
    character = meta.get("character") or meta.get("name")
    payload = dict(meta)
    payload.update({
        "kind": IMAGE_DIR_NAME,
        "name": stem,
        "character": character,
        "source": relative_to_root(source, paths.root),
        "promoted": _stamp(),
        "promoted_from": relative_to_root(source, paths.root),
        "size": _image_size(target),
    })
    (paths.image_origin / (stem + SIDECAR_SUFFIX)).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {
        "kind": IMAGE_DIR_NAME,
        "name": stem,
        "target": relative_to_root(target, paths.root),
        "from": payload["promoted_from"],
        "size": payload["size"],
    }


def promote_frames(
    paths: ProjectPaths,
    source: Union[str, Path],
    name: Optional[str] = None,
    *,
    entity: Optional[str] = None,
    clip: Optional[str] = None,
    overwrite: bool = False,
    sizes: Sequence[Any] = COPY_DIRS,
) -> Dict[str, Any]:
    """One cut of frames -> ``origin/video/<entity>_<clip>/``, in the canonical shape.

    The whole cut travels together and arrives already normalised - the frames renamed
    in playing order, the 512 / 256 copies, both sheets, the stats and where it came
    from - so what lands in ``origin`` is exactly what a Unity project would import,
    with nothing left to reconstruct and nothing left to guess.
    """
    source = Path(source)
    if not source.is_dir():
        raise ValueError("找不到这个文件夹：" + str(source))
    frames = _frame_files(source)
    if not frames:
        raise ValueError("这个文件夹里没有帧：" + relative_to_root(source, paths.root))

    entity, clip, stem = cut_identity(paths, source, name, entity=entity, clip=clip)
    target = paths.video_origin / stem
    if target.exists() and not overwrite:
        raise PromoteConflict(target, paths)
    if target.exists():
        # Copying over a cut of a different length would leave the old frames behind,
        # mixed in with the new ones and numbered as if the clip were longer than it is.
        shutil.rmtree(target)

    _write_frames(frames, target, stem)
    counts = _write_sizes(naming.cut_frames(target), target, sizes)
    sheet, contact = _write_sheets(naming.cut_frames(target), target, stem)

    payload = relativize_paths(_read_meta(source), paths.root)
    payload.update({
        "kind": VIDEO_DIR_NAME,
        "name": stem,
        "entity": entity,
        "clip": clip,
        "promoted": _stamp(),
        "promoted_from": relative_to_root(source, paths.root),
        "frames": len(frames),
        "sizes": counts,
        "sheet": relative_to_root(sheet, paths.root) if sheet else None,
        "contact": relative_to_root(contact, paths.root) if contact else None,
    })
    (target / META_NAME).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {
        "kind": VIDEO_DIR_NAME,
        "name": stem,
        "entity": entity,
        "clip": clip,
        "target": relative_to_root(target, paths.root),
        "from": payload["promoted_from"],
        "frames": len(frames),
        "sizes": counts,
    }


def promote(
    paths: ProjectPaths,
    source: Union[str, Path],
    name: Optional[str] = None,
    *,
    entity: Optional[str] = None,
    clip: Optional[str] = None,
    overwrite: bool = False,
) -> Dict[str, Any]:
    """Promote whatever this is: a still, or a cut of frames."""
    if kind_of(source) == VIDEO_DIR_NAME:
        return promote_frames(
            paths, source, name, entity=entity, clip=clip, overwrite=overwrite
        )
    return promote_image(paths, source, name or entity, overwrite=overwrite)
# -----------------------------------------------------------------------------------
# What is in origin
# -----------------------------------------------------------------------------------


def _entry(path: Path, paths: ProjectPaths) -> Dict[str, Any]:
    stat = path.stat()
    return {
        "name": path.name,
        "path": relative_to_root(path, paths.root),
        "kind": IMAGE_DIR_NAME,
        "size": stat.st_size,
        "mtime": stat.st_mtime,
    }


def sheet_of(folder: Path) -> Tuple[Optional[Path], Optional[Path]]:
    """``(sprite sheet, contact sheet)`` for a cut, whatever they are called.

    This project writes ``<name>.png`` and ``<name>_contact.png`` now; it wrote
    ``sprite_sheet.png`` and ``contact_sheet.png`` before that; and a cut copied in from
    somewhere else may call them anything at all, as long as the word is in the name.
    All three are looked for, in that order, so a cut never loses its straps on the way
    through :func:`list_origin` or the cut viewer - and never counts one as a frame.
    """
    folder = Path(folder)
    if not folder.is_dir():
        return None, None
    pngs = sorted(
        (entry for entry in folder.iterdir()
         if entry.is_file() and entry.suffix.lower() in IMAGE_SUFFIXES),
        key=lambda entry: naming.natural_key(entry.name),
    )

    def named(*names: str) -> Optional[Path]:
        for name in names:
            for path in pngs:
                if path.name.lower() == name.lower():
                    return path
        return None

    def like(word: str, but_not: str = "") -> Optional[Path]:
        for path in pngs:
            text = path.name.lower()
            if word in text and (not but_not or but_not not in text):
                return path
        return None

    sheet = named(folder.name + SHEET_SUFFIX, "sprite_sheet.png") or like("sheet", "contact")
    contact = named(folder.name + CONTACT_SUFFIX, "contact_sheet.png") or like("contact")
    return sheet, contact


def is_normalized(folder: Path, meta: Dict[str, Any]) -> bool:
    """Is this cut already in the shape :mod:`src.naming` describes?

    This is what lets the page offer 「整理」 only where it would do something, and only a
    cut that arrived from somewhere else ever answers ``False``: the frames have to be
    ``<folder>_001.png`` and up, the delivery copies have to match them name for name,
    and both sheets have to be there. Nothing about it is required for the folder to be
    listed, played or imported - a copy that is not tidy is still a working animation.
    """
    folder = Path(folder)
    if str(meta.get("name") or "") != folder.name:
        return False
    frames = _frame_files(folder)
    if not frames:
        return False
    expected = [naming.frame_name(folder.name, index) for index in range(1, len(frames) + 1)]
    if [path.name for path in frames] != expected:
        return False
    for side in COPY_DIRS:
        copies = folder / side
        if copies.is_dir() and sorted(path.name for path in copies.glob("*.png")) != expected:
            return False
    sheet, contact = sheet_of(folder)
    return bool(sheet and contact)


def _recency(meta: Dict[str, Any], path: Path) -> str:
    """When a kept thing landed in ``origin``, as a string that sorts newest first.

    Three answers, in the order they can be trusted: the stamp written when it was
    promoted; the run stamp inside ``promoted_from``; and - for a folder copied in by
    hand, which has neither - the folder's own mtime, the only thing it knows about its
    own age. ISO 8601 in local time sorts and prints correctly, which is all
    :func:`list_origin` asks of it.
    """
    stamp = str(meta.get("promoted") or "").strip()
    if stamp:
        return stamp
    run = naming.stamp_of(meta.get("promoted_from"))
    if run:
        return "%s-%s-%sT%s:%s:%s" % (
            run[0:4], run[4:6], run[6:8], run[9:11], run[11:13], run[13:15]
        )
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")
    except OSError:
        return ""


def list_origin(paths: ProjectPaths) -> Dict[str, Any]:
    """Everything that has been kept, so the page can show it as one list.

    Stills and cuts are both reported with the same fields plus a ``kind``, because the
    page shows them in one column: "what have I kept" is one question, not two.

    The order is **newest first** (:func:`_recency`), because what was kept a minute ago
    is what is being worked on: keeping something puts it at the top of the list instead
    of making a person hunt through a folder that only grows.

    None of this needs a ``meta.json`` or a canonical name. A folder copied in from
    another machine is counted, played, covered and ranked exactly like one the pipeline
    wrote, and reports itself through ``normalized`` so the page can offer to tidy it up.
    """
    clips = naming.known_clips(paths)

    images: List[Dict[str, Any]] = []
    if paths.image_origin.is_dir():
        for path in sorted(paths.image_origin.iterdir()):
            if not path.is_file() or path.suffix.lower() not in IMAGE_SUFFIXES:
                continue
            entry = _entry(path, paths)
            entry["meta"] = _read_json(path.with_suffix(SIDECAR_SUFFIX))
            entry["entity"] = path.stem
            entry["recency"] = _recency(entry["meta"] or {}, path)
            images.append(entry)
        images.sort(key=lambda item: item["recency"], reverse=True)

    cuts: List[Dict[str, Any]] = []
    if paths.video_origin.is_dir():
        for folder in sorted(entry for entry in paths.video_origin.iterdir() if entry.is_dir()):
            if folder.name.startswith("."):
                continue
            meta = _read_meta(folder)
            frames = _frame_files(folder)
            if not frames:
                continue
            sheet, contact = sheet_of(folder)
            entity, clip = naming.split_name(folder.name, clips)
            cuts.append({
                "name": folder.name,
                "kind": VIDEO_DIR_NAME,
                "path": relative_to_root(folder, paths.root),
                "frames": len(frames),
                "sizes": {key: len(list((folder / key).glob("*.png")))
                          for key in COPY_DIRS if (folder / key).is_dir()},
                # The halves are read off the name rather than off meta.json: a folder
                # somebody renamed by hand has a stale meta, and the name is what a
                # person reads in Explorer and in Unity anyway.
                "entity": entity,
                "clip": clip,
                "label": naming.label_of(folder.name, clips),
                "model": meta.get("model"),
                "promoted": meta.get("promoted"),
                "promoted_from": meta.get("promoted_from"),
                "normalized": is_normalized(folder, meta),
                "sheet": _entry(sheet, paths) if sheet else None,
                "contact": _entry(contact, paths) if contact else None,
                "cover": _entry(frames[len(frames) // 2], paths),
                "mtime": folder.stat().st_mtime,
                "recency": _recency(meta, folder),
                "size": sum(entry.stat().st_size for entry in frames),
            })
        cuts.sort(key=lambda item: item["recency"], reverse=True)

    return {
        "image": {"root": relative_to_root(paths.image_origin, paths.root), "items": images},
        "video": {"root": relative_to_root(paths.video_origin, paths.root), "items": cuts},
    }


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
