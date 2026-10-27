"""目标文件系统容量守卫；短锁登记跨进程预算，拒绝时不创建业务路径。"""
from contextlib import contextmanager
from pathlib import Path
import os
import shutil
import tempfile
import json
import time
import uuid

from .studio_runtime import process_lock

SAFETY_MARGIN_BYTES = int(os.getenv("CAPACITY_SAFETY_MARGIN_BYTES", str(512 * 1024 * 1024)))
# 多账号阶段四（D11 全站磁盘保护水位）：
# * CAPACITY_FLOOR_BYTES：全站可用空间下限，低于即拒绝新增上传/转写/渲染（查看/下载/删除不受影响）。
#   默认 0 = 未启用（生产在运维确认 U5-1 终值前保持关闭；隔离环境经 env 设置测试值）。
# * CAPACITY_CHECK_INTERVAL_MS：floor 拒绝后的快速拒绝缓存 TTL（毫秒，默认 0=每请求实时判定）。
#   拒绝结果可缓存（宁可多拒）；放行结果永不缓存（避免超卖）。
CAPACITY_FLOOR_BYTES = int(os.getenv("CAPACITY_FLOOR_BYTES", "0"))
CAPACITY_CHECK_INTERVAL_MS = int(os.getenv("CAPACITY_CHECK_INTERVAL_MS", "0"))
_floor_reject_until: dict[int, float] = {}


def floor_bytes() -> int:
    return max(0, CAPACITY_FLOOR_BYTES)


def _floor_reject_cached(device: int, now: float) -> bool:
    """检查间隔（负面缓存）：TTL 内同一文件系统直接维持拒绝判定。interval=0 时不缓存。"""
    until = _floor_reject_until.get(device)
    return bool(CAPACITY_CHECK_INTERVAL_MS > 0 and until is not None and now < until)


def _record_floor_reject(device: int, now: float) -> None:
    if CAPACITY_CHECK_INTERVAL_MS > 0:
        _floor_reject_until[device] = now + CAPACITY_CHECK_INTERVAL_MS / 1000.0


def _clear_floor_reject(device: int) -> None:
    _floor_reject_until.pop(device, None)


def site_floor_status(path=None) -> dict:
    """监控视图：数据盘水位与放行状态（只读，不做任何判定副作用）。"""
    existing = target_filesystem(path)
    free = free_bytes(existing)
    floor = floor_bytes()
    return {"path": str(existing), "free_bytes": free, "floor_bytes": floor,
            "floor_enabled": floor > 0, "upload_allowed": (not floor > 0) or free >= floor}


class CapacityInsufficientError(RuntimeError):
    def __init__(self, stage, required, available, *, code="E_CAPACITY_INSUFFICIENT"):
        self.code, self.stage = code, stage
        self.required, self.available = int(required), int(available)
        self.deficit = max(0, self.required - self.available)
        super().__init__(f"{stage} 容量不足：需要 {self.required} 字节，可用 {self.available} 字节，缺少 {self.deficit} 字节")

    def as_dict(self):
        return {"code": self.code, "stage": self.stage, "required": self.required,
                "available": self.available, "deficit": self.deficit, "message": str(self)}


def target_filesystem(path=None, *, expected_st_dev=None, storage_marker=None):
    target = Path(path or os.getenv("STUDIO_TASKS_ROOT", Path(__file__).resolve().parent.parent / "tasks"))
    if storage_marker is not None:
        marker = Path(storage_marker)
        marker = marker if marker.is_absolute() else target / marker
        if not target.is_dir() or not marker.is_file():
            raise CapacityInsufficientError("storage", 0, 0, code="E_STORAGE_MARKER_MISSING")
    existing = target
    while not existing.exists():
        if existing == existing.parent:
            raise CapacityInsufficientError("storage", 0, 0, code="E_STORAGE_ROOT_MISSING")
        existing = existing.parent
    if expected_st_dev is not None and existing.stat().st_dev != expected_st_dev:
        raise CapacityInsufficientError("storage", 0, 0, code="E_STORAGE_DEVICE_MISMATCH")
    return existing


def free_bytes(path=None):
    return shutil.disk_usage(target_filesystem(path)).free


def ensure_capacity(stage, peak_bytes, *, path=None, safety_margin_bytes=None,
                    expected_st_dev=None, storage_marker=None):
    existing = target_filesystem(path, expected_st_dev=expected_st_dev, storage_marker=storage_marker)
    required = max(0, int(peak_bytes)) + (SAFETY_MARGIN_BYTES if safety_margin_bytes is None else max(0, int(safety_margin_bytes)))
    available = free_bytes(existing)
    floor = floor_bytes()
    if floor > 0 and available < floor:
        # D11 全站水位：可用空间低于阈值 → 拒绝（查看/下载/删除等 light 路径不经过本函数）
        raise CapacityInsufficientError("site_floor", floor, available, code="E_CAPACITY_FLOOR")
    if available < required:
        raise CapacityInsufficientError(stage, required, available)
    return {"stage": stage, "required": required, "available": available, "deficit": 0}


@contextmanager
def reserve_capacity(stage, peak_bytes, *, path=None, **kwargs):
    existing = target_filesystem(path, expected_st_dev=kwargs.get("expected_st_dev"), storage_marker=kwargs.get("storage_marker"))
    device = existing.stat().st_dev
    lock, ledger = _reservation_paths(device)
    token = uuid.uuid4().hex
    peak = max(0, int(peak_bytes))
    margin = kwargs.get("safety_margin_bytes")
    margin = SAFETY_MARGIN_BYTES if margin is None else max(0, int(margin))
    # 锁只覆盖短台账更新；媒体写入/编码期间其它操作仍可按剩余预算预留。
    now = time.time()
    if _floor_reject_cached(device, now):
        # 检查间隔（负面缓存）：TTL 内维持拒绝，避免高并发下重复 stat/锁竞争
        raise CapacityInsufficientError("site_floor", floor_bytes(), 0, code="E_CAPACITY_FLOOR")
    with process_lock(lock):
        records = _live_reservations(ledger)
        reserved = sum(int(r["peak_bytes"]) for r in records.values())
        available = max(0, free_bytes(existing) - reserved)
        floor = floor_bytes()
        # D11 全站水位：本次预留被准入后的净可用（free - 活跃预留 - 本次 peak）低于阈值 → 拒绝。
        # 把本次 peak 计入判定，防止 N 个并发请求各自通过检查后合计耗尽磁盘。
        if floor > 0 and available - peak < floor:
            _record_floor_reject(device, time.time())
            raise CapacityInsufficientError("site_floor", floor, available, code="E_CAPACITY_FLOOR")
        if available < peak + margin:
            raise CapacityInsufficientError(stage, peak + margin, available)
        _clear_floor_reject(device)
        records[token] = {"pid": os.getpid(), "process_start": _process_start(os.getpid()),
                          "created_at": time.time(), "stage": stage, "peak_bytes": peak}
        _save_reservations(ledger, records)
    try:
        yield {"stage": stage, "required": peak + margin, "available": available,
               "deficit": 0, "reservation_id": token}
    finally:
        with process_lock(lock):
            records = _live_reservations(ledger)
            records.pop(token, None)
            _save_reservations(ledger, records)


def _process_start(pid):
    """Linux进程启动ticks避免PID复用；平台不可读时保守保留仍存活PID。"""
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def _reservation_paths(device):
    lock = Path(tempfile.gettempdir()) / f"studio-capacity-{device}.lock"
    return lock, lock.with_suffix(".json")


def _live_reservations(path):
    if not path.exists():
        return {}
    try:
        records = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(records, dict):
            raise ValueError("reservation ledger is not an object")
    except (ValueError, OSError) as exc:
        # 不把损坏台账当空，避免重复预留造成超额写入。
        raise CapacityInsufficientError("reservation", 0, 0, code="E_CAPACITY_LEDGER_INVALID") from exc
    live = {}
    for token, record in records.items():
        try:
            pid = int(record["pid"])
            if pid <= 0 or int(record["peak_bytes"]) < 0:
                raise ValueError("invalid reservation")
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        except (ValueError, TypeError, KeyError):
            raise CapacityInsufficientError("reservation", 0, 0, code="E_CAPACITY_LEDGER_INVALID")
        except PermissionError:
            pass
        start = _process_start(pid)
        if record.get("process_start") is not None and start is not None and start != record["process_start"]:
            continue
        live[token] = record
    return live


def _save_reservations(path, records):
    temporary = path.with_suffix("." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            temporary.chmod(0o600)
            json.dump(records, handle)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def estimate_upload_peak(size_bytes, *, merging=False):
    # 分片已经占用的字节包含在 free 中；合并额外完整副本仍须预算。
    return max(0, int(size_bytes)) + 64 * 1024


def estimate_render_peak(duration_ms):
    # 临时编码+原子最终发布：按每秒16Mbit视频和侧车保守预算。
    return int(max(1, duration_ms) / 1000 * 2_000_000) + 1024 * 1024


def estimate_publish_peak(paths, *, hardlink=False):
    return 64 * 1024 if hardlink else sum(Path(p).stat().st_size for p in paths) + 64 * 1024


def cleanup_candidates(path):
    """只读候选，不删除或把候选空间计为可用。"""
    root = Path(path)
    return [{"path": str(p), "bytes": p.stat().st_size} for p in root.glob("*.assembling") if p.is_file()]
