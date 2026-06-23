"""Edge suspicious-clip recorder — pre/post-roll DVR (option A, req #3/#4/#5).

Each camera is continuously written to a rolling ring of ~1s segments on disk
(bounded — old segments pruned), so when the behaviour engine flags a suspicious
EPISODE we can cut a clip that starts `pre` seconds BEFORE the action began and
ends `post` seconds AFTER it ended (the founder's −3s … +3s requirement).

8GB store PC constraints drove the design:
  * frames are NEVER held in RAM — ffmpeg writes time-named ``.ts`` segments with
    ``-c copy`` (negligible CPU); a rolling-window prune keeps only ``keep_sec``.
  * on an episode the covering segments are concatenated (``-c copy``) into one
    mp4; a bounded JSON index lists clips + metadata and enforces retention
    (max count / max age), deleting old clip files.

ffmpeg comes from the bundled binary (``resolve_ffmpeg_exe``).
"""

from __future__ import annotations

import contextlib
import json
import queue
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from sentry_agent_pc.logging_setup import get_logger
from sentry_agent_pc.resources import resolve_ffmpeg_exe

log = get_logger("sentry_agent_pc.edge.recorder")

_SEG_PREFIX = "seg_"
_SEG_FMT = "%Y%m%d_%H%M%S"  # strftime — 1s granularity
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
# No suspicious episode realistically runs longer than this. A protect floor
# older than keep_sec + this is treated as a LEAK (pipeline died mid-episode
# without clearing it) and self-expires, so prune can't be pinned forever →
# unbounded disk growth on an 8GB store PC.
_MAX_EPISODE_SEC = 300.0


@dataclass(frozen=True, slots=True)
class Segment:
    path: Path
    start_ts: float
    duration: float = 1.0

    @property
    def end_ts(self) -> float:
        return self.start_ts + self.duration


@dataclass(slots=True)
class SuspiciousEpisode:
    """Emitted by the behaviour layer when a suspicious episode opens then closes."""

    camera_id: str
    start_ts: float  # wall-clock when the action began
    end_ts: float  # wall-clock when it fully ended
    risk_pct: float
    behaviors: list[str] = field(default_factory=list)
    # Per-movement score breakdown banked this episode: [{key, offset_sec, score}].
    # Mirrors the cloud's triggered_behavior_detail so the same per-fire breakdown
    # surfaces for edge-flagged clips.
    behavior_detail: list[dict[str, Any]] = field(default_factory=list)
    # Per-FIRE timeline: [{key, ts, offset_sec, amount, risk}] in chronological
    # order — every individual banking, for the clip detail view (one row per +N).
    events: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class ClipRecord:
    clip_id: str
    camera_id: str
    path: str
    started_at: float  # episode start − pre
    ended_at: float  # episode end + post
    risk_pct: float
    behaviors: list[str]
    created_at: float
    behavior_detail: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return max(0.0, self.ended_at - self.started_at)


# ── pure helpers (unit-tested) ──────────────────────────────────────────────


def parse_segment_name(path: Path) -> float | None:
    """Wall-clock start encoded in a segment filename, or None if it doesn't match."""
    name = path.stem
    if not name.startswith(_SEG_PREFIX):
        return None
    try:
        return datetime.strptime(name[len(_SEG_PREFIX) :], _SEG_FMT).timestamp()
    except ValueError:
        return None


def list_segments(seg_dir: Path, *, duration: float = 1.0) -> list[Segment]:
    out: list[Segment] = []
    if not seg_dir.is_dir():
        return out
    for p in seg_dir.glob(f"{_SEG_PREFIX}*"):
        ts = parse_segment_name(p)
        if ts is not None:
            out.append(Segment(p, ts, duration))
    out.sort(key=lambda s: s.start_ts)
    return out


def select_segments(segments: Sequence[Segment], t0: float, t1: float) -> list[Segment]:
    """Segments overlapping the half-open window [t0, t1], time-ordered."""
    hits = [s for s in segments if s.end_ts > t0 and s.start_ts < t1]
    return sorted(hits, key=lambda s: s.start_ts)


def build_segment_cmd(
    ffmpeg: str, src_url: str, seg_dir: Path, *, segment_sec: float = 1.0
) -> list[str]:
    """ffmpeg argv: pull `src_url`, write a rolling ring of time-named segments."""
    pattern = str(seg_dir / f"{_SEG_PREFIX}%Y%m%d_%H%M%S.ts")
    return [
        ffmpeg, "-nostdin", "-rtsp_transport", "tcp", "-i", src_url,
        "-map", "0:v:0", "-an", "-c:v", "copy",
        "-f", "segment", "-segment_time", str(segment_sec),
        "-strftime", "1", "-reset_timestamps", "1",
        pattern,
    ]  # fmt: skip


def build_concat_cmd(ffmpeg: str, list_file: Path, out_path: Path) -> list[str]:
    """ffmpeg argv: concat the segments listed in `list_file` (no re-encode)."""
    return [
        ffmpeg, "-nostdin", "-f", "concat", "-safe", "0",
        "-i", str(list_file), "-c", "copy", "-y", str(out_path),
    ]  # fmt: skip


def _record_from_dict(d: dict[str, Any]) -> ClipRecord:
    return ClipRecord(
        clip_id=str(d["clip_id"]),
        camera_id=str(d["camera_id"]),
        path=str(d["path"]),
        started_at=float(d["started_at"]),
        ended_at=float(d["ended_at"]),
        risk_pct=float(d["risk_pct"]),
        behaviors=[str(b) for b in d.get("behaviors", [])],
        created_at=float(d["created_at"]),
        behavior_detail=list(d.get("behavior_detail", [])),
        events=list(d.get("events", [])),
    )


# ── building blocks ─────────────────────────────────────────────────────────


def build_clip(
    seg_dir: Path,
    episode: SuspiciousEpisode,
    clips_dir: Path,
    *,
    pre: float = 3.0,
    post: float = 3.0,
    segment_sec: float = 1.0,
    clip_id: str | None = None,
    ffmpeg: str | None = None,
) -> ClipRecord | None:
    """Concat the segments covering [start−pre, end+post] into one mp4. None on miss."""
    t0 = episode.start_ts - pre
    t1 = episode.end_ts + post
    segs = select_segments(list_segments(seg_dir, duration=segment_sec), t0, t1)
    if not segs:
        log.warning("clip.no_segments", camera_id=episode.camera_id)
        return None
    clips_dir.mkdir(parents=True, exist_ok=True)
    cid = clip_id or f"{episode.camera_id}_{int(t0)}"
    out_path = clips_dir / f"{cid}.mp4"
    list_file = clips_dir / f"{cid}.txt"
    list_file.write_text(
        "".join(f"file '{s.path.as_posix()}'\n" for s in segs), encoding="utf-8"
    )
    cmd = build_concat_cmd(ffmpeg or resolve_ffmpeg_exe(), list_file, out_path)
    try:
        subprocess.run(  # noqa: S603
            cmd, stdin=subprocess.DEVNULL, capture_output=True,
            timeout=30, check=False, creationflags=_CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("clip.ffmpeg_error", camera_id=episode.camera_id, error=str(e))
    finally:
        with contextlib.suppress(OSError):
            list_file.unlink()
    if not out_path.exists() or out_path.stat().st_size == 0:
        log.warning("clip.build_failed", camera_id=episode.camera_id)
        return None
    return ClipRecord(
        clip_id=cid, camera_id=episode.camera_id, path=str(out_path),
        started_at=t0, ended_at=t1, risk_pct=episode.risk_pct,
        behaviors=list(episode.behaviors), created_at=time.time(),
        behavior_detail=list(episode.behavior_detail),
        events=list(episode.events),
    )


class ClipStore:
    """Bounded JSON-backed list of suspicious clips. Enforces retention on add."""

    def __init__(
        self, index_path: Path, *, max_clips: int = 50, max_age_sec: float = 7 * 24 * 3600
    ) -> None:
        self.index_path = Path(index_path)
        self.max_clips = max_clips
        self.max_age_sec = max_age_sec
        self._lock = threading.Lock()

    def records(self) -> list[ClipRecord]:
        if not self.index_path.exists():
            return []
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return [_record_from_dict(d) for d in data]

    def add(self, rec: ClipRecord, *, now: float | None = None) -> None:
        with self._lock:
            recs = [*self.records(), rec]
            self._save(self._prune(recs, now if now is not None else time.time()))

    def _prune(self, recs: list[ClipRecord], now: float) -> list[ClipRecord]:
        fresh = [r for r in recs if now - r.created_at <= self.max_age_sec]
        aged_out = [r for r in recs if now - r.created_at > self.max_age_sec]
        fresh.sort(key=lambda r: r.created_at, reverse=True)
        survivors, over_cap = fresh[: self.max_clips], fresh[self.max_clips :]
        for r in (*aged_out, *over_cap):
            with contextlib.suppress(OSError):
                Path(r.path).unlink()
        return survivors

    def _save(self, recs: list[ClipRecord]) -> None:
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        self.index_path.write_text(
            json.dumps([asdict(r) for r in recs], ensure_ascii=False), encoding="utf-8"
        )


class SegmentRecorder:
    """One ffmpeg writing a rolling segment ring for a camera; prunes old segments."""

    def __init__(
        self,
        camera_id: str,
        src_url: str,
        seg_dir: Path,
        *,
        segment_sec: float = 1.0,
        keep_sec: float = 45.0,
        max_episode_sec: float = _MAX_EPISODE_SEC,
    ) -> None:
        self.camera_id = camera_id
        self.src_url = src_url
        self.seg_dir = Path(seg_dir)
        self.segment_sec = segment_sec
        self.keep_sec = keep_sec
        self.max_episode_sec = max_episode_sec
        self._proc: subprocess.Popen[bytes] | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # Watermark: never prune segments newer than this (an open episode's
        # start − pre). None = no open episode → normal keep_sec window only.
        self._protect_floor: float | None = None

    def set_protect_floor(self, floor_ts: float | None) -> None:
        """Protect segments at/after `floor_ts` from the prune. Plain attribute
        write — atomic under the GIL — called from the pipeline thread."""
        self._protect_floor = floor_ts

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return  # already running — don't spawn a second ffmpeg ring
        self.seg_dir.mkdir(parents=True, exist_ok=True)
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name=f"seg-{self.camera_id}", daemon=True
        )
        self._thread.start()

    def _run(self) -> None:
        cmd = build_segment_cmd(
            resolve_ffmpeg_exe(), self.src_url, self.seg_dir, segment_sec=self.segment_sec
        )
        try:
            self._proc = subprocess.Popen(  # noqa: S603
                cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, creationflags=_CREATE_NO_WINDOW,
            )
        except OSError as e:
            log.error("recorder.spawn_failed", camera_id=self.camera_id, error=str(e))
            return
        while not self._stop.is_set():
            self.prune()
            self._stop.wait(self.segment_sec)
        self._reap()

    def prune(self, now: float | None = None) -> int:
        """Delete segments older than the keep window — but NEVER newer than the
        open-episode protect floor, so a long episode keeps its pre-roll until the
        clip is cut. Returns count removed."""
        ref = now if now is not None else time.time()
        cutoff = ref - self.keep_sec
        floor = self._protect_floor
        # Honour the pre-roll pin ONLY while it's plausibly a live episode. A
        # floor older than keep_sec + max_episode means the episode that set it
        # never closed (pipeline died) — self-expire so prune isn't pinned
        # forever and disk stays bounded.
        if floor is not None and floor >= ref - (self.keep_sec + self.max_episode_sec):
            cutoff = min(cutoff, floor)
        removed = 0
        for s in list_segments(self.seg_dir, duration=self.segment_sec):
            if s.end_ts <= cutoff:
                with contextlib.suppress(OSError):
                    s.path.unlink()
                    removed += 1
        return removed

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        self._reap()

    def _reap(self) -> None:
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        with contextlib.suppress(OSError):
            proc.terminate()
        try:
            proc.wait(timeout=3.0)
        except Exception:  # noqa: BLE001 — terminate ignored → force-kill
            with contextlib.suppress(OSError):
                proc.kill()


class EdgeClipRecorder:
    """Per-camera facade: rolling segment ring + on-episode clip extraction → store.

    ``on_clip`` (set by the runtime) fires for each saved clip — that's where the
    server upload / VLM handoff hooks in, decoupled from recording."""

    def __init__(
        self,
        camera_id: str,
        src_url: str,
        base_dir: Path,
        store: ClipStore,
        *,
        pre: float = 3.0,
        post: float = 3.0,
        segment_sec: float = 1.0,
        keep_sec: float | None = None,
        on_clip: Callable[[ClipRecord], None] | None = None,
    ) -> None:
        self.camera_id = camera_id
        self.pre = pre
        self.post = post
        self.segment_sec = segment_sec
        self.store = store
        self.on_clip = on_clip
        base = Path(base_dir)
        self.seg_dir = base / "segments" / camera_id
        self.clips_dir = base / "clips"
        keep = keep_sec if keep_sec is not None else max(45.0, pre + post + 30.0)
        self._recorder = SegmentRecorder(
            camera_id, src_url, self.seg_dir, segment_sec=segment_sec, keep_sec=keep
        )
        # Episodes are CUT off the decode/process() thread: submit() just queues,
        # a worker thread runs the (blocking) ffmpeg concat + store + on_clip
        # upload. Bounded + drop-oldest so a slow handoff can't stall live view
        # or grow memory on an 8GB store PC.
        self._queue: queue.Queue[SuspiciousEpisode | None] = queue.Queue(maxsize=16)
        self._worker: threading.Thread | None = None
        # Set in stop(): makes submit() a no-op so its drop-oldest eviction can
        # never discard the shutdown sentinel from a full queue (which would
        # hang the join).
        self._stopping = threading.Event()

    def set_protect_floor(self, oldest_open_start: float | None) -> None:
        """Pin pre-roll: protect segments from (oldest open episode start − pre)."""
        floor = None if oldest_open_start is None else oldest_open_start - self.pre
        self._recorder.set_protect_floor(floor)

    def submit(self, episode: SuspiciousEpisode) -> None:
        """Queue a closed episode for off-thread clip extraction (non-blocking)."""
        if self._stopping.is_set():
            return  # shutting down — don't risk evicting the sentinel
        try:
            self._queue.put_nowait(episode)
        except queue.Full:
            with contextlib.suppress(queue.Empty):
                self._queue.get_nowait()  # drop the oldest pending
            with contextlib.suppress(queue.Full):
                self._queue.put_nowait(episode)
            log.warning("clip.queue_full_dropped_oldest", camera_id=self.camera_id)

    def _worker_loop(self) -> None:
        while True:
            ep = self._queue.get()
            if ep is None:  # sentinel
                return
            try:
                self.on_episode(ep)
            except Exception:  # noqa: BLE001 — never let one bad clip kill the worker
                log.exception("clip.worker_failed", camera_id=self.camera_id)

    def start(self) -> None:
        self._recorder.start()
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(
                target=self._worker_loop, name=f"clip-{self.camera_id}", daemon=True
            )
            self._worker.start()

    def stop(self) -> None:
        self._stopping.set()  # block further submits before queuing the sentinel
        self._recorder.stop()
        if self._worker is not None:
            self._queue.put(None)  # sentinel
            self._worker.join(timeout=5.0)
            self._worker = None

    def on_episode(self, episode: SuspiciousEpisode) -> ClipRecord | None:
        """Cut + store the [start−pre, end+post] clip for a suspicious episode."""
        rec = build_clip(
            self.seg_dir, episode, self.clips_dir,
            pre=self.pre, post=self.post, segment_sec=self.segment_sec,
        )
        if rec is not None:
            self.store.add(rec)
            log.info(
                "clip.saved", camera_id=self.camera_id, clip_id=rec.clip_id,
                risk=round(rec.risk_pct), dur=round(rec.duration, 1),
            )
            if self.on_clip is not None:
                try:
                    self.on_clip(rec)
                except Exception:  # noqa: BLE001 — a failed handoff must not break recording
                    log.exception("clip.on_clip_failed", camera_id=self.camera_id)
        return rec
