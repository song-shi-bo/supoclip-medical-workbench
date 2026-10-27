"""多账号阶段二：workspace 与冻结配额（D14 两段式 · M2 §2.1 实施包落地）。

**隔离边界**：
* 本模块只在隔离环境被调用（`STUDIO_AUTH_BACKEND=better-auth` 分支，见 studio_access.py）；
  生产默认分支不经过任何本模块函数。
* DDL 与 M2 前置材料包 §2.1 完全一致（IF NOT EXISTS 幂等）；quotas 表无 DDL——
  冻结行 (0,0) 由 ensure_workspace 应用代码写入（D14），解冻为显式 UPDATE。

语义（全部已在阶段一测试实证，本模块不改变 AccessLedger 行为）：
* quotas (0,0)：重操作 reserve → 429（存储/并发）；light（查看/下载/删除/台账写）不经 reserve。
* 解冻 UPDATE (107374182400, 2)：重操作恢复；100GiB 是用量上限而非预占。
* 授权主体 = Better Auth user.id（principal.subject），与账号名/邮箱无关。
"""

from __future__ import annotations

import os
import secrets
import sqlite3
import time
from typing import Any

# D10 终版配额：100GiB / 2 并发（上限，非预占）
OPEN_QUOTA: tuple[int, int] = (100 * 1024**3, 2)
# 方案 B 开关（多账号阶段五准备）：ACCOUNT_AUTO_OPEN_QUOTA=true 时，新建 workspace
# 直接写入 (100GiB, 2)（跳过冻结态）——"注册后无需逐人审批即可剪辑"。
# 默认 false：新账号维持冻结 (0,0)（方案 A / 生产现状）。既有配额行永不回写。
AUTO_OPEN_QUOTA = os.getenv("ACCOUNT_AUTO_OPEN_QUOTA", "false").strip().lower() in ("1", "true", "yes")
# D14 冻结态：登录/查看/下载/删除可用，上传/渲染等重操作拒绝
FROZEN_QUOTA: tuple[int, int] = (0, 0)

# M2 前置材料包 §2.1 原文 DDL（幂等）
WORKSPACE_DDL = """
CREATE TABLE IF NOT EXISTS workspaces(
  domain TEXT NOT NULL,
  workspace_id TEXT NOT NULL,        -- 'ws_' + 12hex，应用层生成
  owner_subject TEXT NOT NULL,       -- better-auth user.id（路径 B）/ 平台 subject（兼容）
  created_ms INTEGER NOT NULL,
  PRIMARY KEY(domain, workspace_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_workspaces_owner ON workspaces(domain, owner_subject);

CREATE TABLE IF NOT EXISTS workspace_members(
  domain TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  subject TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('owner','member')),
  created_ms INTEGER NOT NULL,
  PRIMARY KEY(domain, workspace_id, subject)
);
CREATE INDEX IF NOT EXISTS idx_workspace_members_subject ON workspace_members(domain, subject);
"""


def ensure_workspace_tables(conn: sqlite3.Connection) -> None:
    """幂等创建 workspaces / workspace_members（M2 §2.1）。对既有五表零改动。"""
    conn.executescript(WORKSPACE_DDL)


def _new_workspace_id() -> str:
    return f"ws_{secrets.token_hex(6)}"  # 'ws_' + 12hex（M2 §2.1 约定）


def ensure_workspace(conn: sqlite3.Connection, principal: dict[str, Any]) -> dict[str, Any]:
    """首次登录幂等创建个人 workspace + owner 成员 + 冻结配额 (0,0)。

    幂等性：以 UNIQUE(domain, owner_subject) 为锚——重复调用返回既有 workspace，
    不产生重复行、不覆盖已解冻的配额。授权主体 = principal['subject']（Better Auth user.id）。
    """
    ensure_workspace_tables(conn)
    domain = principal["domain_id"]
    subject = str(principal["subject"])
    if not subject:
        raise ValueError("ensure_workspace: empty subject")
    now_ms = int(time.time() * 1000)

    conn.execute(
        "INSERT OR IGNORE INTO workspaces(domain, workspace_id, owner_subject, created_ms)"
        " VALUES(?, ?, ?, ?)",
        (domain, _new_workspace_id(), subject, now_ms),
    )
    row = conn.execute(
        "SELECT workspace_id, created_ms FROM workspaces WHERE domain=? AND owner_subject=?",
        (domain, subject),
    ).fetchone()
    workspace_id = row["workspace_id"] if isinstance(row, sqlite3.Row) else row[0]

    conn.execute(
        "INSERT OR IGNORE INTO workspace_members(domain, workspace_id, subject, role, created_ms)"
        " VALUES(?, ?, ?, 'owner', ?)",
        (domain, workspace_id, subject, now_ms),
    )
    # D14：默认冻结 (0,0)；方案 B 开关（ACCOUNT_AUTO_OPEN_QUOTA=true）时新账号直接
    # 写入 (100GiB, 2)。INSERT OR IGNORE——既有配额行（含已解冻）永不回写。
    initial_quota = OPEN_QUOTA if AUTO_OPEN_QUOTA else FROZEN_QUOTA
    conn.execute(
        "INSERT OR IGNORE INTO quotas(domain, subject, bytes_limit, concurrent_limit)"
        " VALUES(?, ?, ?, ?)",
        (domain, subject, initial_quota[0], initial_quota[1]),
    )
    return {"workspace_id": workspace_id, "owner_subject": subject,
            "frozen": not AUTO_OPEN_QUOTA, "auto_open": AUTO_OPEN_QUOTA}


def set_workspace_quota(
    conn: sqlite3.Connection,
    domain: str,
    subject: str,
    *,
    bytes_limit: int,
    concurrent_limit: int,
) -> None:
    """显式解冻/再冻结/调整配额（D14 两段式的管理动作；仅管理路径调用）。

    bytes_limit 是用量上限（used+reserved+new ≤ limit），不是预先占用。
    """
    conn.execute(
        "INSERT INTO quotas(domain, subject, bytes_limit, concurrent_limit) VALUES(?,?,?,?)"
        " ON CONFLICT(domain,subject) DO UPDATE SET bytes_limit=excluded.bytes_limit,"
        " concurrent_limit=excluded.concurrent_limit",
        (domain, subject, int(bytes_limit), int(concurrent_limit)),
    )


def unfreeze_workspace(conn: sqlite3.Connection, domain: str, subject: str) -> None:
    """解冻为 D10 终版 (100GiB, 2)。"""
    set_workspace_quota(conn, domain, subject, bytes_limit=OPEN_QUOTA[0], concurrent_limit=OPEN_QUOTA[1])


# ---------------------------------------------------------------------------
# 管理命令（方案 b 交付物）：为指定 Better Auth user.id 幂等创建三件套；
# 或对已有配额执行解冻。仅写入显式指定的 ACL 文件；生产 ACL 需另行批准后操作。
# 用法：
#   python -m src.studio_workspace ensure <acl_path> <user_id> [--domain <domain_id>]
#   python -m src.studio_workspace unfreeze <acl_path> <user_id> [--domain <domain_id>] #       [--bytes-limit N] [--concurrent-limit N]
# 输出：单行 JSON（便于脚本/记录）。
# ---------------------------------------------------------------------------

def _cli():
    import argparse
    import json
    import re

    parser = argparse.ArgumentParser(prog="python -m src.studio_workspace",
                                     description="workspace/配额 管理命令（幂等）")
    sub = parser.add_subparsers(dest="action", required=True)
    for name, help_text in (("ensure", "幂等创建 workspace + owner 成员 + 冻结配额 (0,0)"),
                            ("unfreeze", "将既有配额解冻为指定上限（默认 100GiB / 2）")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("acl_path", help="目标 SQLite ACL 文件路径")
        p.add_argument("user_id", help="Better Auth user.id（授权主体）")
        p.add_argument("--domain", default="bdd01c9e-1ace-47b1-8288-bee275fa4ad0",
                       help="域 ID（默认生产域）")
        p.add_argument("--bytes-limit", type=int, default=100 * 1024**3)
        p.add_argument("--concurrent-limit", type=int, default=2)
    args = parser.parse_args()

    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", args.user_id or ""):
        raise SystemExit("invalid user_id（仅限字母/数字/下划线/连字符，1–64 字符）")

    # 经 AccessLedger 连接：自动创建五张基表（规范 schema），再执行 workspace 三件套
    from .studio_access import AccessLedger
    with AccessLedger(args.acl_path).connect() as conn:
        principal = {"subject": args.user_id, "domain_id": args.domain, "expires_at_ms": 0}
        if args.action == "ensure":
            info = ensure_workspace(conn, principal)
            quota = conn.execute(
                "SELECT bytes_limit, concurrent_limit FROM quotas WHERE domain=? AND subject=?",
                (args.domain, args.user_id)).fetchone()
            out = {"action": "ensure", "workspace_id": info["workspace_id"],
                   "owner_subject": info["owner_subject"],
                   "bytes_limit": quota["bytes_limit"], "concurrent_limit": quota["concurrent_limit"]}
        else:
            set_workspace_quota(conn, args.domain, args.user_id,
                                bytes_limit=args.bytes_limit,
                                concurrent_limit=args.concurrent_limit)
            out = {"action": "unfreeze", "subject": args.user_id,
                   "bytes_limit": args.bytes_limit, "concurrent_limit": args.concurrent_limit}
        conn.commit()
        print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    _cli()
