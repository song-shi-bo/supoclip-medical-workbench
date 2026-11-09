"""docs/AUTOEDIT_WORKBENCH.md 事实快照漂移检测（2026-11-08）。

代码变了而文档快照没刷新 → 本测试红，提示跑 `python3 docs/generate_workbench_doc.py`。
（pre-push 钩子会在推送前自动刷新；本测试是本地开发期的第二道保险。）
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
GENERATOR = REPO / "docs" / "generate_workbench_doc.py"


def test_workbench_doc_snapshot_is_fresh():
    assert GENERATOR.is_file(), f"生成器缺失: {GENERATOR}"
    result = subprocess.run(
        [sys.executable, str(GENERATOR), "--check"],
        cwd=REPO, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, (
        "docs/AUTOEDIT_WORKBENCH.md 代码事实快照已过期——"
        "运行 `python3 docs/generate_workbench_doc.py` 刷新后随改动一起提交。\n"
        + result.stderr
    )
