import "@testing-library/jest-dom/vitest";
import { afterAll, afterEach, beforeAll } from "vitest";
import { cleanup } from "@testing-library/react";
import { setupServer } from "msw/node";

export const server = setupServer();

// jsdom 没有实现 ResizeObserver，而用到尺寸观测的组件（如变换舞台 TransformStage）在
// 挂载时就会 new ResizeObserver(...)，缺失会让整组用例直接抛 ReferenceError。
// 这里提供最小桩：只保证接口存在，尺寸计算在测试里由组件自身的默认值兜底。
if (!("ResizeObserver" in globalThis)) {
  class ResizeObserverStub {
    observe(): void {}
    unobserve(): void {}
    disconnect(): void {}
  }
  (globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = ResizeObserverStub;
}


beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  cleanup();
  server.resetHandlers();
});
afterAll(() => server.close());
