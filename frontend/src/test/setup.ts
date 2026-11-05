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


beforeAll(() => {
  server.listen({ onUnhandledRequest: "error" });
  // 组件在浏览器里用 AbortController 中止串片请求；jsdom 环境里 MSW 拦截层的
  // fetch 信号校验与全局 AbortController 分属不同实现域，`init.signal` 会因
  // 跨域 instanceof 被拒（"Expected signal … to be an instance of AbortSignal"），
  // 与组件逻辑无关。这里包一层：仅当命中该校验错误时去掉 signal 重发一次。
  // 请求中止语义在测试里由组件的 clip_id 防串片校验兜底（本就是双保险）。
  const intercepted = globalThis.fetch?.bind(globalThis);
  if (intercepted) {
    globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
      try {
        return await intercepted(input, init);
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        if (init?.signal && message.includes("instance of AbortSignal")) {
          const rest = { ...init } as RequestInit;
          delete (rest as { signal?: AbortSignal }).signal;
          return await intercepted(input, rest);
        }
        throw error;
      }
    }) as typeof fetch;
  }
});
afterEach(() => {
  cleanup();
  server.resetHandlers();
});
afterAll(() => server.close());
