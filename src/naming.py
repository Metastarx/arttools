"""How a kept asset is named - the one place that decides.

``origin`` is what a game project imports and what a person opens six months later, so
every name that lands there follows one rule, and this module is that rule::

    origin/image/<entity>.png            one still, one character
    origin/video/<entity>_<clip>/        one cut of frames = one animation
        <entity>_<clip>_001.png ...      the frames, in playing order
        512/  256/                       the same names, scaled
        <entity>_<clip>.png              the sprite sheet
        <entity>_<clip>_contact.png      the contact sheet
        meta.json                        where it came from

Three things this buys, and they are the whole reason for it:

* **the name is the identity.** ``<entity>_<clip>`` is the sprite prefix Unity shows,
  what a search finds, and what a person reads off a file that has been copied
  somewhere else. Nothing has to be looked up to know what a file is.
* **it can be read from the right.** A cut strips a trailing clip name off an entity,
  so a folder somebody copied in - ``knight_walk`` - already reads as the walk cut of
  ``knight``, with no ``meta.json`` involved. Reading has to survive names this project
  did not write, because half of ``origin`` may have arrived from somewhere else.
* **it grows without a new layout.** A new animation is a new clip name; a new
  character is a new entity. Neither needs a new folder, column or extension.

What deliberately does NOT go into a name: the model that drew it, the time it was
generated, the index it had in the batch it came from. Those are generation
parameters - they belong in ``meta.json``. Whoever re-draws a walk with a better model
wants the new file to *replace* the old one, not to sit beside it under a name nobody
can rank.

The other half of the rule is that reading is tolerant and writing is strict: anything
dropped into ``origin`` is listed, played and imported as it is (see
:func:`sort_frames`), and a name only gets rewritten when the thing is promoted through
:mod:`src.library`. Nothing has to be renamed by hand to be usable.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable, List, Optional, Sequence, Tuple

# ``kept`` - the picked frames of a cut, written beside the full cut by stage 5. It is a
# folder name like everything else here, and it lives in this module so that reading the
# identity of ``.../<clip>/<model>/kept/`` knows to look one level up for the clip
# instead of calling the animation "kept".
SELECT_DIR = "kept"

# Frames are numbered with three digits so a folder of them sorts in playing order in
# every file manager and in Unity: 001 .. 999. Longer clips roll over to four digits
# rather than ending up with 1000 sorting next to 100.
FRAME_DIGITS = 3

# ``<stem>_001`` - the shape a frame this project wrote always has.
_FRAME_NUMBER = re.compile(r"^(?P<stem>.+?)_(?P<number>\d{%d,4})$" % FRAME_DIGITS)

# ``<spec>_01`` - the shape an artwork has inside a run: the spec, then which of the
# several pictures one request asked for.
_BATCH_NUMBER = re.compile(r"^(?P<entity>.+?)_(?P<index>\d{1,2})$")

# ``20260923-172725_monster_imp`` - a run folder. The stamp is when the run was made,
# which is a property of the run and not of the character: it is dropped when the run's
# name is read as an entity, and it is what runs are ranked by in the sidebar.
_RUN_STAMP = re.compile(r"(?P<date>\d{8})-(?P<time>\d{6})")


# --------------------------------------------------------------------------------------
# Frames
# --------------------------------------------------------------------------------------


def frame_name(stem: str, index: int, suffix: str = ".png") -> str:
    """``("knight_walk", 1) -> "knight_walk_001.png"``.

    One based: a person counts frames from one, and a clip that reports "119 frames"
    should be named 001..119, not 000..118.
    """
    return "%s_%0*d%s" % (str(stem), FRAME_DIGITS, max(1, int(index)), suffix)


def frame_index(name: Any) -> Optional[int]:
    """The number a frame name ends in, or ``None`` when it does not end in digits."""
    match = _FRAME_NUMBER.match(Path(str(name)).stem)
    return int(match.group("number")) if match else None


def is_frame_file(path: Any, folder: Any = None) -> bool:
    """Is this a frame, or one of the sheets that travel with the frames?

    Frames are everything that is a PNG and is not a sheet, which is what makes a cut
    from somewhere else readable without a manifest: no list of names to maintain, just
    "whatever is in there and is not a sheet".

    Two ways a PNG says it is a sheet. The word in its name (``sprite_sheet``,
    ``atlas_sheet``, and the ``<name>_contact`` this project writes) - and, since the
    sprite sheet is now named exactly like the folder it sits in
    (``knight_walk/knight_walk.png``), ``folder``. Pass it whenever the folder is known,
    or the sheet counts as one frame too many and a five frame walk reports seven.
    """
    from src.paths import IMAGE_SUFFIXES

    path = Path(path)
    if path.suffix.lower() not in IMAGE_SUFFIXES or is_sheet(path.name):
        return False
    return not (folder is not None and path.stem.lower() == Path(folder).name.lower())


def is_sheet(name: Any) -> bool:
    """Does this name say "sheet" rather than "frame"?

    ``contact`` counts: the contact sheet this project writes is ``<name>_contact.png``,
    which has no ``sheet`` in it at all.
    """
    text = str(name).lower()
    return "sheet" in text or "contact" in text


def sheet_files(folder: Path) -> List[Path]:
    """The sheets in a cut folder - the exact complement of :func:`cut_frames`."""
    folder = Path(folder)
    if not folder.is_dir():
        return []
    return sorted(
        (entry for entry in folder.iterdir()
         if entry.is_file() and not is_frame_file(entry, folder)),
        key=lambda entry: natural_key(entry.name),
    )


def natural_key(text: Any) -> Tuple:
    """A sort key that reads numbers as numbers: ``frame_9`` before ``frame_10``.

    Names this project writes are zero padded and would sort fine either way, but a
    folder somebody copied in is very often ``1.png ... 12.png``, and lexical order
    would play that 1, 10, 11, 12, 2 - a wrong animation that looks like a right one.
    """
    parts = re.split(r"(\d+)", str(text))
    # Tagged so a text part never gets compared against a number part.
    return tuple((1, int(part)) if part.isdigit() else (0, part.lower()) for part in parts)


def sort_frames(paths: Iterable[Path]) -> List[Path]:
    """Frames in playing order, whatever they are called."""
    return sorted(paths, key=lambda path: natural_key(Path(path).name))


def cut_frames(folder: Path) -> List[Path]:
    """The frames in a cut folder, in playing order. Sheets and sidecars are not frames."""
    folder = Path(folder)
    if not folder.is_dir():
        return []
    return sort_frames(
        entry for entry in folder.iterdir()
        if entry.is_file() and is_frame_file(entry, folder)
    )


# --------------------------------------------------------------------------------------
# Entities and clips
# --------------------------------------------------------------------------------------


def entity_of_artwork(stem: Any) -> str:
    """``monster_imp_01`` -> ``monster_imp``.

    An artwork inside a run is ``<spec>_<batch index>``: the index separates the several
    pictures one request asked for. A character has one still in ``origin``, so the
    index is dropped on the way in - it is a generation detail, like the model name.
    """
    match = _BATCH_NUMBER.match(str(stem))
    return match.group("entity") if match else str(stem)


def stamp_of(value: Any) -> Optional[str]:
    """``"20260923-172725_monster_imp"`` -> ``"20260923-172725"``, else ``None``.

    The stamp compares and sorts as a string, which is the whole reason both trees are
    named ``<YYYYMMDD>/<YYYYMMDD>-<HHMMSS>_<name>`` in the first place.
    """
    match = _RUN_STAMP.search(str(value or ""))
    return match.group("date") + "-" + match.group("time") if match else None


def without_stamp(value: Any) -> str:
    """``"20260923-172725_monster_imp"`` -> ``"monster_imp"``; anything else unchanged.

    A run carries the time so two runs the same day do not collide; a character in
    ``origin`` is read months later, when the time is the one thing nobody needs.
    """
    text = str(value or "")
    stamp = stamp_of(text)
    if stamp and text.startswith(stamp):
        return text[len(stamp):].lstrip("_-")
    return text


def still_name(entity: Any) -> str:
    """The name of a character's still: just the entity."""
    return str(entity)


def cut_name(entity: Any, clip: Any) -> str:
    """The name of one animation: ``<entity>_<clip>``.

    No clip is a real case - a folder with a name the project cannot split keeps its
    whole self as the entity - so a missing clip has to come back as ``knight_attack``
    and not as ``knight_attack_None``.
    """
    entity = str(entity or "")
    clip = str(clip or "")
    return entity + "_" + clip if clip else entity


def split_name(name: Any, clips: Sequence[str] = ()) -> Tuple[str, Optional[str]]:
    """``"knight_walk"`` -> ``("knight", "walk")``; ``"item_sword_icon"`` -> no clip.

    The clip is only stripped when the tail really is a clip name the project knows, so
    an entity that happens to end in something else (``item_sword_icon``, ``hero_knight_2d``)
    keeps its whole name. Longer clip names win, so ``hit_air`` is never read as the
    entity ``..._hit_air`` when both ``hit`` and ``hit_air`` are known.
    """
    text = str(name or "").strip()
    for clip in sorted({str(item) for item in clips if item}, key=len, reverse=True):
        suffix = "_" + clip.lower()
        if text.lower().endswith(suffix) and len(text) > len(suffix):
            return text[: len(text) - len(suffix)], clip
    return text, None


def declared_clips() -> List[str]:
    """The animations the project declares, without touching the disk.

    ``common.yaml`` is where this project says which animations it has (adding one is a
    one-file change), so that is the list. The built-in three are the fallback for a
    checkout with no harness around it.
    """
    from src import video

    names: List[str] = list(video.motion_names())
    try:
        from src.paths import load_paths
        from src.stages import video_defaults

        defaults = video_defaults(load_paths().harness / "common.yaml")
        for name in (defaults.get("clips") or []):
            if name and name not in names:
                names.append(str(name))
    except Exception:
        pass
    return names


def _subdirs(folder: Path) -> List[Path]:
    try:
        return sorted((entry for entry in folder.iterdir() if entry.is_dir()),
                      key=lambda entry: entry.name)
    except OSError:
        return []


def known_clips(paths: Optional[Any] = None) -> List[str]:
    """Every animation name the project can currently recognise.

    The declared ones first, then anything already used: a clip folder under
    ``resource/video/<day>/<run>/02_video/`` or ``03_frames/`` is an animation this
    project has made, so ``knight_attack`` reads as an attack the next time it is
    written without anybody editing a config file. Learning from the tree is what keeps
    a name working the second time it is used - which is exactly the case a person hits
    right after adding an animation, so it is the case worth handling.

    Only the run tree is walked, and only to a fixed depth: the point is to read the
    clip folders, not to search a hundred thousand frames.
    """
    names = declared_clips()
    seen = {name.lower() for name in names}

    def add(name: Any) -> None:
        text = str(name or "").strip()
        if text and text.lower() not in seen:
            seen.add(text.lower())
            names.append(text)

    if paths is None:
        try:
            from src.paths import load_paths

            paths = load_paths()
        except Exception:
            return names
    try:
        for day in _subdirs(paths.video_resource):
            for run in _subdirs(day):
                for stage in ("02_video", "03_frames"):
                    for clip_dir in _subdirs(run / stage):
                        add(clip_dir.name)
    except OSError:
        pass
    return names


def label_of(name: Any, clips: Sequence[str] = ()) -> str:
    """``"knight_walk"`` -> ``"knight · walk"`` - 一行里把两个身份分开写给人看。"""
    entity, clip = split_name(name, clips)
    return entity + " · " + clip if clip else entity
