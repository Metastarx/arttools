"""The local web interface for the pipeline.

Standard library only: no Flask, no Node, no build step. ``python main.py ui`` starts a
server on localhost, serves ``static/`` and drives the very same command line the
terminal uses - pressing the run button for a stage runs ``python main.py stage1 hero_idle``,
``python main.py run --from 2 --to 3 --run <run>``, ``python main.py stage4 ...``.
That is deliberate: the GUI can never drift away from the commands in the README,
because it does not have its own copy of the pipeline.

Why a web page instead of a desktop toolkit: every stage ends in something the browser
already knows how to show - a still, a video, a hundred-odd frames, a sprite sheet - and
``<video>`` plays the Seedance clips with no codec dependency to install.

Routes:

    GET  /                     the page
    GET  /api/state            paths, defaults, specs, styles, the four stages
    GET  /api/runs?root=&q=    what is under one browse root (image / video / kept / old)
    GET  /api/run?path=...     one run, stage by stage, with what each one produced
                               (``steps`` = the five folders on disk, ``stages`` = the
                               four the page draws)
    GET  /api/kept?path=...    one still, or one cut of frames, that was kept
    GET  /api/folder?path=...  a plain folder and its files, for the archive
    POST /api/command          the form -> the command line, for the live preview
    POST /api/job/start        run one stage (or the whole pipeline, or the picker)
    GET  /api/job              the job that is running, or the last one
    GET  /api/job/log?scan=N   only the log lines the page has not seen yet
    POST /api/job/stop         stop it
    POST /api/promote          copy a still, or a cut of frames, into origin
    POST /api/normalize        put a cut that is already in origin into the canonical
                               shape, in place - for a folder copied in from elsewhere
    GET  /api/file?path=&w=    a file under the project root (still, video, json)
    GET  /api/slice?path=&i=&w=  one frame out of a sprite sheet, cut here because
                               the strip is far too wide for a browser to decode
    POST /api/reveal           open a folder in the desktop file manager

The pipeline writes two trees - resource/image for the stills and resource/video for
the clips - and the page shows them as one run, twice. What is worth keeping is copied
into origin by the promote button, and it is origin/image that stage 3 animates.
"""

from __future__ import annotations

import io
import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src import naming
from src.paths import (
    IMAGE_DIR_NAME,
    IMAGE_SUFFIXES as _IMAGE_SUFFIXES,
    KEPT_KEY,
    OLD_KEY,
    VIDEO_DIR_NAME,
    ProjectPaths,
    load_paths,
    relative_to_root,
    resolve_inside,
)
from src.stages import (
    DEFAULT_CLIPS,
    DEFAULT_FILL,
    DEFAULT_SIZES,
    SELECT_DIR,
    STAGE_DIRS,
    STAGES,
    Run,
    resolve_run,
    video_defaults,
)

# ``n`` -> the stage, for the page's numbering. The page only ever sends a stage number,
# and this is the one place that turns it back into what it is made of.
STAGE_BY_NUMBER: Dict[int, Dict[str, Any]] = {int(stage["n"]): dict(stage) for stage in STAGES}

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

STATIC_DIR = Path(__file__).resolve().parent / "static"

# One list, shared with the pipeline: "which files count as artwork" has to mean the
# same thing to the promote button, to stage 3's picker and to stage 3 itself.
IMAGE_SUFFIXES = set(_IMAGE_SUFFIXES)
VIDEO_SUFFIXES = {".mp4", ".webm", ".mov"}
TEXT_SUFFIXES = {".json", ".txt", ".md", ".yaml", ".yml", ".jsonl", ".csv", ".log"}

# ``origin`` keeps two kinds of thing in one folder - the stills somebody picked and
# the frame sequences somebody picked - and the sidebar shows them as two tabs, because
# "which portrait" and "which walk cycle" get asked at different moments. ``kept`` still
# means "both at once": old links keep working and the command line calls the folder that.
KEPT_IMAGE_KEY = "kept_image"
KEPT_VIDEO_KEY = "kept_video"
KEPT_HALVES: Dict[str, str] = {
    KEPT_IMAGE_KEY: IMAGE_DIR_NAME,
    KEPT_VIDEO_KEY: VIDEO_DIR_NAME,
}

# The sidebar tabs, in order. The archive is not one of them - it is reached from the
# toolbar and only re-appears as a tab while it is what you are looking at, so there is
# always a way back out of it.
BROWSE_TABS: Tuple[str, ...] = (
    IMAGE_DIR_NAME, VIDEO_DIR_NAME, KEPT_IMAGE_KEY, KEPT_VIDEO_KEY,
)

# ``mimetypes`` consults the Windows registry, and on this machine that makes ``.js``
# come back as ``text/plain``. A browser happens to tolerate that inside a <script>,
# but it is wrong and it breaks the moment anything sets X-Content-Type-Options.
# Everything the project writes is listed here; ``mimetypes`` is only the fallback.
MIME_TYPES = {
    ".html": "text/html",
    ".css": "text/css",
    ".js": "text/javascript",
    ".json": "application/json",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".gif": "image/gif",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
    ".txt": "text/plain",
    ".md": "text/plain",
    ".yaml": "text/plain",
    ".yml": "text/plain",
    ".jsonl": "text/plain",
    ".csv": "text/csv",
    ".log": "text/plain",
}

# A clip name, a model name, a spec id: anything passed straight to the command line.
# Requiring a leading alphanumeric is what stops a value like ``--force`` from being
# read as a flag of its own.
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")

# What the page shows for each stage. The only place a stage is described is
# :data:`src.stages.STAGES`; this is that table, handed to the page as it is, so the tabs
# and the pips and the pipeline can never disagree about how many stages there are.
#
# ``steps`` rides along with every stage: it is which of the five run folders the stage is
# made of, and it is what the artifact panel opens. 阶段 2 is two of them, which is the
# whole reason the page's numbers and the folder numbers do not line up.


def is_safe_token(value: Any) -> bool:
    return bool(_SAFE_TOKEN.match(str(value).strip()))


def safe_tokens(value: Any) -> List[str]:
    """``"idle, walk"`` / ``["idle", "walk"]`` -> the two names, anything else rejected."""
    if value in (None, ""):
        return []
    raw = list(value) if isinstance(value, (list, tuple)) else re.split(r"[,\s]+", str(value))
    tokens: List[str] = []
    for item in raw:
        text = str(item).strip()
        if not text:
            continue
        if not is_safe_token(text):
            raise ValueError("不认识的取值: " + text)
        tokens.append(text)
    return tokens


# --------------------------------------------------------------------------------------
# The job runner - one CLI command at a time
# --------------------------------------------------------------------------------------


MAX_LOG_LINES = 5000


@dataclass
class Job:
    """One run of ``main.py``, with everything it has printed so far."""

    id: str
    label: str
    argv: List[str]
    started: float = field(default_factory=time.time)
    status: str = "running"          # running | done | failed | stopped
    code: Optional[int] = None
    ended: Optional[float] = None
    dropped: int = 0                 # log lines that fell off the front of ``lines``
    lines: List[str] = field(default_factory=list)
    proc: Optional[Any] = None
    stop_requested: bool = False

    @property
    def total(self) -> int:
        """How many lines have been printed altogether, including the dropped ones."""
        return self.dropped + len(self.lines)

    def snapshot(self, scan: int = 0) -> Dict[str, Any]:
        """Everything the page needs, and only the log lines it has not seen.

        ``scan`` is the count the page last received, so a long render does not resend
        the whole log on every poll.
        """
        start = max(0, scan - self.dropped)
        return {
            "id": self.id,
            "label": self.label,
            "command": " ".join(self.argv[2:]),
            "status": self.status,
            "code": self.code,
            "started": self.started,
            "ended": self.ended,
            "elapsed": (self.ended or time.time()) - self.started,
            "total": self.total,
            "dropped": self.dropped,
            "lines": self.lines[start:],
        }


class Runner:
    """Runs one pipeline command at a time.

    One at a time on purpose: the stages share a run folder, and two of them writing
    into it at once is how a half-finished run folder gets built.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._job: Optional[Job] = None
        self._seq = 0

    def current(self) -> Optional[Job]:
        with self._lock:
            return self._job

    def busy(self) -> bool:
        job = self.current()
        return job is not None and job.status == "running"

    def start(self, argv: Sequence[str], label: str, cwd: Path, env: Dict[str, str]) -> Job:
        with self._lock:
            if self._job is not None and self._job.status == "running":
                raise ValueError("已经有一个步骤在跑了，先停止再开新的")
            self._seq += 1
            job = Job(id="job-%d" % self._seq, label=label, argv=[str(a) for a in argv])
            self._job = job
        threading.Thread(target=self._pump, args=(job, cwd, env), daemon=True).start()
        return job

    def _append(self, job: Job, line: str) -> None:
        with self._lock:
            job.lines.append(line)
            overflow = len(job.lines) - MAX_LOG_LINES
            if overflow > 0:
                del job.lines[:overflow]
                job.dropped += overflow

    def _finish(self, job: Job, status: str, code: Optional[int]) -> None:
        with self._lock:
            job.status = status
            job.code = code
            job.ended = time.time()
            job.proc = None

    def _pump(self, job: Job, cwd: Path, env: Dict[str, str]) -> None:
        """Run the command and stream its output into the job.

        stderr is folded into stdout because the pipeline logs progress there; the page
        should show one ordered story, not two interleaved ones.
        """
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            job.proc = subprocess.Popen(
                job.argv, cwd=str(cwd), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                creationflags=flags,
            )
        except OSError as exc:
            self._append(job, "无法启动: " + str(exc))
            self._finish(job, "failed", None)
            return
        stream = job.proc.stdout
        if stream is not None:
            for line in stream:
                self._append(job, line.rstrip("\r\n"))
        code = job.proc.wait()
        status = "stopped" if job.stop_requested else ("done" if code == 0 else "failed")
        self._finish(job, status, code)

    def stop(self) -> bool:
        with self._lock:
            job = self._job
            if job is None or job.status != "running" or job.proc is None:
                return False
            job.stop_requested = True
            proc = job.proc
        try:
            proc.terminate()
        except OSError:
            return False
        return True


# --------------------------------------------------------------------------------------
# Browsing what is on disk
# --------------------------------------------------------------------------------------


def _cache_header(versioned: bool) -> str:
    """How long the browser may keep what it just got.

    Only ever long when the URL carries the file's mtime: the pipeline rewrites files
    in place on a re-run, and a cached copy of the old render has no way to know.
    """
    return "private, max-age=3600" if versioned else "no-store"


def _kind(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        return "image"
    if suffix in VIDEO_SUFFIXES:
        return "video"
    if suffix in TEXT_SUFFIXES:
        return "text"
    return "other"


def mime_of(path: Path) -> str:
    """The content type to send for a file, with the table above taking priority."""
    return MIME_TYPES.get(path.suffix.lower()) or (
        mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    )


def _entry(path: Path, paths: ProjectPaths) -> Dict[str, Any]:
    """A file as the page sees it: always relative to the project root.

    The browser never receives an absolute path, so a page that is left open keeps
    working when the project moves, and nothing it sends back can point outside.
    """
    stat = path.stat()
    return {
        "name": path.name,
        "path": relative_to_root(path, paths.root),
        "kind": _kind(path),
        "size": stat.st_size,
        "mtime": stat.st_mtime,
    }


def _is_noise(path: Path) -> bool:
    """``.gitkeep`` and friends keep a folder alive in git, but they are not assets.

    They are files like any other, so every otherwise-empty tree grew a ".gitkeep 0 B"
    row in the sidebar - noise next to the thing that is actually being looked for.
    """
    return path.name.startswith(".")


def _entries(folder: Path, paths: ProjectPaths, suffix: Optional[str] = None) -> List[Dict[str, Any]]:
    if not folder.is_dir():
        return []
    items = [
        p for p in folder.iterdir()
        if p.is_file() and (suffix is None or p.suffix.lower() == suffix)
    ]
    return [_entry(p, paths) for p in sorted(items, key=lambda p: p.name)]


def _frame_entries(folder: Path, paths: ProjectPaths) -> List[Dict[str, Any]]:
    """A folder's frames as rows, in playing order, sheets left out.

    The order comes from :mod:`src.naming`, which reads numbers as numbers: a folder
    somebody copied in as ``1.png .. 12.png`` plays 1, 2, 3 ... and not 1, 10, 11, 12, 2.
    A sheet is anything with the word in its name, so a cut that arrived with its own
    naming still counts its frames right.
    """
    return [_entry(path, paths) for path in naming.cut_frames(folder)]


def _sample(items: Sequence[Dict[str, Any]], count: int) -> List[Dict[str, Any]]:
    """A handful of evenly spaced entries - a hundred and nineteen frames need not all
    be sent to the page, but the first, the last and the shape in between should be."""
    if len(items) <= count or count < 2:
        return list(items)
    step = (len(items) - 1) / float(count - 1)
    return [items[int(round(index * step))] for index in range(count)]


def _size_dirs(folder: Path) -> Dict[str, int]:
    """``{"512": 119, "256": 119}`` for the delivery copies a stage wrote."""
    if not folder.is_dir():
        return {}
    found: Dict[str, int] = {}
    for child in sorted(folder.iterdir()):
        if child.is_dir() and child.name.isdigit():
            found[child.name] = sum(1 for p in child.iterdir() if p.suffix.lower() == ".png")
    return found


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _subfolders(folder: Path) -> List[Path]:
    if not folder.is_dir():
        return []
    return sorted((p for p in folder.iterdir() if p.is_dir()), key=lambda p: p.name)


def is_date_folder(name: str) -> bool:
    """``20260923`` - the top level of ``resource/`` is one folder per day."""
    return len(name) == 8 and name.isdigit()


def _ordered_clip_dirs(folder: Path) -> List[Path]:
    """``idle``, ``walk``, ``hit`` - the order the project documents - then the rest.

    Alphabetical order would put ``hit`` first, which reads like a mistake when the
    page is listing the three clips of a run.
    """
    order = {name: index for index, name in enumerate(DEFAULT_CLIPS)}
    return sorted(
        _subfolders(folder), key=lambda p: (order.get(p.name, len(order)), p.name)
    )


def _ordered_folders(base: Path) -> List[Path]:
    """Newest day first.

    Anything that is not a date - the hand-filed ``old/`` archive, say - is listed
    after the dates rather than sorted in among them, because the dates are the ones
    that grow and the archives are the ones that do not move.
    """
    folders = [p for p in base.iterdir() if p.is_dir()]
    dated = sorted(
        (p for p in folders if is_date_folder(p.name)), key=lambda p: p.name, reverse=True
    )
    other = sorted(
        (p for p in folders if not is_date_folder(p.name)), key=lambda p: p.name, reverse=True
    )
    return dated + other


def _ordered_runs(day: Path) -> List[Path]:
    """The runs of one day, newest first.

    Ordered by the stamp in the folder name, which is what a run folder is for - and by
    mtime when there is none, which is what a run folder copied in from somewhere else
    looks like. Dated runs come first for the same reason dated days do: they are the
    ones this project wrote.
    """
    def key(folder: Path) -> Tuple[int, Any]:
        stamp = naming.stamp_of(folder.name)
        if stamp:
            return (1, stamp)
        try:
            return (0, folder.stat().st_mtime)
        except OSError:
            return (0, 0.0)

    if not day.is_dir():
        return []
    return sorted((entry for entry in day.iterdir() if entry.is_dir()), key=key, reverse=True)


# ``resource/image/<day>/<run>/...`` or ``resource/video/<day>/<run>/...`` - the shape
# every promoted file records about where it came from. Being able to read it back is
# what lets the kept view show the run around a kept file instead of just the file.
_RUN_IN_PATH = re.compile(r"(?:^|/)(?:image|video)/([0-9]{8})/([^/]+)(?:/|$)")


def run_key_of(value: Any) -> Optional[str]:
    """``resource/image/20260923/20260923-172725_main/...`` -> ``20260923/20260923-...``."""
    text = str(value or "")
    match = _RUN_IN_PATH.search(text)
    if not match:
        return None
    return match.group(1) + "/" + match.group(2)


def _run_item(paths: ProjectPaths, key: str) -> Dict[str, Any]:
    """One run as a row of the sidebar: both halves, and what each stage produced."""
    from src import stages

    row = stages.run_row(Run(resource=paths.resource, key=key))
    row["path"] = key
    return row


def _promote_candidates(
    paths: ProjectPaths, run: Run, kind: str
) -> List[Dict[str, Any]]:
    """What the row's 「→ 保留」 button would copy into ``origin``, best first.

    One click has to mean one obvious thing, so a row offers the few things actually
    worth keeping rather than every file it holds:

    * in the stills tree, the artwork stage 2 produced - the 512 copy first, because
      that is the size the video stages read back;
    * in the clips tree, one entry per cut: the frames somebody picked out of it if they
      picked any, because that selection *is* the animation, and the full take otherwise.

    ``name`` and the two halves ride along with the path, which is what makes this a
    single click instead of a rename dialog. The name is ``<entity>_<clip>`` and nothing
    else: no model, no time, no ``_kept`` - all three would land in Unity and in a search
    six months later. The full take of a cut that has a picked set is deliberately *not*
    offered here; it is one button away in the cut panel, where the choice is explicit.
    """
    from src import library

    if kind == IMAGE_DIR_NAME:
        ready = run.stage_dir(2)
        for folder, note in ((ready / "512", "512"), (ready, ""), (run.stage_dir(1), "原画")):
            if not folder.is_dir():
                continue
            files = sorted(
                (entry for entry in folder.iterdir()
                 if entry.is_file() and entry.suffix.lower() in IMAGE_SUFFIXES),
                key=lambda entry: entry.name,
            )
            if files:
                return [{
                    "kind": IMAGE_DIR_NAME,
                    "path": relative_to_root(files[0], paths.root),
                    # ``monster_imp_01`` is the first of the pictures this run drew;
                    # ``monster_imp`` is the character, and that is what gets kept.
                    "name": naming.entity_of_artwork(files[0].stem),
                    "label": files[0].name + ("（" + note + "）" if note else ""),
                    "frames": 0,
                }]
        return []

    cuts: List[Dict[str, Any]] = []
    frames_root = run.stage_dir(5)
    if not frames_root.is_dir():
        return []
    # ``20260923-172725_monster_imp`` -> ``monster_imp``: the stamp is a time, and a name
    # in origin is read months later when the time is the one thing nobody needs.
    who = naming.without_stamp(run.name)
    for clip_dir in _ordered_clip_dirs(frames_root):
        for model_dir in _subfolders(clip_dir):
            picked = model_dir / SELECT_DIR
            chosen = naming.cut_frames(picked)
            if chosen:
                cuts.append({
                    "kind": VIDEO_DIR_NAME,
                    "path": relative_to_root(picked, paths.root),
                    "name": naming.cut_name(who, clip_dir.name),
                    "entity": who,
                    "clip": clip_dir.name,
                    "label": clip_dir.name + " 留存 %d 帧" % len(chosen),
                    "frames": len(chosen),
                })
                continue
            take = naming.cut_frames(model_dir)
            if not take:
                continue
            cuts.append({
                "kind": VIDEO_DIR_NAME,
                "path": relative_to_root(model_dir, paths.root),
                "name": naming.cut_name(who, clip_dir.name),
                "entity": who,
                "clip": clip_dir.name,
                "label": clip_dir.name + " 全段 %d 帧" % len(take),
                "frames": len(take),
            })
    return cuts


def _scan_work(paths: ProjectPaths, kind: str, needle: str) -> Dict[str, Any]:
    """The two halves of the work tree, listed by day.

    The same run appears in both lists - it is drawn in one tree and animated in the
    other - and every row carries the state of all five stages, so the page can say what
    a run still needs without being asked twice.
    """
    base = paths.work_root(kind)
    groups: List[Dict[str, Any]] = []
    loose: List[Dict[str, Any]] = []
    if base.is_dir():
        loose = [_entry(p, paths) for p in sorted(base.iterdir())
                 if p.is_file() and not _is_noise(p)]
        for day in _ordered_folders(base):
            rows = [
                _run_item(paths, day.name + "/" + folder.name)
                for folder in _ordered_runs(day)
            ]
            # 每行带一份「一键转录」的候选。放这里而不是让页面自己拼路径：页面只认
            # run key，不知道阶段 2 的 512 副本、阶段 5 的 kept 这些约定。
            for row in rows:
                row["promote"] = _promote_candidates(
                    paths, Run(resource=paths.resource, key=row["path"]), kind
                )
            if needle:
                rows = [row for row in rows
                        if needle in row["name"].lower() or needle in row["path"].lower()]
            # A day folder with nothing under it is left over from a run that was
            # deleted or that failed before it wrote anything; showing an empty heading
            # for it just makes the sidebar look like something is missing.
            if rows:
                groups.append({
                    "date": day.name,
                    "is_date": is_date_folder(day.name),
                    "runs": rows,
                })
    return {"groups": groups, "loose": loose}


def _kept_row(item: Dict[str, Any]) -> Dict[str, Any]:
    """A kept cut, shaped like a run row so the sidebar draws it the same way.

    ``entity`` / ``clip`` / ``label`` come straight from :mod:`src.library`, which read
    them off the folder name: one place decides what a kept thing is called, and the
    page only has to draw it. ``normalized`` is what the 「整理」 button keys off.
    """
    return {
        "name": item["name"],
        "path": item["path"],
        "kept": VIDEO_DIR_NAME,
        "steps": {},
        "done": 0,
        "clips": [item["clip"]] if item.get("clip") else [],
        "models": [item["model"]] if item.get("model") else [],
        "frames": item.get("frames"),
        "cover": item.get("cover"),
        "from": item.get("promoted_from"),
        "run": run_key_of(item.get("promoted_from")),
        "label": item.get("label"),
        "entity": item.get("entity"),
        "clip": item.get("clip"),
        "normalized": item.get("normalized"),
        "promoted": item.get("promoted"),
        "mtime": item.get("mtime") or 0.0,
        "files": item.get("frames") or 0,
    }


def _scan_kept(
    paths: ProjectPaths, needle: str, kind: Optional[str] = None
) -> Dict[str, Any]:
    """origin - the stills and the frame sequences somebody decided were good.

    Shaped like the run tree so the sidebar looks the same in every view, but nothing in
    here is work in progress: it is what the video stages read, and what goes to Unity.
    Both halves come back **newest first** straight from :func:`src.library.list_origin`
    - the last thing kept is the thing being worked on, so it belongs at the top - and
    neither list is re-sorted here, because two orders for one list is how they drift.

    ``kind`` narrows it to one half, which is what the two kept tabs ask for; without it
    both halves come back, which is what the old single ``kept`` view wants.
    """
    from src import library

    kept = library.list_origin(paths)
    stills: List[Dict[str, Any]] = []
    for item in kept["image"]["items"]:
        meta = item.get("meta") or {}
        stills.append({
            "name": str(item["name"]).rsplit(".", 1)[0],
            "path": item["path"],
            "kept": IMAGE_DIR_NAME,
            "steps": {},
            "done": 0,
            "clips": [],
            "models": [],
            "entity": item.get("entity"),
            "cover": item,
            "size": item.get("size"),
            "from": meta.get("source"),
            "run": run_key_of(meta.get("source")),
            "mtime": item.get("mtime") or 0.0,
        })
    cuts = [_kept_row(item) for item in kept["video"]["items"]]
    if needle:
        stills = [row for row in stills if needle in row["name"].lower()]
        cuts = [row for row in cuts if needle in row["name"].lower()]
    groups: List[Dict[str, Any]] = []
    if stills and kind in (None, IMAGE_DIR_NAME):
        groups.append({"date": "原画", "is_date": False, "kind": IMAGE_DIR_NAME,
                       "runs": stills})
    if cuts and kind in (None, VIDEO_DIR_NAME):
        groups.append({"date": "视频", "is_date": False, "kind": VIDEO_DIR_NAME,
                       "runs": cuts})
    return {"groups": groups, "loose": []}


def _scan_archive(paths: ProjectPaths, needle: str) -> Dict[str, Any]:
    """resource/old - what was made before the trees were split.

    Listed as folders, because that is what they are now: the layout moved, so a folder
    in here is not a run and nothing can be run against it. It stays visible so the
    history is not lost, and copyable so anything worth keeping can be promoted by hand.
    """
    base = paths.browse_roots()[OLD_KEY]
    groups: List[Dict[str, Any]] = []
    loose: List[Dict[str, Any]] = []
    if base.is_dir():
        loose = [_entry(p, paths) for p in sorted(base.iterdir())
                 if p.is_file() and not _is_noise(p)]
        for day in _ordered_folders(base):
            rows: List[Dict[str, Any]] = []
            for child in sorted(
                (p for p in day.iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True
            ):
                rows.append({
                    "name": child.name,
                    "path": relative_to_root(child, paths.root),
                    "kept": OLD_KEY,
                    "steps": {},
                    "done": 0,
                    "clips": [],
                    "models": [],
                    "files": sum(1 for entry in child.rglob("*") if entry.is_file()),
                    "mtime": child.stat().st_mtime,
                })
            if needle:
                rows = [row for row in rows if needle in row["name"].lower()]
            if rows:
                groups.append({
                    "date": day.name,
                    "is_date": is_date_folder(day.name),
                    "runs": rows,
                })
    return {"groups": groups, "loose": loose}


def scan_runs(
    paths: ProjectPaths, root_key: str = IMAGE_DIR_NAME, search: str = ""
) -> Dict[str, Any]:
    """The sidebar, in one call: whatever is under one of the browse roots.

    ``roots`` in the answer is the tab bar, so it lists the four tabs and not every key
    this function accepts: ``kept`` and ``old`` are still readable - an old link to
    either one lands somewhere sensible - they just are not offered as tabs.
    """
    roots = paths.browse_roots()
    known = set(roots) | set(KEPT_HALVES)
    key = root_key if root_key in known else IMAGE_DIR_NAME
    needle = search.strip().lower()
    if key in KEPT_HALVES:
        body = _scan_kept(paths, needle, kind=KEPT_HALVES[key])
    elif key == KEPT_KEY:
        body = _scan_kept(paths, needle)
    elif key == OLD_KEY:
        body = _scan_archive(paths, needle)
    else:
        body = _scan_work(paths, key, needle)
    # 两个保留页签看的是同一个文件夹，只是各看一半。
    where = {
        IMAGE_DIR_NAME: roots[IMAGE_DIR_NAME],
        VIDEO_DIR_NAME: roots[VIDEO_DIR_NAME],
        KEPT_IMAGE_KEY: roots[KEPT_KEY],
        KEPT_VIDEO_KEY: roots[KEPT_KEY],
    }
    tabs = {name: relative_to_root(where[name], paths.root) for name in BROWSE_TABS}
    base = roots[KEPT_KEY] if key in KEPT_HALVES else roots[key]
    return dict(body, root=key, roots=tabs, base=relative_to_root(base, paths.root))


def _inside_root(paths: ProjectPaths, rel: str, base: Path) -> Path:
    """A path the page sent back -> an absolute path that is inside ``base``.

    The page only ever sees paths relative to the project root, so the run it sends
    back reads ``resource/20260923/...``. Joining that onto the resource folder would
    give ``resource/resource/...``, so it is resolved against the root first and then
    held to the folder it is supposed to be in.
    """
    text = str(rel or "").strip()
    if not text:
        raise ValueError("没有指定路径")
    target = resolve_inside(paths.root, text)
    if target != base and base not in target.parents:
        raise ValueError(
            "路径不在 " + relative_to_root(base, paths.root) + " 里面: " + text
        )
    return target


def _cut_entry(model_dir: Path, paths: ProjectPaths, full: bool = False) -> Dict[str, Any]:
    """One cut: the frames, the delivery copies, the sheets and the stats.

    full sends every frame instead of a dozen samples, which is what the frame viewer
    needs in order to step through a walk cycle one frame at a time.

    Everything here is read off the folder rather than out of a manifest, because a cut
    copied in from somewhere else has no manifest: the frames in playing order, the two
    sheets by name (this project's ``<name>.png``, the older ``sprite_sheet.png``, or
    anything with the word in it), and the name off the path.
    """
    from src import library

    meta = _read_json(model_dir / library.META_NAME) or {}
    frames = _frame_entries(model_dir / "512", paths) or _frame_entries(model_dir, paths)
    entity, clip, name = library.cut_identity(paths, model_dir)
    # The frames that were chosen out of this cut. Same shape as the cut itself, so the
    # page plays it with the same player; absent until somebody picks any.
    kept_dir = model_dir / SELECT_DIR
    kept = _cut_entry(kept_dir, paths, full=True) if (kept_dir / library.META_NAME).is_file() else None
    sheet, contact = library.sheet_of(model_dir)
    return {
        "dir": relative_to_root(model_dir, paths.root),
        "name": name,
        "entity": entity,
        "clip": clip,
        "frames": meta.get("frames") or len(frames),
        "canvas": meta.get("canvas"),
        "sizes": meta.get("sizes") or {},
        "stats": meta.get("stats") or {},
        "fps": meta.get("fps"),
        "skipped_frames": meta.get("skipped_frames"),
        "promoted_from": meta.get("promoted_from"),
        "sheet": _entry(sheet, paths) if sheet else None,
        "sheet_slice": sheet_geometry(sheet, meta) if sheet else None,
        "contact": _entry(contact, paths) if contact else None,
        "samples": frames if full else _sample(frames, 12),
        "kept": kept,
        # What was chosen out of this cut, and out of how many - the picking page opens
        # on the selection that is already there instead of on an empty one.
        "selected_frames": meta.get("selected_frames"),
        "full_frames": meta.get("full_frames"),
    }


def describe_run(paths: ProjectPaths, rel: str) -> Dict[str, Any]:
    """One run, stage by stage - the stills in one tree and the clips in the other.

    Both halves are read together, because the run *is* the pair. The page has to be
    able to show where a clip came from and what it turned into without knowing that
    they are two folders, so the reply is one object either way.
    """
    run = resolve_run(paths.resource, rel)
    if not run.exists():
        raise ValueError("找不到这个 run: " + str(rel))

    clips: List[Dict[str, Any]] = []
    for clip_dir in _ordered_clip_dirs(run.stage_dir(4)):
        prompt = clip_dir / "prompt.txt"
        models: List[Dict[str, Any]] = []
        for model_dir in _subfolders(clip_dir):
            mp4 = model_dir / "source.mp4"
            meta = model_dir / "meta.json"
            models.append({
                "clip": clip_dir.name,
                "model": model_dir.name,
                "video": _entry(mp4, paths) if mp4.is_file() else None,
                "meta": _entry(meta, paths) if meta.is_file() else None,
            })
        clips.append({
            "clip": clip_dir.name,
            "prompt": _entry(prompt, paths) if prompt.is_file() else None,
            "models": models,
        })

    cuts: List[Dict[str, Any]] = []
    for clip_dir in _ordered_clip_dirs(run.stage_dir(5)):
        for model_dir in _subfolders(clip_dir):
            cut = _cut_entry(model_dir, paths)
            cut["clip"] = clip_dir.name
            cut["model"] = model_dir.name
            cuts.append(cut)

    ready = run.stage_dir(2)
    steps: Dict[str, Any] = {
        str(number): {"dir": relative_to_root(run.stage_dir(number), paths.root)}
        for number in STAGE_DIRS
    }
    steps["1"]["images"] = _entries(run.stage_dir(1), paths, ".png")
    steps["2"]["images"] = _entries(ready, paths, ".png")
    steps["2"]["sizes"] = _size_dirs(ready)
    steps["2"]["dist"] = {key: _entries(ready / key, paths, ".png") for key in _size_dirs(ready)}
    steps["3"]["images"] = _entries(run.stage_dir(3), paths, ".png")
    steps["4"]["clips"] = clips
    steps["5"]["cuts"] = cuts

    # Each step also says whether it has produced anything, and the four stages add those
    # five flags up. The sidebar row and this panel both draw the same four pips, from
    # whichever of the two pieces of data the page happens to be holding.
    from src import stages as stage_module

    row = stage_module.run_row(run)
    for number, info in row["steps"].items():
        steps[number].update(ok=info["ok"], files=info["files"])

    from src import prompts as prompt_presets

    return {
        "name": run.name,
        "run": run.key,
        "day": run.day,
        "path": run.key,
        # What stage 3 recorded about the prompt. Sent up so stage 4 can show the presets
        # it is about to inherit, and let them be changed for one run.
        "prompt_presets": _read_json(run.stage_dir(3) / prompt_presets.SELECTION_NAME),
        "image_dir": relative_to_root(run.image_dir, paths.root),
        "video_dir": relative_to_root(run.video_dir, paths.root),
        "has_image": run.image_dir.is_dir(),
        "has_video": run.video_dir.is_dir(),
        # ``steps`` is the five folders, in order, with what each holds; ``stages`` is the
        # four the page shows, each already added up from its steps.
        "steps": steps,
        "stages": row["stages"],
        "run_json": _read_json(run.image_dir / "run.json"),
    }


def describe_kept(paths: ProjectPaths, rel: str) -> Dict[str, Any]:
    """One thing that was kept: a still to animate, or a cut of frames to import."""
    from src import library

    target = _inside_root(paths, rel, paths.origin)
    if target.is_file():
        meta = _read_json(target.with_suffix(".json")) or {}
        return {
            "kind": IMAGE_DIR_NAME,
            "name": target.stem,
            "path": relative_to_root(target, paths.root),
            "meta": meta,
            "image": _entry(target, paths),
            "source": meta.get("source"),
            "prompt": meta.get("prompt"),
            "character": meta.get("character"),
            # The run this still came out of, when it can be read off the promoted path.
            # The page uses it to show the five stages around a kept file, so keeping
            # something does not cost you the view of the pipeline that made it.
            "run": run_key_of(meta.get("source")),
        }
    if not target.is_dir():
        raise ValueError("origin 里找不到: " + str(rel))
    # Only ``origin/video/<name>`` is a cut. ``origin`` and ``origin/image`` are folders of
    # folders, and describing one as a cut reports an empty clip of zero frames instead of
    # saying what it is - which reads like "this animation is broken" rather than "pick
    # something inside it".
    if target != paths.video_origin and paths.video_origin not in target.parents:
        raise ValueError(
            relative_to_root(target, paths.root)
            + " 是一个目录，不是一段帧 —— 选里面的原画或帧序列"
        )
    cut = _cut_entry(target, paths, full=True)
    meta = _read_json(target / library.META_NAME) or {}
    cut.update({
        "kind": VIDEO_DIR_NAME,
        "name": target.name,
        "path": relative_to_root(target, paths.root),
        "sizes": cut["sizes"] or _size_dirs(target),
        "run": run_key_of(meta.get("promoted_from")),
        "label": naming.label_of(target.name, naming.known_clips(paths)),
        "normalized": library.is_normalized(target, meta),
        "promoted": meta.get("promoted"),
        "promoted_from": meta.get("promoted_from"),
    })
    return cut


def describe_cut(paths: ProjectPaths, rel: str) -> Dict[str, Any]:
    """Every frame of one cut in the work tree, for the frame viewer.

    A run's overview sends a dozen samples, because a hundred and nineteen thumbnails
    would swamp it. Stepping through a walk cycle one frame at a time needs all of them,
    and they come from here - the 512 copy, which is what gets imported into Unity.
    """
    target = resolve_inside(paths.root, rel)
    if target != paths.resource and paths.resource not in target.parents:
        raise ValueError("只认 resource 下面的帧目录: " + str(rel))
    if not target.is_dir():
        raise ValueError("找不到: " + str(rel))
    cut = _cut_entry(target, paths, full=True)
    cut["path"] = relative_to_root(target, paths.root)
    return cut


def describe_folder(paths: ProjectPaths, rel: str, limit: int = 400) -> Dict[str, Any]:
    """A plain folder and everything under it: how the archive is browsed by hand."""
    target = _inside_root(paths, rel, paths.resource)
    if not target.is_dir():
        raise ValueError("找不到目录: " + str(rel))
    files = [p for p in sorted(target.rglob("*")) if p.is_file()]
    return {
        "name": target.name,
        "path": relative_to_root(target, paths.root),
        "total": len(files),
        "files": [_entry(p, paths) for p in files[:limit]],
        "truncated": len(files) > limit,
    }


# --------------------------------------------------------------------------------------
# Turning the page's form into a command
# --------------------------------------------------------------------------------------


# ``--frames`` is the one value on the command line that is not a bare name: it is a
# list of numbers and ranges. Only those characters are allowed through, so a form
# cannot smuggle anything else onto the line.
_FRAME_LIST = re.compile(r"^[0-9,\-\s]*$")


def _add(argv: List[str], form: Dict[str, Any], key: str, flag: str, cast=None) -> None:
    value = form.get(key)
    if value in (None, ""):
        return
    if cast is not None:
        try:
            value = cast(value)
        except (TypeError, ValueError):
            return
    if flag == "--frames" and not _FRAME_LIST.match(str(value)):
        raise ValueError("帧号只能写数字和范围，比如 0-40,45,50-70")
    argv += [flag, str(value)]


def _add_keying_flags(
    argv: List[str], form: Dict[str, Any], *,
    sizes: bool = True, trim: bool = True, flatten: bool = True,
) -> None:
    """The keying knobs, and only the ones the stage on the other end reads.

    They are not one blob. 阶段 2 crops the still and writes its delivery sizes; 阶段 4
    does the same to every frame *and* snaps the clip's noisy backdrop back onto one
    colour code; 阶段 3 reads none of them. Handing every flag to every stage would put
    flags in the log that the command quietly ignores, and the log is the thing people
    copy out of.
    """
    _add(argv, form, "key_color", "--key-color")
    _add(argv, form, "tolerance", "--tolerance")
    if sizes:
        if form.get("no_sizes"):
            argv.append("--no-sizes")
        else:
            found = safe_tokens(form.get("sizes"))
            if found:
                argv += ["--sizes", ",".join(found)]
    if trim and form.get("no_trim"):
        argv.append("--no-trim")
    if flatten:
        _add(argv, form, "flatten_tolerance", "--flatten-tolerance")
        if form.get("no_flatten"):
            argv.append("--no-flatten")


def _add_clip_flags(argv: List[str], form: Dict[str, Any]) -> None:
    clips = safe_tokens(form.get("clips"))
    if clips:
        argv += ["--clips", ",".join(clips)]
    models = safe_tokens(form.get("models"))
    if models:
        argv += ["--models", *models]


def _spec_ids(form: Dict[str, Any]) -> List[str]:
    """The specs stage 1 should draw, in the order they were ticked.

    The page offers a list to tick through rather than a single choice, because drawing
    four characters one at a time is four presses of the same button: ``gen`` takes any
    number of ids and each one becomes its own run either way.
    """
    raw = form.get("spec")
    items = raw if isinstance(raw, (list, tuple)) else ([raw] if raw else [])
    found: List[str] = []
    for item in items:
        spec = str(item or "").strip()
        if not spec:
            continue
        if not is_safe_token(spec):
            raise ValueError("spec 名字不合法: " + spec)
        if spec not in found:
            found.append(spec)
    if not found:
        raise ValueError("先勾一个 spec —— 阶段 1 要画的是哪个角色")
    return found


def _spec_label(specs: Sequence[str]) -> str:
    return specs[0] if len(specs) == 1 else ("%d 个角色" % len(specs))


def _preset_names(form: Dict[str, Any]) -> List[str]:
    """The prompt presets the page ticked, in the order they were ticked.

    A preset name is a file name in ``hareness/prompts`` and it goes on the command line
    as the value of ``--preset``, never as a flag of its own, so what it has to be safe
    of is looking like an option: a leading dash would do that and nothing else does.
    """
    raw = form.get("presets")
    items = raw if isinstance(raw, (list, tuple)) else ([raw] if raw else [])
    found: List[str] = []
    for item in items:
        name = str(item or "").strip()
        if not name:
            continue
        if name.startswith("-") or "\n" in name:
            raise ValueError("预设名字不能用: " + name)
        if name not in found:
            found.append(name)
    return found


def _preset_selection(form: Dict[str, Any], paths: ProjectPaths) -> List[str]:
    """The ticked presets -> the ``--preset`` flags this command should carry.

    ``common`` is never sent. It is applied by :func:`src.prompts.resolve` whether or not
    anybody names it, so sending it would only make an empty selection look like a
    deliberate "no presets" - and that is exactly what stops stage 4 from picking up the
    presets stage 3 recorded.
    """
    from src import prompts as prompt_presets

    folder = prompt_presets.preset_dir(paths.harness)
    chosen = prompt_presets.resolve(folder, _preset_names(form))
    return [preset.name for preset in chosen if not preset.is_common]


def stage_of(form: Dict[str, Any]) -> Any:
    """``"all"`` for the whole pipeline, 1-4 for one stage, ``"select"`` for the picker.

    The four are *stages*, not the five steps underneath them: 阶段 2 is steps 2 and 3,
    which is why one button can be worth two commands. Anything else is a mistake worth
    saying out loud rather than turning into a command that does something unexpected.
    """
    value = form.get("stage")
    if value == "all":
        return "all"
    if value == "select":
        # Not a stage at all: choosing frames happens after them, on what 阶段 4 already
        # cut, and costs nothing. It rides the same command line and the same log.
        return "select"
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError("不知道要跑哪个阶段: " + str(value))
    if number not in STAGE_BY_NUMBER:
        raise ValueError("阶段必须是 1-4，或者 all")
    return number


def _stage_steps(value: Any) -> int:
    """The page's stage number -> the first step that stage is made of.

    The page counts four stages, the command line counts the five steps, and this is the
    bridge between them: carrying on from 阶段 3 is ``--from 4``, because 视频生成 is the
    fourth step on disk.
    """
    try:
        number = int(value or 1)
    except (TypeError, ValueError):
        raise ValueError("从第几阶段开始要写 1-4: " + str(value))
    if number not in STAGE_BY_NUMBER:
        raise ValueError("从第几阶段开始要写 1-4: " + str(value))
    return int(STAGE_BY_NUMBER[number]["steps"][0])


def _selected_run(form: Dict[str, Any], paths: ProjectPaths, required: bool) -> Optional[str]:
    """The run the form is aimed at, as a key - never as a path.

    A run is named day/name and that is what goes on the command line, so the line in
    the log can be pasted into a terminal after the project has been copied elsewhere,
    and there is nothing to rewrite when the trees move.
    """
    key = str(form.get("run") or "").strip()
    if not key:
        if required:
            raise ValueError(
                "这个阶段要先有一条 run —— 在左边点一个 run，"
                "或者先跑阶段 2（它会替原画开一条新 run）"
            )
        return None
    run = resolve_run(paths.resource, key)
    if not run.exists():
        raise ValueError("这个 run 不存在: " + key)
    return run.key


def _source_paths(form: Dict[str, Any], paths: ProjectPaths) -> List[str]:
    """The artwork this run should be built from, relative to the project root.

    Usually left empty: with nothing named, 阶段 2 works on the run's own 01_artwork, and
    with no run either it starts from whatever is in origin/image - which is what the
    promote button on a still is for.
    """
    raw = form.get("source")
    items = raw if isinstance(raw, (list, tuple)) else ([raw] if raw else [])
    found: List[str] = []
    for item in items:
        text = str(item or "").strip()
        if not text:
            continue
        target = resolve_inside(paths.root, text)
        if not target.is_file():
            raise ValueError("找不到这张原稿: " + text)
        found.append(relative_to_root(target, paths.root))
    return found


def _where(paths: ProjectPaths) -> List[str]:
    """``--out``, when the console is browsing a resource folder somewhere else.

    It goes *after* the subcommand - ``main.py --out X run`` is not a command line this
    project accepts, ``main.py run --out X`` is - which is why the join happens here and
    not once at the front of the argv.
    """
    if paths.resource == paths.root / "resource":
        return []
    return ["--out", str(paths.resource)]


def build_command(stage: Any, form: Dict[str, Any], paths: ProjectPaths) -> Tuple[List[str], str]:
    """The page's form -> the exact python main.py ... the README documents.

    Every value is checked before it is put on the command line: the tokens that reach
    the pipeline are names (clips, models, specs, sizes), and the paths are resolved
    inside the project root and written relative to it, so the line in the log can be
    pasted into a terminal anywhere. Nothing here is ever handed to a shell.
    """
    argv: List[str] = [sys.executable, str(paths.root / "main.py")]

    if stage == "all":
        argv.append("run")
        argv += _where(paths)
        run_key = _selected_run(form, paths, required=False)
        if run_key is not None:
            # The page asks which *stage* to carry on from; the command line counts the
            # five steps, so 阶段 3 is "--from 4".
            before = _stage_steps(form.get("from_stage"))
            argv += ["--run", run_key, "--from", str(before)]
            label = "全流程 · " + run_key.split("/")[-1]
        else:
            specs = _spec_ids(form)
            argv += [*specs, "--from", "1"]
            label = "全流程 · " + _spec_label(specs)
        _add_clip_flags(argv, form)
        for name in _preset_selection(form, paths):
            argv += ["--preset", name]
        _add(argv, form, "extra_prompt", "--extra-prompt")
        _add(argv, form, "fill", "--fill")
        _add(argv, form, "facing", "--facing")
        _add(argv, form, "seconds", "--seconds", int)
        _add(argv, form, "resolution", "--resolution")
        _add(argv, form, "skip_frames", "--skip-frames", int)
        _add_keying_flags(argv, form)
        if form.get("force"):
            argv.append("--force")
        argv.append("-v")
        return argv, label

    if stage == "select":
        # Choosing frames is not one of the four stages: it happens after them, on what
        # 阶段 4 already cut, and it costs nothing. It still goes through the same
        # command line and the same log, so the page is doing exactly what the README
        # tells a person to type.
        argv.append("select")
        argv += _where(paths)
        run_key = _selected_run(form, paths, required=True)
        argv += ["--run", run_key]
        _add_clip_flags(argv, form)
        _add(argv, form, "frames", "--frames")
        _add_keying_flags(argv, form)
        if form.get("clear"):
            argv.append("--clear")
        argv.append("-v")
        return argv, "挑帧 · " + run_key.split("/")[-1]

    number = int(stage)
    info = STAGE_BY_NUMBER[number]
    label = "阶段 %d · %s" % (number, info["title"])

    if number == 1:
        specs = _spec_ids(form)
        argv.append("stage1")
        argv += _where(paths)
        argv += list(specs)
        _add(argv, form, "count", "--count", int)
        _add(argv, form, "style", "--style")
        if form.get("dry_run"):
            argv.append("--dry-run")
        argv.append("-v")
        return argv, label + " · " + _spec_label(specs)

    if number == 2:
        # 原画处理 is the two local steps and no model call: key the green out of the
        # still, crop it, write 512 / 256 - then lay the 512 back onto flat green with a
        # margin, which is the picture the video model is handed. One command runs both
        # (``--from 2 --to 3``) and stops before anything costs money.
        #
        # A run is optional here. Named, the still comes from its own 01_artwork and the
        # result lands in it; not named, the still is the one picked on the left and a
        # new run opens named after it.
        source = _source_paths(form, paths)
        run_key = _selected_run(form, paths, required=False)
        if not source and run_key is None:
            raise ValueError("先选一张原画 —— 阶段 2 要把哪张原稿处理成视频输入")
        argv += ["run", "--from", "2", "--to", "3"]
        argv += _where(paths)
        if run_key:
            argv += ["--run", run_key]
            label += " · " + run_key.split("/")[-1]
        else:
            label += " · 新 run · " + Path(source[0]).stem
        if source:
            argv += ["--source", *source]
        for name in _preset_selection(form, paths):
            argv += ["--preset", name]
        _add(argv, form, "extra_prompt", "--extra-prompt")
        _add(argv, form, "fill", "--fill")
        _add_keying_flags(argv, form, flatten=False)
        argv.append("-v")
        return argv, label

    run_key = _selected_run(form, paths, required=True)
    # 视频生成 is step 4 and 视频处理 is step 5, whatever the page calls them.
    argv.append("stage%d" % int(info["steps"][0]))
    argv += _where(paths)
    argv += ["--run", run_key]
    label += " · " + run_key.split("/")[-1]

    if number == 3:
        _add_clip_flags(argv, form)
        for name in _preset_selection(form, paths):
            argv += ["--preset", name]
        _add(argv, form, "extra_prompt", "--extra-prompt")
        _add(argv, form, "facing", "--facing")
        _add(argv, form, "seconds", "--seconds", int)
        _add(argv, form, "resolution", "--resolution")
        if form.get("force"):
            argv.append("--force")
        for clip, raw in sorted((form.get("references") or {}).items()):
            if not raw:
                continue
            if not is_safe_token(clip):
                raise ValueError("reference 的动作名不合法: " + str(clip))
            argv += ["--reference", "%s=%s" % (clip, resolve_inside(paths.root, str(raw)))]
    else:
        _add_clip_flags(argv, form)
        _add(argv, form, "skip_frames", "--skip-frames", int)
        _add_keying_flags(argv, form)
    argv.append("-v")
    return argv, label


def child_env(paths: ProjectPaths) -> Dict[str, str]:
    """The environment a stage process runs in.

    The two folders are pinned to the ones the page is browsing: if the server was
    started with an override, the child must not quietly fall back to the built-in
    defaults and write somewhere else. ``PYTHONUNBUFFERED`` is what makes the log
    arrive while a stage is still running instead of in one lump at the end.
    """
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["OUTPUT_DIR"] = str(paths.resource)
    env["HARNESS_DIR"] = str(paths.harness)
    return env


# --------------------------------------------------------------------------------------
# Thumbnails
# --------------------------------------------------------------------------------------


THUMB_CACHE_MAX = 600
_THUMBS: Dict[Tuple[str, int, int], bytes] = {}
_THUMB_LOCK = threading.Lock()


# A slice costs a full decode of the sheet on its own - 50 megapixels, half a second -
# and playback needs a hundred and nineteen of them in a row. So the decoded sheet is
# kept for the next slice, and the finished PNGs are kept for the next playback.
# One decoded sheet at a time: previewing a second clip drops the first, which is all
# anybody is looking at anyway.
SLICE_CACHE_MAX = 200
SHEET_CACHE_PIXELS = 40_000_000  # ~160 MB as RGBA; past that it is not worth holding

_SLICES: Dict[Tuple[str, int, int, int], bytes] = {}
_SHEET: Optional[Tuple[str, int, Any]] = None
_SLICE_LOCK = threading.Lock()


def _decoded_sheet(path: Path) -> Optional[Any]:
    """The sheet, loaded, remembered for the next slice.

    Returns None for a sheet big enough that holding it would cost more than it saves;
    the caller then simply pays the decode again, which is slower but bounded.
    """
    global _SHEET
    stat = path.stat()
    key = (str(path), int(stat.st_mtime))
    with _SLICE_LOCK:
        if _SHEET is not None and _SHEET[0] == key[0] and _SHEET[1] == key[1]:
            return _SHEET[2]

    from PIL import Image

    with Image.open(path) as handle:
        if handle.width * handle.height > SHEET_CACHE_PIXELS:
            return None
        image = handle.convert("RGBA")
        image.load()
    with _SLICE_LOCK:
        _SHEET = (key[0], key[1], image)
    return image


def slice_sheet(path: Path, index: int, width: int) -> Optional[bytes]:
    """One frame cut out of ``sprite_sheet.png``, as a PNG.

    The strip is one horizontal row of equal-width frames (that is what
    ``make_strip_sheet`` writes), so frame ``i`` is the box
    ``(i * width, 0, (i + 1) * width, height)`` and nothing has to be guessed.

    It is cut here rather than in the page because the file is 40k-70k pixels across:
    no browser will decode an image that size, so "play the sprite sheet" can only work
    by handing the page one slice at a time.

    Returns None when the slice does not exist, which the caller turns into a 404.
    """
    stat = path.stat()
    key = (str(path), int(stat.st_mtime), int(index), int(width))
    with _SLICE_LOCK:
        hit = _SLICES.get(key)
    if hit is not None:
        return hit

    image = _decoded_sheet(path)
    if image is None:
        # Too big to hold on to: decode it, cut the one slice, and let it go.
        from PIL import Image

        with Image.open(path) as handle:
            image = handle.convert("RGBA")
            image.load()

    return _encode_slice(image, index, width, key)


def _encode_slice(image: Any, index: int, width: int,
                  key: Tuple[str, int, int, int]) -> Optional[bytes]:
    """Crop one column out of an already decoded sheet and return it as PNG bytes."""
    left = int(index) * int(width)
    if left >= image.width:
        return None
    right = min(image.width, left + int(width))

    buffer = io.BytesIO()
    image.crop((left, 0, right, image.height)).save(buffer, "PNG")
    body = buffer.getvalue()

    with _SLICE_LOCK:
        if len(_SLICES) > SLICE_CACHE_MAX:
            _SLICES.clear()
        _SLICES[key] = body
    return body


def sheet_geometry(sheet: Path, meta: Dict[str, Any]) -> Optional[Dict[str, int]]:
    """How many equal frames ``sheet`` holds, and how wide one of them is.

    The strip is laid out from the aligned frames, which all share one canvas, so the
    slice width is the canvas width. Measuring the file instead of trusting the
    metadata is what keeps this honest: if the sheet is ever not a whole number of
    canvas widths, no slicing is offered at all rather than one frame drifting into
    the next.
    """
    if not sheet.is_file():
        return None
    try:
        from PIL import Image

        with Image.open(sheet) as image:
            size = (int(image.width), int(image.height))
    except Exception:
        return None

    canvas = meta.get("canvas") or []
    width = int(canvas[0]) if len(canvas) >= 2 and canvas[0] else 0
    if width <= 0:
        frames = int(meta.get("frames") or 0)
        if frames > 1 and size[0] % frames == 0:
            width = size[0] // frames
    if width <= 0 or size[0] < width:
        return None
    count = size[0] // width
    if count <= 1:
        return None
    return {"frames": count, "width": width, "height": size[1], "leftover": size[0] - count * width}


def thumbnail(path: Path, width: int) -> bytes:
    """A shrunk PNG, cached per (file, mtime, width).

    A clip is a hundred and nineteen frames of 512px; sending them full size would make
    the page unusable, and re-shrinking them on every scroll would too.
    """
    stat = path.stat()
    key = (str(path), int(stat.st_mtime), int(width))
    with _THUMB_LOCK:
        hit = _THUMBS.get(key)
    if hit is not None:
        return hit

    from PIL import Image

    with Image.open(path) as image:
        image = image.convert("RGBA")
        height = max(1, int(round(image.height * (float(width) / image.width))))
        image = image.resize((max(1, int(width)), height), Image.LANCZOS)
        buffer = io.BytesIO()
        image.save(buffer, "PNG")
    body = buffer.getvalue()

    with _THUMB_LOCK:
        if len(_THUMBS) > THUMB_CACHE_MAX:
            _THUMBS.clear()
        _THUMBS[key] = body
    return body


# --------------------------------------------------------------------------------------
# The HTTP server
# --------------------------------------------------------------------------------------


PATHS: ProjectPaths = load_paths()
RUNNER = Runner()


class Handler(BaseHTTPRequestHandler):
    server_version = "ArtPipelineUI"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        """Stay quiet: the page is the log, the console belongs to the pipeline."""

    # -- plumbing ------------------------------------------------------------------

    def _json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, exc: Exception, status: int = 400) -> None:
        self._json({"error": str(exc)}, status)

    def _query(self) -> Dict[str, List[str]]:
        return urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

    def _body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _scan(self) -> int:
        try:
            return int((self._query().get("scan") or ["0"])[0])
        except ValueError:
            return 0

    # -- routing -------------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - the name is fixed by BaseHTTPRequestHandler
        route = urllib.parse.urlparse(self.path).path
        try:
            if route == "/api/state":
                return self._api_state()
            if route == "/api/runs":
                query = self._query()
                return self._json(scan_runs(
                    PATHS,
                    (query.get("root") or [IMAGE_DIR_NAME])[0],
                    (query.get("q") or [""])[0],
                ))
            if route == "/api/run":
                rel = urllib.parse.unquote((self._query().get("path") or [""])[0])
                return self._json(describe_run(PATHS, rel))
            if route == "/api/kept":
                rel = urllib.parse.unquote((self._query().get("path") or [""])[0])
                return self._json(describe_kept(PATHS, rel))
            if route == "/api/folder":
                rel = urllib.parse.unquote((self._query().get("path") or [""])[0])
                return self._json(describe_folder(PATHS, rel))
            if route == "/api/cut":
                rel = urllib.parse.unquote((self._query().get("path") or [""])[0])
                return self._json(describe_cut(PATHS, rel))
            if route in ("/api/job", "/api/job/log"):
                job = RUNNER.current()
                return self._json({"job": job.snapshot(self._scan()) if job else None})
            if route == "/api/file":
                return self._api_file()
            if route == "/api/slice":
                return self._api_slice()
            if route in ("/", ""):
                return self._static("index.html")
            return self._static(route.lstrip("/"))
        except ValueError as exc:
            self._error(exc, 404)
        except Exception as exc:  # a broken request must not take the server down
            sys.stderr.write("ui: %s: %s\n" % (route, exc))
            self._error(exc, 500)

    def do_POST(self) -> None:  # noqa: N802
        route = urllib.parse.urlparse(self.path).path
        try:
            if route == "/api/job/start":
                return self._api_start()
            if route == "/api/job/stop":
                return self._json({"stopped": RUNNER.stop()})
            if route == "/api/command":
                return self._api_command()
            if route == "/api/reveal":
                return self._api_reveal()
            if route == "/api/promote":
                return self._api_promote()
            if route == "/api/normalize":
                return self._api_normalize()
            return self._error(ValueError("unknown endpoint: " + route), 404)
        except ValueError as exc:
            self._error(exc, 400)
        except Exception as exc:
            sys.stderr.write("ui: %s: %s\n" % (route, exc))
            self._error(exc, 500)

    # -- handlers ------------------------------------------------------------------

    def _api_state(self) -> None:
        from src.config import load_settings
        from src.harness import load_specs, load_styles
        from src.paths import describe_env

        settings = load_settings(
            PATHS.env_file, harness_dir=PATHS.harness, output_dir=PATHS.resource
        )
        defaults = video_defaults(settings.common_path)

        specs: List[Dict[str, Any]] = []
        try:
            for spec in load_specs(settings.characters_dir).values():
                specs.append({
                    "id": getattr(spec, "id", ""),
                    "title": getattr(spec, "title", ""),
                    "name": getattr(spec, "name", ""),
                    "category": getattr(spec, "category", ""),
                    "style": getattr(spec, "style", ""),
                    "count": getattr(spec, "count", None),
                    "references": [str(r) for r in (getattr(spec, "references", None) or [])],
                    "reference_mode": getattr(spec, "reference_mode", ""),
                })
        except Exception as exc:
            sys.stderr.write("ui: could not read the specs: %s\n" % exc)

        styles: List[str] = []
        try:
            loaded = load_styles(settings.styles_dir)
            styles = sorted(loaded.keys()) if isinstance(loaded, dict) else sorted(loaded)
        except Exception as exc:
            sys.stderr.write("ui: could not read the styles: %s\n" % exc)

        # The prompt presets: small files in hareness/prompts, ``common`` plus the ones
        # that are specific to a clip. The page shows them as a list to tick on stage 3.
        presets: List[Dict[str, Any]] = []
        preset_dir = ""
        preset_dir_abs = ""
        try:
            from src import prompts as prompt_presets

            folder = prompt_presets.preset_dir(PATHS.harness)
            presets = [item.describe() for item in prompt_presets.list_presets(folder)]
            preset_dir = relative_to_root(folder, PATHS.root)
            preset_dir_abs = str(folder)
        except Exception as exc:
            sys.stderr.write("ui: could not read the prompt presets: %s\n" % exc)

        # The stills that are kept: what stage 3 is allowed to animate. Sent up front so
        # the stage-3 form can offer them as pictures instead of asking for a path.
        artwork: List[Dict[str, Any]] = []
        try:
            from src import library

            artwork = library.list_origin(PATHS)["image"]["items"]
        except Exception as exc:
            sys.stderr.write("ui: could not read origin/image: %s\n" % exc)

        job = RUNNER.current()
        self._json({
            # Relative on purpose: the top bar is a thing people screenshot. The absolute
            # paths ride along in ``project_abs`` for the tooltips only.
            "project": PATHS.describe_relative(),
            "project_abs": PATHS.describe(),
            "env_overrides": describe_env(),
            "defaults": {
                "sizes": list(DEFAULT_SIZES),
                "fill": defaults.get("fill", DEFAULT_FILL),
                "clips": defaults.get("clips", list(DEFAULT_CLIPS)),
                "models": defaults.get("models", []),
                "facing": defaults.get("facing", "left"),
                "seconds": defaults.get("seconds", 5),
                "resolution": defaults.get("resolution", "720p"),
                "skip_frames": defaults.get("skip_frames", 2),
                "tolerance": defaults.get("tolerance"),
                "flatten_tolerance": defaults.get("flatten_tolerance"),
                "key_color": default_key_color_of(settings.common_path),
            },
            "specs": specs,
            "styles": styles,
            "presets": presets,
            "prompt_dir": preset_dir,
            "prompt_dir_abs": preset_dir_abs,
            "artwork": artwork,
            "stages": [dict(stage) for stage in STAGES],
            "job": job.snapshot() if job else None,
            "busy": RUNNER.busy(),
        })

    def _api_start(self) -> None:
        form = self._body()
        argv, label = build_command(stage_of(form), form, PATHS)
        job = RUNNER.start(argv, label, PATHS.root, child_env(PATHS))
        self._json({"job": job.snapshot()}, 202)

    def _api_command(self) -> None:
        """The form -> the command line, so the page can show it before anything runs.

        The preview and the real thing go through the same function, so the line on
        screen is the line that will run, and a form that cannot be turned into a
        command says why instead of failing later.
        """
        form = self._body()
        try:
            argv, label = build_command(stage_of(form), form, PATHS)
        except ValueError as exc:
            return self._json({"label": None, "argv": None, "command": None, "error": str(exc)})
        return self._json({
            "label": label,
            "argv": argv,
            "command": " ".join(argv[2:]),
            "error": None,
        })

    def _api_reveal(self) -> None:
        """Open a folder in the desktop file manager.

        A clip is 119 frames; being able to look at them in Explorer matters more than
        being able to look at a dozen of them in a web page.
        """
        rel = str(self._body().get("path") or "").strip()
        target = _inside_root(PATHS, rel, PATHS.root)
        if not target.exists():
            raise ValueError("找不到: " + rel)
        folder = target if target.is_dir() else target.parent
        opener = getattr(os, "startfile", None)
        if opener is not None:
            opener(str(folder))
        else:
            subprocess.Popen(["xdg-open", str(folder)])
        self._json({"opened": relative_to_root(folder, PATHS.root)})

    def _api_promote(self) -> None:
        """Copy something out of the work tree and into origin.

        The one write the page can make outside resource. Everything the pipeline draws
        is disposable, and this is how a person says which of it is not - and it is the
        switch between the two halves of the pipeline, because stage 3 animates what is
        in origin/image.

        ``name`` is the whole of the naming contract with the page: the entity and the
        clip are read out of it here, so a caller that has only a name - the command
        line, a script - gets the same result as the page, which sends both halves.
        """
        from src import library

        body = self._body()
        rel = str(body.get("path") or "").strip()
        target = _inside_root(PATHS, rel, PATHS.resource)
        try:
            result = library.promote(
                PATHS,
                target,
                body.get("name") or None,
                entity=body.get("entity") or None,
                clip=body.get("clip") or None,
                overwrite=bool(body.get("force")),
            )
        except library.PromoteConflict as conflict:
            where = relative_to_root(conflict.target, PATHS.root)
            return self._json({
                "error": where + " 已经有了",
                "conflict": True,
                "target": where,
            }, 409)
        self._json({"kept": result})

    def _api_normalize(self) -> None:
        """Put a cut that is already in ``origin`` into the canonical shape, in place.

        The answer to "I copied a folder of frames in from somewhere else": the frames
        are renumbered ``<entity>_<clip>_001.png``, the 512 / 256 copies and the two
        sheets are built beside them, and ``meta.json`` is written - all in place,
        because there is nowhere else to put it.

        Reading never needed any of this: the folder is listed, played and imported the
        moment it lands. This is only about making it tidy, and it is deliberately the
        same code that promoting runs, so a cut that arrived by hand and a cut that came
        out of the pipeline end up indistinguishable.
        """
        from src import library

        body = self._body()
        rel = str(body.get("path") or "").strip()
        target = _inside_root(PATHS, rel, PATHS.origin)
        try:
            result = library.normalize(
                PATHS,
                target,
                name=body.get("name") or None,
                entity=body.get("entity") or None,
                clip=body.get("clip") or None,
                overwrite=bool(body.get("force")),
            )
        except library.PromoteConflict as conflict:
            where = relative_to_root(conflict.target, PATHS.root)
            return self._json({
                "error": where + " 已经有了",
                "conflict": True,
                "target": where,
            }, 409)
        self._json({"normalized": result})

    def _api_file(self) -> None:
        query = self._query()
        rel = urllib.parse.unquote((query.get("path") or [""])[0])
        if not rel:
            raise ValueError("path is required")
        target = resolve_inside(PATHS.root, rel)
        if not target.is_file():
            raise ValueError("不是文件: " + rel)
        # The page puts the file's mtime in the URL, so an unchanged file can be cached
        # by the browser and a redrawn one gets a new URL. Without that stamp a stale
        # frame is worse than a re-read: playback would keep showing the old render.
        versioned = bool((query.get("v") or [""])[0])
        try:
            width = int((query.get("w") or ["0"])[0] or 0)
        except ValueError:
            width = 0
        if width and target.suffix.lower() in IMAGE_SUFFIXES:
            body = thumbnail(target, max(16, min(width, 2048)))
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", _cache_header(versioned))
            self.end_headers()
            self.wfile.write(body)
            return
        ctype = mime_of(target)
        if target.suffix.lower() in TEXT_SUFFIXES and not ctype.startswith("text/"):
            ctype = "text/plain"
        if ctype.startswith("text/"):
            ctype += "; charset=utf-8"
        if versioned and ctype.startswith("image/"):
            body = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", _cache_header(True))
            self.end_headers()
            self.wfile.write(body)
            return
        self._send_file(target, ctype)

    def _api_slice(self) -> None:
        """One frame out of a sprite sheet. See :func:`slice_sheet`."""
        query = self._query()
        rel = urllib.parse.unquote((query.get("path") or [""])[0])
        if not rel:
            raise ValueError("path is required")
        target = resolve_inside(PATHS.root, rel)
        if not target.is_file() or target.suffix.lower() != ".png":
            raise ValueError("不是 PNG: " + rel)
        try:
            index = int((query.get("i") or ["0"])[0])
            width = int((query.get("w") or ["0"])[0])
        except ValueError:
            raise ValueError("i 和 w 都要是整数")
        if width <= 0 or index < 0:
            raise ValueError("w 要大于 0，i 不能是负数")
        body = slice_sheet(target, index, width)
        if body is None:
            raise ValueError("第 %d 片不在 %s 里" % (index, rel))
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(body)))
        # A playback asks for the same 119 slices over and over; caching them is the
        # difference between a smooth loop and a request per frame. The URL carries the
        # sheet's mtime, so a re-cut sheet is a different URL and never serves stale.
        self.send_header("Cache-Control", _cache_header(True))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, target: Path, ctype: str) -> None:
        """Send a file, honouring ``Range`` so the browser can seek inside a clip."""
        size = target.stat().st_size
        start, end, status = 0, max(0, size - 1), 200
        match = re.match(r"bytes=(\d*)-(\d*)$", (self.headers.get("Range") or "").strip())
        if match and (match.group(1) or match.group(2)):
            if match.group(1):
                start = int(match.group(1))
                end = int(match.group(2)) if match.group(2) else size - 1
            else:
                start = max(0, size - int(match.group(2)))
            end = min(end, size - 1)
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", "bytes */%d" % size)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            status = 206
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()
        with target.open("rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining > 0:
                chunk = handle.read(min(262144, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def _static(self, rel: str) -> None:
        root = STATIC_DIR.resolve()
        target = (root / rel).resolve()
        if target != root and root not in target.parents:
            raise ValueError("bad path: " + rel)
        if not target.is_file():
            raise ValueError("not found: " + rel)
        ctype = mime_of(target)
        if ctype.startswith("text/") or ctype == "application/json":
            ctype += "; charset=utf-8"
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def default_key_color_of(common_path: Path) -> str:
    """The project's backdrop colour, read the same way the keyer reads it."""
    try:
        from src.stages import default_key_color
        return default_key_color(common_path)
    except Exception:
        from src.chroma import DEFAULT_KEY_COLOR
        return DEFAULT_KEY_COLOR


class Server(ThreadingHTTPServer):
    """The one thing a local server has to get right: shutting up.

    A browser that closes a keep-alive connection mid-request raises inside the
    request thread, and the default handler prints a full traceback for it. That is
    not a problem with the pipeline, and a console full of them hides the log lines
    that are.
    """

    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        error = sys.exc_info()[1]
        if isinstance(error, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


def port_in_use(host: str, port: int) -> bool:
    """Is something already answering on this port?

    ``allow_reuse_address`` is on so that a restart is not blocked by a socket in
    TIME_WAIT, and on Windows that has a sharp edge: a *second* server can bind the
    port happily, and the first one keeps taking the connections. The console that
    would then be showing the old code while the new one sits there is worth refusing
    outright, so the port is probed before the socket is opened.
    """
    import socket

    probe = socket.socket()
    probe.settimeout(1.0)
    try:
        probe.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def serve(
    paths: Optional[ProjectPaths] = None,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    open_browser: bool = True,
) -> None:
    """Start the console. Blocks until Ctrl+C."""
    global PATHS
    PATHS = paths or load_paths()
    if not (PATHS.root / "main.py").is_file():
        raise SystemExit("找不到 main.py，项目根目录不对: " + str(PATHS.root))

    probe_host = "127.0.0.1" if host in ("0.0.0.0", "") else host
    if port_in_use(probe_host, port):
        raise SystemExit(
            "端口 %d 上已经有东西在应答了 —— 多半是上一次的 ui 还开着。\n"
            "先把它关掉，或者换一个端口：python main.py ui --port %d"
            % (port, port + 1)
        )

    httpd = Server((host, port), Handler)
    address = "127.0.0.1" if host in ("0.0.0.0", "") else host
    url = "http://%s:%d/" % (address, port)
    print("")
    print("  美术流水线控制台")
    print("  项目根  : " + str(PATHS.root))
    print("  resource: " + str(PATHS.resource))
    print("  地址    : " + url)
    print("  停止    : Ctrl+C")
    print("")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  已停止")
    finally:
        httpd.server_close()
