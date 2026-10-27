"""方案 b 交付物测试：ensure_workspace 管理命令（CLI）。

**隔离边界**：ACL 全部位于 tmp_path；不触碰生产 ACL。
覆盖：ensure 幂等（两次调用同 workspace、配额冻结 (0,0)）、unfreeze 动作、
非法 user_id 拒绝、JSON 输出格式。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[2]
DOMAIN = "bdd01c9e-1ace-47b1-8288-bee275fa4ad0"
UID = "55555555-5555-4555-8555-555555555555"


def run_cli(acl: Path, *args: str) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "src.studio_workspace", *args],
        capture_output=True, text=True, cwd=str(BACKEND),
        env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(BACKEND)},
    )
    return proc.returncode, (proc.stdout + proc.stderr).strip()


@pytest.fixture()
def acl(tmp_path) -> Path:
    return tmp_path / "cli-acl.sqlite3"


def test_cli_ensure_is_idempotent_and_outputs_json(acl):
    out1 = run_cli(acl, "ensure", str(acl), UID, "--domain", DOMAIN)
    assert out1[0] == 0
    data1 = json.loads(out1[1].splitlines()[0])
    assert data1["action"] == "ensure"
    assert data1["workspace_id"].startswith("ws_")
    assert data1["owner_subject"] == UID
    assert (data1["bytes_limit"], data1["concurrent_limit"]) == (0, 0)  # D14 冻结

    out2 = run_cli(acl, "ensure", str(acl), UID, "--domain", DOMAIN)
    data2 = json.loads(out2[1].splitlines()[0])
    assert data2["workspace_id"] == data1["workspace_id"]  # 幂等

    import sqlite3
    conn = sqlite3.connect(str(acl))
    assert conn.execute("SELECT COUNT(*) FROM workspaces").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM workspace_members").fetchone()[0] == 1
    conn.close()


def test_cli_unfreezes_existing_quota(acl):
    run_cli(acl, "ensure", str(acl), UID, "--domain", DOMAIN)
    code, out = run_cli(acl, "unfreeze", str(acl), UID, "--domain", DOMAIN,
                        "--bytes-limit", str(100 * 1024**3), "--concurrent-limit", "2")
    assert code == 0
    data = json.loads(out.splitlines()[0])
    assert data["action"] == "unfreeze"
    assert (data["bytes_limit"], data["concurrent_limit"]) == (100 * 1024**3, 2)

    # 解冻后重复 ensure 不得回写冻结值
    run_cli(acl, "ensure", str(acl), UID, "--domain", DOMAIN)
    import sqlite3
    conn = sqlite3.connect(str(acl))
    row = conn.execute("SELECT bytes_limit, concurrent_limit FROM quotas WHERE subject=?", (UID,)).fetchone()
    assert (row[0], row[1]) == (100 * 1024**3, 2)
    conn.close()


def test_cli_rejects_invalid_user_id(acl):
    code, out = run_cli(acl, "ensure", str(acl), "bad id with spaces", "--domain", DOMAIN)
    assert code != 0
    assert "invalid user_id" in (out + "")
