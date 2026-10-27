// 多账号阶段一：测试邮箱 API（开发/测试工具，替代 SMTP）。
// 仅当 ENABLE_TEST_MAIL_ROUTES=true 时可用；生产环境未设置该变量，
// 本路由恒返回 404，不暴露任何数据。

import { readFile } from "node:fs/promises";
import { NextResponse } from "next/server";
import path from "node:path";
import { testMailerConfigured, type TestMail } from "@/lib/test-mailer";

function outboxPath(): string {
  return (
    process.env.TEST_MAIL_OUTBOX ??
    path.join(process.cwd(), ".test-mailbox", "outbox.jsonl")
  );
}

export async function GET() {
  if (!testMailerConfigured()) {
    return NextResponse.json({ error: "not found" }, { status: 404 });
  }

  let entries: TestMail[] = [];
  try {
    const raw = await readFile(outboxPath(), "utf8");
    entries = raw
      .split("\n")
      .filter((line) => line.trim().length > 0)
      .map((line) => JSON.parse(line) as TestMail)
      .reverse(); // 最新在前
  } catch {
    entries = []; // outbox 尚不存在 = 还没有测试邮件
  }

  return NextResponse.json({ mails: entries.slice(0, 50) });
}

export async function DELETE() {
  if (!testMailerConfigured()) {
    return NextResponse.json({ error: "not found" }, { status: 404 });
  }

  // 清空 outbox：测试前重置状态用（写入空文件）
  const { writeFile, mkdir } = await import("node:fs/promises");
  const file = outboxPath();
  await mkdir(path.dirname(file), { recursive: true });
  await writeFile(file, "", "utf8");
  return NextResponse.json({ mails: [] });
}
