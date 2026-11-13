#!/usr/bin/env python3
"""小闭环第 3/4 步：视频片段库 + 知识点↔片段映射（2026-11-13，用户授权）。

输入：
- tasks/transcripts/<source>/v2.json 的 asr_segments（537 段，绝对源时间轴）
- speaker_labels.json 源级说话人 overlay（60 段已标 医生/患者/家属）
- medical_kb_seed/active_snapshot.json（45 条 MEL 知识点）

输出（tasks/projects/timeline_a01488a3894a/ 下）：
- medical_segment_library.json：全部分段 + speaker + 角色标签
  （结论/解释/行动建议/风险提示/其他，启发式 v1，labeled_by 如实标注）
- medical_kb_mapping.json：每条知识点 ↔ 支持片段。
  两层映射：direct（关键词直命中分段）+ topic_context（闸门主题块关联分段）。
映射只描述「医生在哪些时间说过相关的话」，绝不生成或改写医生的话。
"""
import json
import re
import sys
import time
from pathlib import Path

BASE = Path("/domain/supoclip-medical-workbench/backend")
TASKS = Path("/domain/supoclip-medical-workbench/tasks")
PROJECT = "timeline_a01488a3894a"
SOURCE_ID = "2070f41f051ac5b2"
TRANSCRIPT_V2 = TASKS / "transcripts" / SOURCE_ID / "v2.json"
SPEAKER = TASKS / "transcripts" / SOURCE_ID / "speaker_labels.json"
SNAPSHOT = BASE / "medical_kb_seed" / "active_snapshot.json"
OUT_DIR = TASKS / "projects" / PROJECT

RE_ADVICE = re.compile(r"建议|应该|需要|尽快|及时|最好|记得|一定要|尽早|要到|复查|来做|去医院")
RE_RISK = re.compile(r"风险|担心|警惕|注意|小心|严重|恶变|转移|复发|危险|变化")
RE_CONCL = re.compile(r"就是|指的是|说明|提示|意味着|属于|叫做|主要是|关键在于")
RE_EXPLAIN = re.compile(r"因为|所以|原因|原理|一般来说|通常|意思是|也就是说")


def time_str(ms: float) -> str:
    s = int(round(ms / 1000.0))
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def role_of(text: str) -> str:
    if RE_ADVICE.search(text):
        return "行动建议"
    if RE_RISK.search(text):
        return "风险提示"
    if RE_CONCL.search(text):
        return "结论"
    if RE_EXPLAIN.search(text):
        return "解释"
    return "其他"


def main() -> None:
    v2 = json.loads(TRANSCRIPT_V2.read_text(encoding="utf-8"))
    segments = list(v2.get("asr_segments") or [])
    speaker_doc = json.loads(SPEAKER.read_text(encoding="utf-8"))
    speaker = speaker_doc.get("labels") or {}
    snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    entries = snapshot["entries"]
    snapshot_sha = snapshot["meta"]["snapshot_sha256"]

    # ---- 片段库 ----
    library = []
    for seg in segments:
        sid = str(seg.get("segment_id") or "")
        text = str(seg.get("text") or "").strip()
        s_ms, e_ms = float(seg.get("start_ms") or 0), float(seg.get("end_ms") or 0)
        library.append({
            "segment_id": sid,
            "start_ms": s_ms, "end_ms": e_ms,
            "time": f"{time_str(s_ms)}–{time_str(e_ms)}",
            "text": text,
            "speaker": speaker.get(sid, "未标注"),
            "role": role_of(text),
        })
    labeled_by = "heuristic-v1（规则启发式：行动建议/风险提示/结论/解释，未做人工复核）"
    lib_doc = {
        "schema": "medical_segment_library/1",
        "project_id": PROJECT,
        "transcript": f"{SOURCE_ID}/v2（asr_segments，医学纠错后）",
        "segment_count": len(library),
        "speaker_source": "speaker_labels.json overlay（60 段已标注，其余未标注）",
        "role_labeled_by": labeled_by,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "segments": library,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    lib_path = OUT_DIR / "medical_segment_library.json"
    lib_path.write_text(json.dumps(lib_doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"segment library: {lib_path} ({len(library)} segments)")

    # ---- 映射 ----
    NEGATION = re.compile(r"(没有|没|无|不会|否认|看不出)[^，。；！？]{0,4}$")

    def negated(text: str, kw: str) -> bool:
        """命中关键词前若紧跟否定词（如「没有变化」），不计为支持。"""
        start = 0
        while True:
            i = text.find(kw, start)
            if i < 0:
                return False
            if NEGATION.search(text[:i]):
                return True
            start = i + len(kw)

    # 主题块关联：闸门 unit.matched[].id → entry 继承 unit.segment_ids（topic_context）
    sys_path_setup = str(BASE)
    if sys_path_setup not in sys.path:
        sys.path.insert(0, sys_path_setup)
    from src import clip_medical_gate as gate
    analysis_path = TASKS / "analysis" / "c6cd115b51e348e89ec6aa8a13f9e583.json"
    analysis = json.loads(analysis_path.read_text(encoding="utf-8")) if analysis_path.is_file() else {}
    profile = gate.run_gate(segments, analysis)
    unit_context: dict[str, list[dict]] = {}
    for unit in profile.get("unit_annotations") or []:
        seg_ids = [str(s) for s in (unit.get("segment_ids") or [])]
        for m in unit.get("matched") or []:
            eid = str(m.get("id") or "")
            if eid:
                unit_context.setdefault(eid, []).append({
                    "unit_title": str(unit.get("title") or ""),
                    "level": str(unit.get("level") or ""),
                    "score": float(unit.get("score") or 0.0),
                    "segment_ids": seg_ids,
                })

    seg_by_id = {s["segment_id"]: s for s in library}
    mappings = []
    for e in entries:
        kws = [str(k) for k in (e.get("keywords") or []) if str(k).strip()]
        direct = []
        for s in library:
            text = s["text"]
            hits = sorted({k for k in kws if k in text and not negated(text, k)})
            if hits:
                direct.append({"segment": s, "hits": hits})
        direct.sort(key=lambda d: (-len(d["hits"]), d["segment"]["start_ms"]))
        seg_rows = [{"segment_id": d["segment"]["segment_id"],
                     "time": d["segment"]["time"],
                     "start_ms": d["segment"]["start_ms"], "end_ms": d["segment"]["end_ms"],
                     "speaker": d["segment"]["speaker"], "role": d["segment"]["role"],
                     "hit_keywords": d["hits"],
                     "text": d["segment"]["text"]}
                    for d in direct[:8]]
        ctx = unit_context.get(e["id"]) or []
        ctx_seg_ids = []
        for c in ctx:
            for sid in c["segment_ids"]:
                if sid not in {r["segment_id"] for r in seg_rows} and sid not in ctx_seg_ids:
                    ctx_seg_ids.append(sid)
        ctx_rows = [{"segment_id": sid, "time": seg_by_id[sid]["time"] if sid in seg_by_id else "",
                     "speaker": seg_by_id[sid]["speaker"] if sid in seg_by_id else "未标注",
                     "role": seg_by_id[sid]["role"] if sid in seg_by_id else "其他",
                     "text": seg_by_id[sid]["text"] if sid in seg_by_id else ""} for sid in ctx_seg_ids[:10]]
        mappings.append({
            "entry_id": e["id"],
            "topic": " > ".join(e.get("topic_path") or []),
            "public_question": e.get("public_question") or "",
            "statement": e.get("statement") or "",
            "direct_segments": seg_rows,
            "direct_hit_count": len(direct),
            "topic_context_units": [{"title": c["unit_title"], "level": c["level"],
                                     "score": round(c["score"], 3)} for c in ctx],
            "topic_context_segments": ctx_rows,
        })
    mapped = sum(1 for m in mappings if m["direct_hit_count"] > 0)
    doc = {
        "schema": "medical_kb_mapping/1",
        "project_id": PROJECT,
        "transcript": f"{SOURCE_ID}/v2",
        "kb_snapshot_sha256": snapshot_sha,
        "kb_entry_count": len(entries),
        "entries_with_direct_hits": mapped,
        "method": "direct=关键词在分段文本直命中（按命中数+时间排序，取前8）；"
                  "topic_context=闸门主题块关联分段（模型敏感度通道）",
        "note": "映射只描述医生在哪些时间说过相关的话，绝不生成或改写医生的话；"
                "direct 命中依赖字面关键词，口语化表达需结合 topic_context 使用",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "mappings": mappings,
    }
    map_path = OUT_DIR / "medical_kb_mapping.json"
    map_path.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"mapping: {map_path} ({len(mappings)} entries, {mapped} with direct hits)")
    for m in mappings:
        if m["direct_hit_count"]:
            ex = m["direct_segments"][0]
            print(f"  {m['entry_id']} {m['public_question'][:18]} → {ex['time']} [{ex['speaker']}] hits={ex['hit_keywords']}")


if __name__ == "__main__":
    main()
