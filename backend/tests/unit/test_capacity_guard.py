"""隔离容量检查：拒绝零业务副作用、峰值、挂载预留和跨进程互斥。"""
from pathlib import Path
import multiprocessing
import asyncio
import pytest
from src import capacity_guard as cg


@pytest.fixture(autouse=True)
def isolated_reservations(tmp_path_factory, monkeypatch):
    ledger_root = tmp_path_factory.mktemp("capacity-ledger")
    monkeypatch.setattr(cg, "_reservation_paths", lambda device: (
        ledger_root / f"{device}.lock", ledger_root / f"{device}.json"))


def test_refusal_has_structure_and_no_target_write(tmp_path, monkeypatch):
    monkeypatch.setattr(cg, "free_bytes", lambda _: 10)
    target = tmp_path / "not-created"
    before = list(tmp_path.rglob("*"))
    with pytest.raises(cg.CapacityInsufficientError) as caught:
        with cg.reserve_capacity("assemble", 30, path=target, safety_margin_bytes=2):
            target.mkdir()
    assert caught.value.as_dict() | {} == {"code": "E_CAPACITY_INSUFFICIENT", "stage": "assemble",
        "required": 32, "available": 10, "deficit": 22, "message": str(caught.value)}
    assert list(tmp_path.rglob("*")) == before


def test_mount_marker_and_device_fail_closed(tmp_path):
    target = tmp_path / "unmounted"
    with pytest.raises(cg.CapacityInsufficientError, match="storage") as caught:
        cg.ensure_capacity("upload", 1, path=target, storage_marker=".storage-ready")
    assert caught.value.code == "E_STORAGE_MARKER_MISSING"
    assert not target.exists()
    with pytest.raises(cg.CapacityInsufficientError) as caught:
        cg.ensure_capacity("upload", 1, path=tmp_path, expected_st_dev=-1)
    assert caught.value.code == "E_STORAGE_DEVICE_MISMATCH"


def test_estimates_budget_additional_bytes_and_hardlinks(tmp_path):
    source = tmp_path / "source"; source.write_bytes(b"x" * 100)
    assert cg.estimate_upload_peak(100, merging=True) >= 100
    assert cg.estimate_render_peak(2000) >= 4_000_000
    assert cg.estimate_publish_peak([source]) == 100 + 65536
    assert cg.estimate_publish_peak([source], hardlink=True) == 65536
    assert cg.ensure_capacity("normal", 1, path=tmp_path, safety_margin_bytes=0)["deficit"] == 0


def _contend(path, queue, peak=1):
    try:
        with cg.reserve_capacity("upload", peak, path=path, safety_margin_bytes=0):
            queue.put("reserved")
    except cg.CapacityInsufficientError as exc:
        queue.put(exc.code)


def test_concurrent_process_cannot_overbook(tmp_path, monkeypatch):
    ctx = multiprocessing.get_context("fork")
    queue = ctx.Queue()
    monkeypatch.setattr(cg, "free_bytes", lambda _: 100)
    with cg.reserve_capacity("render", 80, path=tmp_path, safety_margin_bytes=0):
        child = ctx.Process(target=_contend, args=(str(tmp_path), queue, 30))
        child.start(); child.join(3)
        assert child.exitcode == 0
        assert queue.get(timeout=1) == "E_CAPACITY_INSUFFICIENT"
        # 长渲染仍持有预算时，小上传可并发完成，不出现设备长锁BUSY退化。
        child = ctx.Process(target=_contend, args=(str(tmp_path), queue, 10))
        child.start(); child.join(3)
        assert child.exitcode == 0 and queue.get(timeout=1) == "reserved"
    with cg.reserve_capacity("publish", 1, path=tmp_path, safety_margin_bytes=0):
        pass


def _crash_with_reservation(path, queue):
    with cg.reserve_capacity("render", 90, path=path, safety_margin_bytes=0):
        queue.put("ready")
        queue.close(); queue.join_thread()
        __import__("os")._exit(0)


def test_dead_process_reservation_reclaimed(tmp_path, monkeypatch):
    monkeypatch.setattr(cg, "free_bytes", lambda _: 100)
    ctx = multiprocessing.get_context("fork"); queue = ctx.Queue()
    child = ctx.Process(target=_crash_with_reservation, args=(str(tmp_path), queue))
    child.start(); child.join(3)
    assert child.exitcode == 0 and queue.get(timeout=1) == "ready"
    with cg.reserve_capacity("upload", 95, path=tmp_path, safety_margin_bytes=0):
        pass


def test_assemble_capacity_refusal_preserves_parts_and_destination(tmp_path, monkeypatch):
    from src.api.routes import media
    directory = tmp_path / "chunks"; directory.mkdir()
    part = directory / "000000.part"; part.write_bytes(b"abc")
    output = tmp_path / "video.mp4"; output.write_bytes(b"old")
    monkeypatch.setattr(cg, "free_bytes", lambda _: 0)
    with pytest.raises(media.HTTPException) as caught:
        media._assemble_parts(directory, output, 1, 100, 3)
    assert caught.value.status_code == 507
    assert caught.value.detail["stage"] == "assemble"
    assert part.read_bytes() == b"abc" and output.read_bytes() == b"old"
    assert not output.with_suffix(".mp4.assembling").exists()


def test_render_and_publish_capacity_refusal_zero_outputs(tmp_path, monkeypatch):
    from src import clip_render as cr, studio_job_worker as worker
    monkeypatch.setattr(cg, "free_bytes", lambda _: 0)
    source = tmp_path / "source"; source.write_bytes(b"synthetic")
    target = tmp_path / "render"
    aggregate = cr.render_plan({"clips": [{"clip_id": "clip-1", "start_ms": 0, "end_ms": 1000}]},
        source=source, out_root=target, corrected_segments=[], transcript_ref={})
    assert aggregate["job_status"] == "technical_failed" and not target.exists()
    assert aggregate["entries"][0]["capacity"]["stage"] == "render"
    monkeypatch.setattr(worker, "BASE_DIR", tmp_path)
    posts = []
    monkeypatch.setattr(worker, "_http", lambda *a, **k: {"artifacts":[]} if a[0]=="GET" else posts.append(a) or {})
    with pytest.raises(cg.CapacityInsufficientError):
        worker._upload_reedit_clip_artifacts("new", {"entries": [
            {"clip_id": "clip-1", "status": "rendered", "path": str(source)}]})
    assert posts == [] and not (tmp_path / "studio-artifacts").exists()


def test_upload_and_chunk_refusal_no_directories_or_stream_read(tmp_path, monkeypatch):
    from src.api.routes import media
    from starlette.datastructures import QueryParams
    from contextlib import nullcontext
    # 此反例只隔离物理容量；身份/用户配额由W07独立覆盖。
    monkeypatch.setattr(media,"upload_part_quota",lambda *a,**k:nullcontext())
    monkeypatch.setattr(cg, "free_bytes", lambda _: 0)
    class Upload:
        size = 100
        async def read(self, *a): pytest.fail("capacity refusal must precede reading")
    class Request:
        query_params = QueryParams("upload_id=" + "a"*32 + "&index=0")
        async def stream(self):
            pytest.fail("capacity refusal must precede streaming")
            yield b""
    monkeypatch.setattr(media, "_chunk_directory", lambda _: tmp_path / "chunk-root")
    for coro in (media._write_upload_to_disk(Upload(), tmp_path/"upload-root"/"video", 1000),
                 media.studio_transcribe_chunk(Request())):
        with pytest.raises(media.HTTPException) as caught:
            asyncio.run(coro)
        assert caught.value.status_code == 507
    assert list(tmp_path.iterdir()) == []
