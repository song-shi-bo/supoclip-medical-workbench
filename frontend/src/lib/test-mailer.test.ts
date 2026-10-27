import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { deliverTestMail } from "./test-mailer";

// 测试邮件工具：只写本地 outbox，绝不发起网络请求

let outboxFile: string;

beforeEach(async () => {
  const dir = await mkdtemp(path.join(tmpdir(), "test-mailer-"));
  outboxFile = path.join(dir, "outbox.jsonl");
  vi.stubEnv("TEST_MAIL_OUTBOX", outboxFile);
});

afterEach(async () => {
  vi.unstubAllEnvs();
  await rm(path.dirname(outboxFile), { recursive: true, force: true });
});

describe("deliverTestMail", () => {
  it("appends a JSON line to the outbox without any network activity", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    const infoSpy = vi.spyOn(console, "info").mockImplementation(() => {});

    await deliverTestMail({
      kind: "verification",
      to: "u@example.com",
      subject: "Verify your email",
      url: "http://localhost:3000/api/auth/verify-email?token=t",
      token: "t",
    });

    const raw = await readFile(outboxFile, "utf8");
    const lines = raw.trim().split("\n");
    expect(lines).toHaveLength(1);
    const record = JSON.parse(lines[0]);
    expect(record).toMatchObject({
      kind: "verification",
      to: "u@example.com",
      token: "t",
    });
    expect(fetchSpy).not.toHaveBeenCalled();
    expect(infoSpy).toHaveBeenCalled();

    fetchSpy.mockRestore();
    infoSpy.mockRestore();
  });
});
