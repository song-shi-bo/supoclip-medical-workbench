#!/usr/bin/env python3
"""docs/AUTOEDIT_WORKBENCH.md 的事实快照自动生成器（2026-11-08）。

只更新文档末尾「代码事实快照」区块（BEGIN/END:AUTO-SNAPSHOT 标记之间），
手写叙述一律不动。事实全部来自代码扫描，不做任何人工推断：
* backend/src/api/routes/*.py 的 HTTP 端点（method+path）
* studio_job_worker 的任务阶段函数（run_*/_run_* + docstring 首行）
* studio_supervisor 托管的服务清单
* Inspector 标签清单（frontend INSPECTOR_TABS）
* video-brain app 模块（可选，路径不存在则静默跳过）
* git HEAD

用法：
    python3 docs/generate_workbench_doc.py            # 就地更新快照区块
    python3 docs/generate_workbench_doc.py --check    # 只检测漂移：过期 exit 1（供钩子/CI/pytest）
"""
from __future__ import annotations

import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DOC = REPO / "docs" / "AUTOEDIT_WORKBENCH.md"
BEGIN = "<!-- BEGIN:AUTO-SNAPSHOT（本区块由 docs/generate_workbench_doc.py 生成，勿手改） -->"
END = "<!-- END:AUTO-SNAPSHOT -->"

ROUTE_RE = re.compile(r'@router\.(get|post|put|delete)\("([^"]+)"')
STAGE_RE = re.compile(r"^def ((?:_?run)_\w+)\(([^)]*)", re.M)
SERVICE_RE = re.compile(r"\('(\w+)',\s*[^,]+,\s*\[")
TAB_RE = re.compile(r'\{ id: "(\w+)", label: "([^"]+)"')


def _routes() -> list[str]:
    out: list[str] = []
    for path in sorted((REPO / "backend/src/api/routes").glob("*.py")):
        hits = [f"{m.upper()} {p}" for m, p in ROUTE_RE.findall(path.read_text(encoding="utf-8"))]
        if hits:
            out.append(f"- `{path.stem}.py`（{len(hits)}）：" + "、".join(f"`{h}`" for h in hits))
    return out or ["- （未扫描到路由）"]


def _worker_stages() -> list[str]:
    path = REPO / "backend/src/studio_job_worker.py"
    text = path.read_text(encoding="utf-8")
    out: list[str] = []
    for match in STAGE_RE.finditer(text):
        name = match.group(1)
        tail = text[match.end():match.end() + 400]
        doc = ""
        m = re.search(r'"""(.+?)(?:。|\n)', tail, re.S)
        if m:
            doc = m.group(1).strip().replace("\n", " ")[:60]
        out.append(f"- `{name}`——{doc}")
    return out or ["- （未扫描到阶段函数）"]


def _services() -> list[str]:
    text = (REPO / "backend/src/studio_supervisor.py").read_text(encoding="utf-8")
    names = SERVICE_RE.findall(text)
    return ["、".join(f"`{n}`" for n in names) or "（未扫描到）"]


def _tabs() -> list[str]:
    path = REPO / "frontend/src/components/studio/inspector/inspector-pane.tsx"
    if not path.is_file():
        return ["（frontend 不在当前检出）"]
    hits = TAB_RE.findall(path.read_text(encoding="utf-8"))
    return ["、".join(f"{label}({tid})" for tid, label in hits) or "（未扫描到）"]


def _head() -> str:
    try:
        rev = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO,
                             capture_output=True, text=True, timeout=10)
        return rev.stdout.strip() or "?"
    except Exception:
        return "?"


def build_snapshot() -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        BEGIN,
        "",
        "## 附录 · 代码事实快照（自动生成）",
        "",
        f"- 生成时间：{now} · 代码版本：`{_head()}` · 生成器：`docs/generate_workbench_doc.py`（`--check` 可做漂移检测）",
        "",
        "### HTTP 端点（backend/src/api/routes）",
        "",
        *_routes(),
        "",
        "### worker 任务阶段（studio_job_worker.py）",
        "",
        *_worker_stages(),
        "",
        "### supervisor 托管服务",
        "",
        *_services(),
        "",
        "### Inspector 标签",
        "",
        *_tabs(),
        "",
        END,
    ]
    return "\n".join(lines)


_VOLATILE_RE = re.compile(r"- 生成时间：.*")


def _normalize(text: str) -> str:
    """归一化易变字段（生成时间/HEAD）：CI 与本机环境不同也能稳定比较。"""
    return _VOLATILE_RE.sub("- 生成时间：（略）", text)


def update_doc(check: bool = False) -> int:
    if not DOC.is_file():
        print(f"doc missing: {DOC}", file=sys.stderr)
        return 2
    text = DOC.read_text(encoding="utf-8")
    snapshot = build_snapshot()
    if BEGIN in text and END in text:
        pre = text.split(BEGIN, 1)[0]
        post = text.split(END, 1)[1]
        new_text = pre + snapshot + post
    else:
        new_text = text.rstrip() + "\n\n" + snapshot + "\n"
    if _normalize(new_text) == _normalize(text):
        print("doc snapshot up-to-date")
        return 0
    if check:
        print("doc snapshot STALE — 运行 python3 docs/generate_workbench_doc.py 更新", file=sys.stderr)
        return 1
    DOC.write_text(new_text, encoding="utf-8")
    print(f"doc snapshot updated: {DOC}")
    return 0


if __name__ == "__main__":
    sys.exit(update_doc(check="--check" in sys.argv))
