import { describe, expect, it } from "vitest";
import { safeInternalRedirect } from "./redirect";

describe("safeInternalRedirect", () => {
  it("allows plain internal paths", () => {
    expect(safeInternalRedirect("/studio")).toBe("/studio");
    expect(safeInternalRedirect("/studio?project=abc")).toBe("/studio?project=abc");
    expect(safeInternalRedirect("/list")).toBe("/list");
  });

  it("falls back on empty or relative targets", () => {
    expect(safeInternalRedirect(null)).toBe("/");
    expect(safeInternalRedirect(undefined)).toBe("/");
    expect(safeInternalRedirect("")).toBe("/");
    expect(safeInternalRedirect("studio")).toBe("/");
  });

  it("blocks protocol-relative and external URLs", () => {
    expect(safeInternalRedirect("//evil.example.com")).toBe("/");
    expect(safeInternalRedirect("https://evil.example.com")).toBe("/");
    expect(safeInternalRedirect("javascript:alert(1)")).toBe("/");
  });

  it("blocks control characters and backslash tricks", () => {
    expect(safeInternalRedirect("/\\evil")).toBe("/");
    expect(safeInternalRedirect("/a\u0000b")).toBe("/");
  });

  it("honors a custom fallback", () => {
    expect(safeInternalRedirect("//evil", "/studio")).toBe("/studio");
  });
});
