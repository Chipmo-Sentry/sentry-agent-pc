"""Edge clip-recorder tests — segment selection, clip index retention, prune.

ffmpeg-free: exercises the pure logic + filesystem bookkeeping with fake segment
files. The live ffmpeg segment→concat path is verified separately (manual run).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from sentry_agent_pc.edge.recorder import (
    ClipRecord,
    ClipStore,
    Segment,
    SegmentRecorder,
    SuspiciousEpisode,
    build_concat_cmd,
    build_segment_cmd,
    list_segments,
    parse_segment_name,
    select_segments,
)

_SEG_FMT = "%Y%m%d_%H%M%S"


def _seg_name(ts: float) -> str:
    return "seg_" + datetime.fromtimestamp(ts).strftime(_SEG_FMT) + ".ts"


def _touch_segments(seg_dir: Path, start_ts: int, count: int) -> None:
    seg_dir.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        (seg_dir / _seg_name(start_ts + i)).write_bytes(b"x")


def test_parse_and_list_segments(tmp_path: Path) -> None:
    base = int(datetime(2026, 6, 19, 14, 0, 0).timestamp())
    _touch_segments(tmp_path, base, 5)
    (tmp_path / "not_a_segment.txt").write_text("ignore")
    segs = list_segments(tmp_path)
    assert len(segs) == 5
    assert [int(s.start_ts) for s in segs] == [base + i for i in range(5)]
    assert parse_segment_name(tmp_path / "random.ts") is None


def test_select_segments_overlap() -> None:
    segs = [Segment(Path(f"{i}.ts"), float(i), 1.0) for i in range(10)]  # [0,1)…[9,10)
    # window [3.5, 6.5] overlaps segments starting 3,4,5,6
    hit = select_segments(segs, 3.5, 6.5)
    assert [int(s.start_ts) for s in hit] == [3, 4, 5, 6]
    assert select_segments(segs, 100.0, 200.0) == []


def test_build_cmds_shape() -> None:
    seg = build_segment_cmd("ffmpeg", "rtsp://x/y", Path("/tmp/seg"), segment_sec=1.0)
    assert seg[0] == "ffmpeg"
    assert "-c:v" in seg and "copy" in seg and "segment" in seg
    assert "-an" in seg  # audio dropped
    concat = build_concat_cmd("ffmpeg", Path("/tmp/list.txt"), Path("/tmp/out.mp4"))
    assert concat[:5] == ["ffmpeg", "-nostdin", "-f", "concat", "-safe"]
    assert concat[-1] == str(Path("/tmp/out.mp4"))


def _clip(tmp_path: Path, cid: str, created_at: float) -> ClipRecord:
    p = tmp_path / f"{cid}.mp4"
    p.write_bytes(b"clip")
    return ClipRecord(
        clip_id=cid,
        camera_id="cam01",
        path=str(p),
        started_at=created_at - 6,
        ended_at=created_at,
        risk_pct=80.0,
        behaviors=["conceal"],
        created_at=created_at,
    )


def test_clipstore_roundtrip_and_count_retention(tmp_path: Path) -> None:
    store = ClipStore(tmp_path / "index.json", max_clips=3)
    now = 1_000_000.0
    for i in range(5):
        store.add(_clip(tmp_path, f"c{i}", now + i), now=now + i)
    recs = store.records()
    assert len(recs) == 3  # capped
    # newest survive; oldest two files deleted
    ids = {r.clip_id for r in recs}
    assert ids == {"c2", "c3", "c4"}
    assert not (tmp_path / "c0.mp4").exists()
    assert not (tmp_path / "c1.mp4").exists()
    assert (tmp_path / "c4.mp4").exists()


def test_clipstore_set_status(tmp_path: Path) -> None:
    # Operator triage: set_status persists a clip's status, survives a reload, and
    # never deletes the clip file. A default record is "open".
    store = ClipStore(tmp_path / "index.json")
    now = 1_000_000.0
    store.add(_clip(tmp_path, "c0", now), now=now)
    assert store.records()[0].status == "open"

    assert store.set_status("c0", "confirmed") is True
    # Reload from a fresh store instance to prove it round-trips through JSON.
    assert ClipStore(tmp_path / "index.json").records()[0].status == "confirmed"
    assert (tmp_path / "c0.mp4").exists()  # status change never deletes the file

    # Unknown clip id → no-op, returns False.
    assert store.set_status("nope", "dismissed") is False


def test_clipstore_age_retention(tmp_path: Path) -> None:
    store = ClipStore(tmp_path / "index.json", max_clips=100, max_age_sec=10.0)
    now = 2_000_000.0
    old = _clip(tmp_path, "old", now - 100)  # older than max_age
    fresh = _clip(tmp_path, "fresh", now - 1)
    store.add(old, now=now - 100)
    store.add(fresh, now=now)
    recs = store.records()
    assert [r.clip_id for r in recs] == ["fresh"]
    assert not (tmp_path / "old.mp4").exists()


def test_segment_recorder_prune_by_age(tmp_path: Path) -> None:
    rec = SegmentRecorder("cam01", "rtsp://x", tmp_path, segment_sec=1.0, keep_sec=10.0)
    now = int(datetime(2026, 6, 19, 14, 0, 0).timestamp())
    _touch_segments(tmp_path, now - 30, 5)  # ~30s old → pruned
    _touch_segments(tmp_path, now - 3, 4)  # recent → kept
    removed = rec.prune(now=float(now))
    assert removed == 5
    assert len(list_segments(tmp_path)) == 4


def test_build_clip_no_segments_returns_none(tmp_path: Path) -> None:
    from sentry_agent_pc.edge.recorder import build_clip

    ep = SuspiciousEpisode("cam01", start_ts=1000.0, end_ts=1002.0, risk_pct=90.0)
    assert build_clip(tmp_path / "empty", ep, tmp_path / "clips") is None


def test_prune_respects_protect_floor(tmp_path: Path) -> None:
    """An open episode's protect floor must keep its pre-roll segments alive even
    though they're far older than keep_sec (the long-episode bug)."""
    rec = SegmentRecorder("cam01", "rtsp://x", tmp_path, segment_sec=1.0, keep_sec=10.0)
    now = int(datetime(2026, 6, 19, 14, 0, 0).timestamp())
    _touch_segments(tmp_path, now - 100, 50)  # all far older than keep_sec
    rec.set_protect_floor(float(now - 100))  # episode opened ~100s ago → protect
    assert rec.prune(now=float(now)) == 0  # nothing pruned while protected
    rec.set_protect_floor(None)  # episode closed → protection lifted
    assert rec.prune(now=float(now)) == 50  # now all aged-out segments go


def test_protect_floor_self_expires_when_stale(tmp_path: Path) -> None:
    """A leaked protect floor (pipeline died mid-episode without clearing it)
    must NOT pin segments forever — once it's older than keep_sec + max_episode
    the prune ignores it and disk stays bounded."""
    rec = SegmentRecorder(
        "cam01", "rtsp://x", tmp_path, segment_sec=1.0, keep_sec=10.0, max_episode_sec=60.0
    )
    now = int(datetime(2026, 6, 19, 14, 0, 0).timestamp())
    _touch_segments(tmp_path, now - 200, 50)  # all far older than keep_sec
    # floor 200s old > keep_sec(10) + max_episode(60) → treated as a leak, ignored
    rec.set_protect_floor(float(now - 200))
    assert rec.prune(now=float(now)) == 50  # self-expired → all aged-out pruned


def test_submit_no_op_after_stop_protects_sentinel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sentry_agent_pc.edge import recorder as rm

    monkeypatch.setattr(rm.SegmentRecorder, "start", lambda self: None)
    monkeypatch.setattr(rm.SegmentRecorder, "stop", lambda self: None)
    rec = rm.EdgeClipRecorder("cam01", "rtsp://x", tmp_path, rm.ClipStore(tmp_path / "i.json"))
    rec.start()
    rec.stop()  # sets the shutdown flag, drains the worker
    before = rec._queue.qsize()
    rec.submit(rm.SuspiciousEpisode("cam01", 0.0, 1.0, 80.0))  # must be a no-op
    assert rec._queue.qsize() == before  # nothing queued post-stop


def test_submit_processes_episode_off_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time

    from sentry_agent_pc.edge import recorder as rm

    monkeypatch.setattr(rm.SegmentRecorder, "start", lambda self: None)
    monkeypatch.setattr(rm.SegmentRecorder, "stop", lambda self: None)
    now = time.time()
    fake = ClipRecord(
        clip_id="id1",
        camera_id="cam01",
        path=str(tmp_path / "x.mp4"),
        started_at=now - 7,
        ended_at=now,
        risk_pct=80.0,
        behaviors=["conceal"],
        created_at=now,
    )
    monkeypatch.setattr(rm, "build_clip", lambda *a, **k: fake)
    captured: list[ClipRecord] = []
    rec = rm.EdgeClipRecorder(
        "cam01",
        "rtsp://x",
        tmp_path,
        rm.ClipStore(tmp_path / "i.json"),
        on_clip=captured.append,
    )
    rec.start()
    rec.submit(rm.SuspiciousEpisode("cam01", 0.0, 1.0, 80.0))
    for _ in range(100):  # the worker processes it on its own thread
        if captured:
            break
        time.sleep(0.02)
    rec.stop()
    assert captured == [fake]


def test_edge_clip_recorder_stores_and_fires_on_clip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sentry_agent_pc.edge import recorder as rm

    now = __import__("time").time()
    fake = ClipRecord(
        clip_id="id1",
        camera_id="cam01",
        path=str(tmp_path / "x.mp4"),
        started_at=now - 7,
        ended_at=now,
        risk_pct=82.0,
        behaviors=["conceal"],
        created_at=now,
    )
    monkeypatch.setattr(rm, "build_clip", lambda *a, **k: fake)
    captured: list[ClipRecord] = []
    store = rm.ClipStore(tmp_path / "index.json")
    rec = rm.EdgeClipRecorder("cam01", "rtsp://x", tmp_path, store, on_clip=captured.append)
    out = rec.on_episode(rm.SuspiciousEpisode("cam01", 0.0, 1.0, 82.0))
    assert out is fake
    assert captured == [fake]
    assert len(store.records()) == 1
