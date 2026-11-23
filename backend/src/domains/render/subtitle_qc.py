"""字幕 QC 指标层（阶段2 2026-11-18）：渲染后从 cue + 词级时间计算验收指标。

定位：**只报告、不拦截**（沿用「字幕问题不阻断渲染」契约）。指标写入成片
``warnings.json`` 的 ``subtitle_qc`` 块，供验收与工作台透出：

======================  ============================================
验收项                   计算方式
======================  ============================================
同步误差 p95≤200ms       逐 cue 首/尾与其首词/尾词词时间偏差（ms）取 p95；
                         无词级数据的切片记 ``words_missing``，不计入分母
每行≤16字 100%           逐行字符数（与 cue 合成层同一 ``max_line_chars`` 口径）
两行以内 100%             逐 cue 行数（``max_lines`` 口径）
超过6秒的cue：0          逐 cue 时长 vs ``max_cue_seconds``
======================  ============================================

「复杂背景可读率≥98%」属阶段2 P2（subtitle_readability），本层预留
``readability`` 字段位，P1 固定为 None。

零模型依赖：输入只有 cue 列表（或 SRT 文本）与词级时间，纯标准库。
"""

from __future__ import annotations

import math
import os
import re

from . import subtitle_cueing as _cueing

#: 同步误差验收阈值（ms）。环境变量 ``SUBTITLE_QC_P95_MS`` 可覆盖（改阈值只
#: 影响报告判定，不影响渲染产物，因此不进渲染指纹）。
DEFAULT_SYNC_P95_TARGET_MS = 200.0
#: 时长比较容差（秒），浮点时间轴防止 1e-9 级误差误报。
_DURATION_EPS = 1e-6

_SRT_TS_RE = re.compile(r"(\d{1,2}):(\d{1,2}):(\d{1,2})[,.](\d{1,3})")


def _ts_to_seconds(ts: str) -> float:
    m = _SRT_TS_RE.match(ts.strip())
    if not m:
        return 0.0
    h, mi, s, ms = (int(m.group(i)) for i in range(1, 5))
    return h * 3600 + mi * 60 + s + ms / 1000.0


def cues_from_srt(srt_text: str) -> list[dict]:
    """规范 SRT → ``[{"start": 秒, "end": 秒, "text": "行1\\n行2"}]``。

    与 :func:`subtitle_cueing.build_cues_from_segments` 的 cue 形状一致
    （text 内 ``\\n`` 为屏内换行）。坏块跳过；时间非法（end<start）钳为 0 长。
    """
    cues: list[dict] = []
    for block in re.split(r"\n\s*\n", srt_text.strip()):
        lines = [ln for ln in block.split("\n") if ln.strip()]
        if len(lines) < 2:
            continue
        t0, t1 = lines[1].split("-->") if "-->" in lines[1] else ("", "")
        start, end = _ts_to_seconds(t0), _ts_to_seconds(t1)
        if end < start:
            end = start
        text = "\n".join(ln.strip() for ln in lines[2:])
        if text:
            cues.append({"start": start, "end": end, "text": text})
    return cues


def _p95(values: list[float]) -> float | None:
    """最近秩 p95（ceil(0.95n)-1 索引）；空列表返回 None。"""
    if not values:
        return None
    ordered = sorted(values)
    idx = max(0, math.ceil(0.95 * len(ordered)) - 1)
    return ordered[min(idx, len(ordered) - 1)]


def _sync_errors_ms(cues: list[dict], words: list[dict]) -> tuple[list[float], int]:
    """逐 cue 边界 vs 词边界偏差（ms）。

    词按**中点落入 cue 区间**归属（cue 文本经过冗余清理/精修替换，与词表
    逐字对不上，时间包含关系才是稳定口径）；首词起点/尾词终点与 cue 边界
    的绝对偏差各计一个样本。返回 ``(误差列表, 未命中词的 cue 数)``。
    """
    errors: list[float] = []
    unmatched = 0
    for cue in cues:
        s, e = float(cue["start"]), float(cue["end"])
        inside = [w for w in words
                  if s <= (float(w["start"]) + float(w["end"])) / 2.0 <= e]
        if not inside:
            unmatched += 1
            continue
        errors.append(abs(s - float(inside[0]["start"])) * 1000.0)
        errors.append(abs(e - float(inside[-1]["end"])) * 1000.0)
    return errors, unmatched


def qc_report(cues: list[dict], words: list[dict] | None = None,
              cfg: "_cueing.CueConfig | None" = None, env: dict | None = None,
              readability: dict | None = None) -> dict:
    """计算验收指标报告（见模块头表格）。

    ``cues``：cue 合成产物（秒）；``words``：词级时间（秒，与 cue 同一时间轴，
    虚拟时间轴切片传平移后的词）；``readability``：P2 可读性检查结果占位。
    """
    cfg = cfg or _cueing.config_from_env(env)
    e = os.environ if env is None else env
    try:
        p95_target = float(e.get("SUBTITLE_QC_P95_MS") or DEFAULT_SYNC_P95_TARGET_MS)
    except ValueError:
        p95_target = DEFAULT_SYNC_P95_TARGET_MS

    line_samples: list[str] = []
    line_violations = 0
    lines_samples: list[dict] = []
    lines_violations = 0
    over_duration: list[dict] = []
    durations: list[float] = []
    for idx, cue in enumerate(cues, start=1):
        text_lines = [ln for ln in str(cue.get("text") or "").split("\n") if ln]
        for ln in text_lines:
            if len(ln) > cfg.max_line_chars:
                line_violations += 1
                if len(line_samples) < 5:
                    line_samples.append(f"cue#{idx}:{ln}({len(ln)}字)")
        if len(text_lines) > cfg.max_lines:
            lines_violations += 1
            if len(lines_samples) < 5:
                lines_samples.append({"cue": idx, "lines": len(text_lines)})
        dur = max(0.0, float(cue["end"]) - float(cue["start"]))
        durations.append(dur)
        if dur > cfg.max_cue_seconds + _DURATION_EPS:
            over_duration.append({"cue": idx, "duration_s": round(dur, 3)})

    words = [w for w in (words or []) if w]
    # 词缺失也如实统计：words 为空时所有 cue 记 words_missing（p95=None、
    # 不计失败，但占比透出，防止「没词→看起来全对」的假绿）
    errors_ms, unmatched = _sync_errors_ms(cues, words)
    p95_ms = _p95(errors_ms)
    sync_pass: bool | None = None if p95_ms is None else p95_ms <= p95_target

    structure_pass = (line_violations == 0 and lines_violations == 0
                      and not over_duration)
    overall: bool | None = structure_pass and (sync_pass is not False)
    report = {
        "cue_count": len(cues),
        "max_line_chars": cfg.max_line_chars,
        "max_lines": cfg.max_lines,
        "max_cue_seconds": cfg.max_cue_seconds,
        "sync_p95_target_ms": p95_target,
        "lines": {"violations": line_violations, "samples": line_samples},
        "lines_per_cue": {"violations": lines_violations, "samples": lines_samples},
        "duration": {"over_max": over_duration,
                     "longest_s": round(max(durations), 3) if durations else 0.0},
        "sync": {
            "p95_ms": round(p95_ms, 1) if p95_ms is not None else None,
            "max_ms": round(max(errors_ms), 1) if errors_ms else None,
            "samples": len(errors_ms),
            "words_missing_cues": unmatched,
            "words_missing_ratio": round(unmatched / len(cues), 4) if cues else 0.0,
        },
        "readability": readability,
        "pass": {"structure": structure_pass, "sync": sync_pass, "overall": overall},
    }
    return report
