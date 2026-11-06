import { describe, expect, it } from "vitest";
import { safeInternalRedirect } from "./redirect";

describe("safeInternalRedirect", () => {
  it("allows plain internal paths", () => {
    expect(safeInternalRedirect("/studio")).toBe("/studio");
    expect(safeInternalRedirect("/studio?project=abc")).toBe("/studio?project=abc");
    expect(safeInternalRedirect("/list")).toBe("/list");
  });

  it("falls back on empty or relative targets", () => {
    // 缺省回 /studio：公网入口的 `/` 归 DSH 平台侧，不能作登录后落点（见 redirect.ts 注释）。
    expect(safeInternalRedirect(null)).toBe("/studio");
    expect(safeInternalRedirect(undefined)).toBe("/studio");
    expect(safeInternalRedirect("")).toBe("/studio");
    expect(safeInternalRedirect("studio")).toBe("/studio");
  });

  it("blocks protocol-relative and external URLs", () => {
    expect(safeInternalRedirect("//evil.example.com")).toBe("/studio");
    expect(safeInternalRedirect("https://evil.example.com")).toBe("/studio");
    expect(safeInternalRedirect("javascript:alert(1)")).toBe("/studio");
  });

  it("blocks control characters and backslash tricks", () => {
    expect(safeInternalRedirect("/\\evil")).toBe("/studio");
    expect(safeInternalRedirect("/a\u0000b")).toBe("/studio");
  });

  it("honors a custom fallback", () => {
    expect(safeInternalRedirect("//evil", "/studio")).toBe("/studio");
  });
});
