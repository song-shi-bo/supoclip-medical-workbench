import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// 测试邮箱 API 路由：仅 ENABLE_TEST_MAIL_ROUTES=true 时可用（生产恒 404）

let outboxFile: string;

beforeEach(async () => {
  const dir = await mkdtemp(path.join(tmpdir(), "test-mailbox-"));
  outboxFile = path.join(dir, "outbox.jsonl");
  vi.resetModules();
});

afterEach(async () => {
  vi.unstubAllEnvs();
  await rm(path.dirname(outboxFile), { recursive: true, force: true });
});

async function importRoute() {
  return import("@/app/api/dev/test-mailbox/route");
}

describe("GET /api/dev/test-mailbox", () => {
  it("returns 404 when test mail routes are disabled", async () => {
    vi.stubEnv("ENABLE_TEST_MAIL_ROUTES", "");
    const { GET } = await importRoute();
    const res = await GET();
    expect(res.status).toBe(404);
  });

  it("returns an empty list when the outbox does not exist yet", async () => {
    vi.stubEnv("ENABLE_TEST_MAIL_ROUTES", "true");
    vi.stubEnv("TEST_MAIL_OUTBOX", outboxFile);
    const { GET } = await importRoute();
    const res = await GET();
    expect(res.status).toBe(200);
    const data = await res.json();
    expect(data.mails).toEqual([]);
  });

  it("returns outbox entries newest first", async () => {
    vi.stubEnv("ENABLE_TEST_MAIL_ROUTES", "true");
    vi.stubEnv("TEST_MAIL_OUTBOX", outboxFile);
    await writeFile(
      outboxFile,
      [
        JSON.stringify({
          id: "a",
          kind: "verification",
          to: "u@example.com",
          subject: "Verify your email",
          url: "http://x/verify-email?token=a",
          token: "a",
          created_at: "2026-10-23T01:00:00.000Z",
        }),
        JSON.stringify({
          id: "b",
          kind: "reset-password",
          to: "u@example.com",
          subject: "Reset your password",
          url: "http://x/reset-password?token=b",
          token: "b",
          created_at: "2026-10-23T02:00:00.000Z",
        }),
      ].join("\n") + "\n",
      "utf8"
    );
    const { GET } = await importRoute();
    const res = await GET();
    const data = await res.json();
    expect(data.mails.map((m: { id: string }) => m.id)).toEqual(["b", "a"]);
  });
});

describe("DELETE /api/dev/test-mailbox", () => {
  it("clears the outbox when enabled", async () => {
    vi.stubEnv("ENABLE_TEST_MAIL_ROUTES", "true");
    vi.stubEnv("TEST_MAIL_OUTBOX", outboxFile);
    await writeFile(outboxFile, '{"id":"a"}\n', "utf8");
    const mod = await importRoute();
    const res = await mod.DELETE();
    expect(res.status).toBe(200);
    const raw = await readFile(outboxFile, "utf8");
    expect(raw).toBe("");
  });

  it("returns 404 when disabled", async () => {
    vi.stubEnv("ENABLE_TEST_MAIL_ROUTES", "");
    const mod = await importRoute();
    const res = await mod.DELETE();
    expect(res.status).toBe(404);
  });
});
