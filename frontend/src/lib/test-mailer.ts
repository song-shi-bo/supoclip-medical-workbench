// 多账号阶段一：测试邮件工具（无 SMTP）。
// Better Auth 的 sendVerificationEmail / sendResetPassword 回调在隔离环境
// 指向这里：把「邮件」追加写入本地 outbox 文件（JSON Lines），供
// /dev/test-mailbox 页面读取并点击其中的验证/重置链接。
// 绝不发起任何网络/SMTP 请求；生产环境不会配置对应的启用开关，也不会调用。

import { appendFile, mkdir } from "node:fs/promises";
import path from "node:path";

export type TestMailKind = "verification" | "reset-password";

export interface TestMail {
  id: string;
  kind: TestMailKind;
  to: string;
  subject: string;
  url: string;
  token: string;
  created_at: string;
}

function outboxPath(): string {
  return (
    process.env.TEST_MAIL_OUTBOX ??
    path.join(process.cwd(), ".test-mailbox", "outbox.jsonl")
  );
}

/** 测试邮件投递：写本地 outbox + 控制台输出，不发送真实邮件。 */
export async function deliverTestMail(mail: {
  kind: TestMailKind;
  to: string;
  subject: string;
  url: string;
  token: string;
}): Promise<void> {
  const record: TestMail = {
    id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
    created_at: new Date().toISOString(),
    ...mail,
  };

  const file = outboxPath();
  await mkdir(path.dirname(file), { recursive: true });
  await appendFile(file, `${JSON.stringify(record)}\n`, "utf8");

  // 控制台同时输出，便于无浏览器调试
  console.info(`[test-mailer] ${mail.kind} -> ${mail.to}\n  ${mail.url}`);
}

export function testMailerConfigured(): boolean {
  return process.env.ENABLE_TEST_MAIL_ROUTES === "true";
}

/**
 * 把 Better Auth 生成的 API 链接改写为前端页面路由，
 * 让测试邮箱里的链接直接走页面 UI（验证中/成功/失败状态可见）：
 * - /api/auth/verify-email?token=…      → /verify-email?token=…
 * - /api/auth/reset-password/<token>…   → /reset-password?token=…
 */
export function pageLinkFor(url: string): string {
  return url
    .replace("/api/auth/verify-email", "/verify-email")
    .replace(/\/api\/auth\/reset-password\/([^?]+)\?/, "/reset-password?token=$1&");
}
