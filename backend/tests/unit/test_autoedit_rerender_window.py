"""单切片复渲染链路回归测试（2026-10-12 422 故障回归）。

**真实根因**：05:02 的 HTTP 422 响应体是
``{"detail":{"code":"E_DISK_SPACE","detail":"197MB free (min 500MB)"}}`` ——
video-brain 的建任务环境门（preflight.py，MIN_DISK_MB=500）因数据盘只剩 197MB
而拒收，**不是**载荷字段错误（同一载荷在本地 Pydantic 校验通过）。把盘写满的
推手是复渲染路径无条件 ``shutil.copy2`` 3.07GB 源片；复制被 ENOSPC 打断还留下
204MB 的截断 source.mp4（任务 autoedit_cf3ca2b90e83）。执行器当时只记状态码，
丢掉了响应体 detail，才让故障看起来像载荷问题。

覆盖范围：

1. 正常单切片窗口 → 载荷合法（窗口裁剪 / required_clip_count=1 / duration_range 钉边界），
   且用 video-brain 真实 TaskCreateRequest 校验（schemas 缺失时自动跳过）；
2. fixed_clips 边界格式异常（HH:MM:SS / 倒挂 / nan / bool / 缺字段）→ 显式报错，绝不静默丢片；
   数值字符串正常解析；
3. 窗口内空 text 段 → 提交前被过滤；全部为空 → 显式报错；时钟格式转写段被剔除；
4. video-brain 建任务 422（E_DISK_SPACE）→ 失败日志与任务状态都携带错误码 + 响应体 detail；
5. 复渲染零拷贝硬链接源片（不复制 3GB）＋ 复制退路的空间守卫与原子性；
6. duration_range 恒为合法有序区间（旧实现对 <1.05s 切片会算出倒挂区间）；
7. clip_plan.json 真实层级是顶层 ``highlights``（不是 ``clips``）。
"""

from __future__ import annotations

import io
import json
import sys
import urllib.error
from email.message import Message
from pathlib import Path
from typing import Any

import pytest

from src import studio_auto_edit_bridge as bridge
from src.studio_auto_edit_bridge import TASK_ID_PATTERN

# 执行器是顶层脚本模块（from studio_runtime import …），把 backend/src 加进 sys.path 导入
SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
import autoedit_runner_video_brain as runner  # noqa: E402

VIDEO_BRAIN_SCHEMAS = Path("/domain/video-brain/app/schemas.py")
ORIGINAL_TASK_ID = "autoedit_064d806d3218"
CLIP_ID = "clip-741ef5-01"
CLIP_START, CLIP_END = 0.14, 16.3


# ---------------------------------------------------------------------------
# 真实数据结构的fixtures：157 段转写 + 原任务产物（clip_plan.json 结构与线上一致）
# ---------------------------------------------------------------------------

# 真实故障（autoedit_064d806d3218 → clip-741ef5-01）窗口内的 6 段转写，原样复用
REAL_WINDOW_SEGMENTS = [
    {"start": 0.14, "end": 3.42, "text": "你说一下情况说刚说的太多我快进入"},
    {"start": 3.42, "end": 5.46, "text": "就是你爸是这样子"},
    {"start": 5.46, "end": 8.54, "text": "他做了个场径做了影像学检查"},
    {"start": 8.54, "end": 11.38, "text": "现在发现首先他是个知常癌"},
    {"start": 11.38, "end": 13.2, "text": "病理拿到了没有问题"},
    {"start": 13.2, "end": 16.3, "text": "第二个发现肝上有多发的转移"},
]


def _realistic_segments() -> list[dict[str, Any]]:
    """按快照结构生成 157 段转写：真实窗口 6 段原样保留，窗口外补 ~141 段铺满 200s。"""
    segments = [dict(seg, speaker="未标注") for seg in REAL_WINDOW_SEGMENTS]
    start = 18.0
    while len(segments) < 157 and start < 200.0:
        end = round(start + 3.0, 3)
        segments.append({
            "start": round(start, 3), "end": end,
            "text": f"窗口外转写段 {len(segments) + 1}", "speaker": "未标注",
        })
        start = end
    return segments


def _write_original_task(root: Path) -> None:
    """在临时 tasks 根写入已完成原任务：任务 JSON + media + clip_plan.json（真实层级）。"""
    task_id = ORIGINAL_TASK_ID
    media_dir = root / "media" / task_id
    media_dir.mkdir(parents=True)
    (media_dir / "source.mp4").write_bytes(b"fake-video")
    output_dir = root / "output" / task_id
    output_dir.mkdir(parents=True)
    # 与线上完全一致的 clip_plan.json 顶层结构：highlights 平铺列表
    (output_dir / "clip_plan.json").write_text(json.dumps({
        "engine": "medical-video-production",
        "video_brain_task_id": "task-741ef509cf",
        "highlights": [{
            "clip_id": CLIP_ID, "start": CLIP_START, "end": CLIP_END,
            "topic": "健康科普", "cover_title": "你说一下情况说刚说的太多我快·医疗科普",
        }],
        "golden_sentences": [], "keywords": [],
    }, ensure_ascii=False), encoding="utf-8")
    for state in ("completed", "status"):
        (root / state).mkdir(parents=True, exist_ok=True)
    (root / "completed" / f"{task_id}.json").write_text(json.dumps({
        "task_id": task_id,
        "raw_video_path": f"media/{task_id}/source.mp4",
        "asr_segments": _realistic_segments(),
        "golden_sentences": [], "keywords": [],
        "output_dir": f"output/{task_id}",
        "bridge": {"engine": "medical-video-production", "ratio": "9:16"},
    }, ensure_ascii=False), encoding="utf-8")
    (root / "status" / f"{task_id}.json").write_text(json.dumps({
        "task_id": task_id, "status": "completed", "artifacts": [],
    }), encoding="utf-8")


@pytest.fixture()
def tasks_root(tmp_path, monkeypatch):
    root = tmp_path / "tasks"
    for name in ("in", "processing", "completed", "failed", "status", "output", "media"):
        (root / name).mkdir(parents=True, exist_ok=True)
    _write_original_task(root)
    # 伪造 video-brain 产物目录，让 GET 终态后的切片回传能走完
    vb_home = tmp_path / "vb"
    clip_dir = vb_home / "data" / "tasks" / "task-fakeee01" / "clips" / CLIP_ID
    clip_dir.mkdir(parents=True)
    (clip_dir / "clip_final.mp4").write_bytes(b"fake-clip")
    (clip_dir / "cover.jpg").write_bytes(b"fake-cover")
    monkeypatch.setattr(bridge, "TASKS_ROOT", root)
    monkeypatch.setattr(runner, "TASKS_ROOT", root)
    monkeypatch.setattr(runner, "VIDEO_BRAIN_HOME", vb_home)
    return root


def _enqueue_rerender() -> str:
    """走真实 bridge 复渲染路径（服务端解析 clip_plan），返回新任务 id。"""
    rerendered = bridge.rerender_auto_edit_task(ORIGINAL_TASK_ID, clip_ids=[CLIP_ID])
    assert TASK_ID_PATTERN.fullmatch(rerendered["task_id"])
    return rerendered["task_id"]


def _capture_start(root: Path, task_id: str, monkeypatch) -> dict[str, Any]:
    """让 runner._request 捕获建任务载荷；GET 轮询返回完成终态以走完 _process。"""
    captured: dict[str, Any] = {}

    def fake_request(method: str, path: str, payload=None, timeout: float = 30):
        if method == "POST":
            captured.update(payload or {})
            return {"task_id": "task-fakeee01", "status": "queued", "steps": [], "clips": []}
        return {
            "task_id": "task-fakeee01", "status": "completed", "steps": [], "error": None,
            "clips": [{"clip_id": CLIP_ID, "start": CLIP_START, "end": CLIP_END,
                       "status": "qc_passed", "topic": "健康科普", "title": "医疗科普"}],
        }

    monkeypatch.setattr(runner, "_request", fake_request)
    task_file = root / "processing" / f"{task_id}.json"
    task = json.loads(task_file.read_text(encoding="utf-8"))
    runner._process(task_id, task_file, task)
    return captured


# ---------------------------------------------------------------------------
# 1. 正常单切片窗口 → 载荷合法
# ---------------------------------------------------------------------------

def test_single_clip_window_builds_valid_payload(tasks_root, monkeypatch):
    rerendered_id = _enqueue_rerender()
    # bridge 从 clip_plan.json 服务端解析出 fixed_clips（真实 highlights 结构）
    cloned = json.loads((tasks_root / "in" / f"{rerendered_id}.json").read_text(encoding="utf-8"))
    assert cloned["fixed_clips"] == [{
        "clip_id": CLIP_ID, "start": CLIP_START, "end": CLIP_END,
        "topic": "健康科普", "cover_title": "你说一下情况说刚说的太多我快·医疗科普",
    }]

    (tasks_root / "in" / f"{rerendered_id}.json").replace(tasks_root / "processing" / f"{rerendered_id}.json")
    payload = _capture_start(tasks_root, rerendered_id, monkeypatch)

    # 窗口裁剪：clip [0.14, 16.3] 内（end>0.16 且 start<16.28）恰好 6 段真实窗口段
    transcript = payload["source_transcript"]
    assert [seg["start"] for seg in transcript] == [0.14, 3.42, 5.46, 8.54, 11.38, 13.2]
    assert transcript == [{k: v for k, v in seg.items()} for seg in REAL_WINDOW_SEGMENTS]
    assert all(float(seg["end"]) > CLIP_START + 0.02 and float(seg["start"]) < CLIP_END - 0.02
               for seg in transcript)
    # 切片数与时长范围钉在原切片边界
    assert payload["required_clip_count"] == 1
    assert payload["duration_range"] == [round(CLIP_END - CLIP_START - 0.05, 3),
                                         round(CLIP_END - CLIP_START + 0.05, 3)]

    # 用 video-brain 真实校验器确认载荷合法（schemas 缺失的环境自动跳过）
    if not VIDEO_BRAIN_SCHEMAS.is_file():
        pytest.skip("/domain/video-brain/app/schemas.py 不存在")
    import importlib.util
    spec = importlib.util.spec_from_file_location("vb_schemas", VIDEO_BRAIN_SCHEMAS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    accepted = module.TaskCreateRequest(**payload)
    assert accepted.required_clip_count == 1
    assert len(accepted.source_transcript) == 6


# ---------------------------------------------------------------------------
# 2. fixed_clips 边界格式异常 → 正确解析数值串 / 跳过非法项 / 显式报错
# ---------------------------------------------------------------------------

def test_fixed_clips_parse_numeric_strings():
    """数值字符串（"0.14"）可解析；这是同一时间基的合法写法。"""
    task = {"fixed_clips": [{"clip_id": CLIP_ID, "start": "0.14", "end": "16.3"}]}
    assert runner._fixed_clips(task) == [{"clip_id": CLIP_ID, "start": 0.14, "end": 16.3}]


@pytest.mark.parametrize(
    ("entry", "reason"),
    [
        ({"clip_id": "clock", "start": "00:00:05.200", "end": "00:00:16.300"}, "时钟格式"),
        ({"clip_id": "reversed", "start": 16.3, "end": 0.14}, "切片边界非法"),
        ({"clip_id": "missing-end", "start": 1.0}, "缺少 start/end"),
        ({"clip_id": "nan", "start": float("nan"), "end": 16.3}, "不是有限数值"),
        ({"clip_id": "bool", "start": True, "end": 16.3}, "必须是数值秒"),
        ({"clip_id": "text", "start": "五秒", "end": 16.3}, "不是合法数值秒"),
        ("not-a-dict", "不是对象"),
    ],
)
def test_fixed_clips_never_silently_drops(entry, reason):
    """固定切片是显式意图：坏条目必须显式失败，不能静默少渲染一条成片。"""
    with pytest.raises(ValueError, match=reason):
        runner._fixed_clips({"fixed_clips": [entry]})


def test_fixed_clips_rejects_duration_below_video_brain_floor():
    """短于 1.0s 的切片无论怎么钉窗口都不可能被规划器接受，必须提前显式拒绝。"""
    with pytest.raises(ValueError, match="低于 video-brain 可渲染下限"):
        runner._fixed_clips({"fixed_clips": [{"clip_id": "tiny", "start": 1.0, "end": 1.4}]})


def test_fixed_clips_absent_is_not_a_fixed_render():
    assert runner._fixed_clips({}) == []
    assert runner._fixed_clips({"fixed_clips": None}) == []


def test_rerender_rejects_unknown_clip_id_explicitly(tasks_root):
    """未知 clip_id 必须显式失败。

    自包含 fixture：``tasks_root`` 在本用例自己的临时目录里写入原任务（含 CLIP_ID 的
    ``clip_plan.json`` 与 ``asr_segments``），因此断言不依赖磁盘上是否还存在
    真实任务 ``autoedit_064d806d3218``（它已被清理，旧写法会先抛
    ``FileNotFoundError: Auto-edit task not found`` 而不是这条校验）。
    """
    assert (tasks_root / "output" / ORIGINAL_TASK_ID / "clip_plan.json").is_file()
    with pytest.raises(ValueError, match="clip_ids"):
        bridge.rerender_auto_edit_task(ORIGINAL_TASK_ID, clip_ids=[CLIP_ID + "-x"])


def test_rerender_rejects_non_numeric_plan_boundaries(tmp_path, monkeypatch):
    """clip_plan 边界若是 HH:MM:SS 字符串，桥接必须显式报错而不是甩出 float() 晦涩异常。"""
    root = tmp_path / "tasks"
    for name in ("completed", "status"):
        (root / name).mkdir(parents=True, exist_ok=True)
    plan_dir = root / "output" / ORIGINAL_TASK_ID
    plan_dir.mkdir(parents=True)
    (plan_dir / "clip_plan.json").write_text(json.dumps({
        "engine": "medical-video-production", "video_brain_task_id": "task-x",
        "highlights": [{"clip_id": CLIP_ID, "start": "00:00:00.140", "end": "00:00:16.300"}],
    }, ensure_ascii=False), encoding="utf-8")
    (root / "completed" / f"{ORIGINAL_TASK_ID}.json").write_text(json.dumps({
        "task_id": ORIGINAL_TASK_ID, "raw_video_path": "media/x/source.mp4",
        "asr_segments": [], "bridge": {},
    }), encoding="utf-8")
    (root / "media" / "x").mkdir(parents=True)
    (root / "media" / "x" / "source.mp4").write_bytes(b"fake-video")
    monkeypatch.setattr(bridge, "TASKS_ROOT", root)
    with pytest.raises(ValueError, match="clip_plan 边界非法"):
        bridge.rerender_auto_edit_task(ORIGINAL_TASK_ID, clip_ids=[CLIP_ID])


# ---------------------------------------------------------------------------
# 3. 窗口内空 text 段 → 提交前被过滤
# ---------------------------------------------------------------------------

def test_blank_text_segments_filtered_from_window(tasks_root, monkeypatch):
    # 在窗口内注入一个空 text 段（video-brain 对 text 非 blank 校验会 422 整单）
    task_path = tasks_root / "completed" / f"{ORIGINAL_TASK_ID}.json"
    task = json.loads(task_path.read_text(encoding="utf-8"))
    task["asr_segments"].insert(2, {"start": 4.0, "end": 5.0, "text": "   ", "speaker": "未标注"})
    task_path.write_text(json.dumps(task, ensure_ascii=False), encoding="utf-8")

    rerendered_id = _enqueue_rerender()
    (tasks_root / "in" / f"{rerendered_id}.json").replace(tasks_root / "processing" / f"{rerendered_id}.json")
    payload = _capture_start(tasks_root, rerendered_id, monkeypatch)

    starts = [seg["start"] for seg in payload["source_transcript"]]
    assert 4.0 not in starts
    assert [seg["start"] for seg in payload["source_transcript"]] == [0.14, 3.42, 5.46, 8.54, 11.38, 13.2]


def test_all_blank_segments_fail_loudly(tasks_root):
    with pytest.raises(ValueError, match="空文本段"):
        runner._start_video_brain_task(ORIGINAL_TASK_ID, {
            "raw_video_path": f"media/{ORIGINAL_TASK_ID}/source.mp4",
            "asr_segments": [{"start": 1.0, "end": 2.0, "text": "   "}],
        })


# ---------------------------------------------------------------------------
# 4. 422 响应体 detail 进日志与任务状态
# ---------------------------------------------------------------------------

def _http_error(body: bytes) -> urllib.error.HTTPError:
    headers = Message()
    headers["Content-Type"] = "application/json"
    return urllib.error.HTTPError("http://vb/v1/tasks", 422, "Unprocessable Entity", headers, io.BytesIO(body))


def test_http_422_detail_reaches_log_and_status(tasks_root, monkeypatch, capsys):
    rerendered_id = _enqueue_rerender()
    (tasks_root / "in" / f"{rerendered_id}.json").replace(tasks_root / "processing" / f"{rerendered_id}.json")

    body = json.dumps({"detail": {"code": "E_DISK_SPACE", "detail": "197MB free (min 500MB)"}}).encode()
    monkeypatch.setattr(runner, "_request", lambda *a, **k: (_ for _ in ()).throw(_http_error(body)))
    # 执行器兜底循环之外直接驱动 _process，验证建任务失败分支
    task_file = tasks_root / "processing" / f"{rerendered_id}.json"
    task = json.loads(task_file.read_text(encoding="utf-8"))
    runner._process(rerendered_id, task_file, task)

    out = capsys.readouterr().out
    assert f"{rerendered_id}: 建任务失败 HTTP 422" in out
    # 错误码必须保真（可检索）+ 原始 detail 必须落日志，缺一不可
    assert "E_DISK_SPACE" in out
    assert "197MB free (min 500MB)" in out
    assert "数据盘剩余空间不足" in out

    status = json.loads((tasks_root / "status" / f"{rerendered_id}.json").read_text(encoding="utf-8"))
    assert status["status"] == "failed"
    assert "E_DISK_SPACE" in status["error"] and "数据盘剩余空间不足" in status["error"]
    assert (tasks_root / "failed" / f"{rerendered_id}.json").is_file()


def test_vb_error_zh_passthrough_on_unshaped_body():
    assert runner._vb_error_zh("not-json") == "not-json"
    assert runner._vb_error_zh('{"detail":"plain"}') == '{"detail":"plain"}'


def test_vb_error_zh_keeps_machine_code_and_raw_detail():
    """中文翻译不能吃掉错误码——排障靠 E_* 检索。"""
    translated = runner._vb_error_zh(
        json.dumps({"detail": {"code": "E_DISK_SPACE", "detail": "197MB free (min 500MB)"}})
    )
    assert "E_DISK_SPACE" in translated
    assert "197MB free (min 500MB)" in translated
    # 未收录的错误码也要保真透出，不能退化成空串
    unknown = runner._vb_error_zh(json.dumps({"detail": {"code": "E_NEW", "detail": "boom"}}))
    assert "E_NEW" in unknown and "boom" in unknown


# ---------------------------------------------------------------------------
# 5. 真实根因回归：复渲染不得复制 3GB 源片（否则写满数据盘 → E_DISK_SPACE 422）
# ---------------------------------------------------------------------------

def test_rerender_hardlinks_source_without_copying(tasks_root):
    """复渲染必须零拷贝复用源片：同盘硬链接（同 inode），否则每单再吃 3GB。"""
    original_media = tasks_root / "media" / ORIGINAL_TASK_ID / "source.mp4"
    rerendered_id = _enqueue_rerender()
    cloned_media = tasks_root / "media" / rerendered_id / "source.mp4"

    assert cloned_media.is_file()
    assert cloned_media.stat().st_ino == original_media.stat().st_ino, "应硬链接复用同一 inode"
    # 内容仍可独立读取（video-brain 只读该路径）
    assert cloned_media.read_bytes() == original_media.read_bytes()


def test_materialise_source_refuses_copy_without_space(tmp_path, monkeypatch):
    """跨设备退回复制时，空间不足必须显式拒绝，而不是写到一半 ENOSPC。"""
    source = tmp_path / "source.mp4"
    source.write_bytes(b"x" * 1024)
    destination = tmp_path / "media" / "n" / "source.mp4"

    monkeypatch.setattr(bridge.os, "link", lambda *a, **k: (_ for _ in ()).throw(OSError("EXDEV")))

    class _Usage:
        free = 1024 * 1024  # 1MB，远小于文件 + 512MB 余量

    monkeypatch.setattr(bridge.shutil, "disk_usage", lambda *a, **k: _Usage())
    with pytest.raises(ValueError, match="不足以安全复制源视频"):
        bridge._materialise_source(source, destination)
    assert not destination.exists(), "空间不足时不得留下半截 source.mp4"


def test_materialise_source_copy_is_atomic_on_failure(tmp_path, monkeypatch):
    """复制中途失败（ENOSPC）只留 .part 临时文件，绝不留下看似有效的截断 source.mp4。"""
    source = tmp_path / "source.mp4"
    source.write_bytes(b"x" * 1024)
    destination = tmp_path / "media" / "n" / "source.mp4"

    monkeypatch.setattr(bridge.os, "link", lambda *a, **k: (_ for _ in ()).throw(OSError("EXDEV")))

    class _Usage:
        free = 1 << 40  # 空间充足，走到复制分支

    monkeypatch.setattr(bridge.shutil, "disk_usage", lambda *a, **k: _Usage())

    def _boom(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(bridge.shutil, "copy2", _boom)
    with pytest.raises(OSError, match="No space left"):
        bridge._materialise_source(source, destination)

    assert not destination.exists(), "失败后不得留下 destination"
    assert list(destination.parent.glob("*.part")) == [], "临时文件必须被清理"


# ---------------------------------------------------------------------------
# 6. duration_range 必须恒为合法区间（旧实现对 <1.05s 切片会算出倒挂区间）
# ---------------------------------------------------------------------------

def _fixed_clip_payload(monkeypatch, root: Path, duration: float) -> dict[str, Any]:
    """直接驱动 _start_video_brain_task 捕获载荷，切片时长可控。"""
    captured: dict[str, Any] = {}

    def fake_request(method, path, payload=None, timeout=30):
        captured.update(payload or {})
        return {"task_id": "task-fakeee01"}

    monkeypatch.setattr(runner, "_request", fake_request)
    task_id = "autoedit_000000000001"
    (root / "media" / task_id).mkdir(parents=True, exist_ok=True)
    (root / "media" / task_id / "source.mp4").write_bytes(b"fake-video")
    task = {
        "raw_video_path": f"media/{task_id}/source.mp4",
        "fixed_clips": [{"clip_id": CLIP_ID, "start": 0.0, "end": duration}],
        "asr_segments": [
            {"start": 0.0, "end": round(duration / 2, 3), "text": "第一段"},
            {"start": round(duration / 2, 3), "end": duration, "text": "第二段"},
        ],
    }
    runner._start_video_brain_task(task_id, task)
    return captured


def test_duration_range_stays_ordered_for_short_clip(tasks_root, monkeypatch):
    """1.0s 切片：下界被抬到 1.0，上界必须仍大于下界（旧实现给出 [1.0, 0.55] 倒挂）。"""
    payload = _fixed_clip_payload(monkeypatch, tasks_root, duration=1.0)
    low, high = payload["duration_range"]
    assert low < high, f"duration_range 倒挂: {payload['duration_range']}"


def test_duration_range_pins_original_clip_window(tasks_root, monkeypatch):
    """正常 16.16s 切片：区间仍钉在原切片 ±0.05s。"""
    payload = _fixed_clip_payload(monkeypatch, tasks_root, duration=16.16)
    assert payload["duration_range"] == [16.11, 16.21]
    assert payload["required_clip_count"] == 1


def test_clock_format_transcript_segment_is_excluded(tasks_root, monkeypatch):
    """转写段边界若是时钟格式，该段被剔除而不是被静默当成另一种时间基。"""
    payload = _fixed_clip_payload(monkeypatch, tasks_root, duration=16.0)
    starts = [seg["start"] for seg in payload["source_transcript"]]
    assert starts == [0.0, 8.0]

    captured: dict[str, Any] = {}

    def fake_request(method, path, payload=None, timeout=30):
        captured.update(payload or {})
        return {"task_id": "task-fakeee01"}

    monkeypatch.setattr(runner, "_request", fake_request)
    task_id = "autoedit_000000000002"
    (tasks_root / "media" / task_id).mkdir(parents=True, exist_ok=True)
    (tasks_root / "media" / task_id / "source.mp4").write_bytes(b"fake-video")
    runner._start_video_brain_task(task_id, {
        "raw_video_path": f"media/{task_id}/source.mp4",
        "asr_segments": [
            {"start": "00:00:01.000", "end": "00:00:05.000", "text": "时钟格式段"},
            {"start": 6.0, "end": 9.0, "text": "数值秒段"},
        ],
    })
    assert [seg["start"] for seg in captured["source_transcript"]] == [6.0]


def test_plan_without_highlights_key_is_not_a_clip_source(tmp_path, monkeypatch):
    """clip_plan 的切片列表在 ``highlights``；旧的 ``clips`` 假设恒为 None。"""
    root = tmp_path / "tasks"
    (root / "completed").mkdir(parents=True)
    (root / "status").mkdir(parents=True)
    output_dir = root / "output" / ORIGINAL_TASK_ID
    output_dir.mkdir(parents=True)
    # 只有旧的错误层级（clips），没有 highlights → 必须视作「无可用成片」显式报错
    (output_dir / "clip_plan.json").write_text(json.dumps({
        "engine": "medical-video-production",
        "clips": [{"clip_id": CLIP_ID, "start": CLIP_START, "end": CLIP_END}],
    }, ensure_ascii=False), encoding="utf-8")
    (root / "completed" / f"{ORIGINAL_TASK_ID}.json").write_text(json.dumps({
        "task_id": ORIGINAL_TASK_ID, "raw_video_path": "media/x/source.mp4",
        "asr_segments": [], "bridge": {},
    }), encoding="utf-8")
    (root / "media" / "x").mkdir(parents=True)
    (root / "media" / "x" / "source.mp4").write_bytes(b"fake-video")
    monkeypatch.setattr(bridge, "TASKS_ROOT", root)
    with pytest.raises(ValueError, match="未匹配到任务"):
        bridge.rerender_auto_edit_task(ORIGINAL_TASK_ID, clip_ids=[CLIP_ID])
