// BGM 上传客户端的单测（契约 §5 POST …/audio/bgm multipart）。
//
// 用假的 XMLHttpRequest 观察真实实现构造的请求：URL、multipart 字段名
// （file / volume / fade_in_s / fade_out_s）、进度回调与 422 错误映射。
// （不在组件测试里走真实 XHR：jsdom 的 XHR 不会把 FormData 序列化成分片体。）

import { describe, expect, it, vi } from "vitest";

import { InspectorApiError, uploadBgm } from "./api";
import { CLIP_ID, PROJECT_ID } from "./test-utils";

type Sent = { url: string; body: FormData | null; headers: Record<string, string> };

function stubXhr(options: { status?: number; responseText?: string; fail?: boolean; progress?: boolean } = {}) {
  const sent: Sent = { url: "", body: null, headers: {} };
  class FakeXhr {
    upload: { onprogress: ((event: { lengthComputable: boolean; loaded: number; total: number }) => void) | null } = { onprogress: null };
    onerror: (() => void) | null = null;
    onload: (() => void) | null = null;
    status = options.status ?? 200;
    responseText = options.responseText ?? "{}";
    open(_method: string, url: string) { sent.url = url; }
    setRequestHeader(name: string, value: string) { sent.headers[name] = value; }
    send(body: FormData) {
      sent.body = body;
      this.upload.onprogress?.({ lengthComputable: true, loaded: 50, total: 100 });
      queueMicrotask(() => {
        if (options.fail) this.onerror?.();
        else this.onload?.();
      });
    }
  }
  vi.stubGlobal("XMLHttpRequest", FakeXhr as unknown as typeof XMLHttpRequest);
  return sent;
}

describe("uploadBgm", () => {
  it("POST 到合同路径，multipart 字段为 file / volume / fade_in_s / fade_out_s", async () => {
    const sent = stubXhr({ responseText: JSON.stringify({ clip_id: CLIP_ID, track: {}, audio_tracks: [], peaks: [] }) });
    const onProgress = vi.fn();
    const file = new File(["audio"], "bgm.mp3", { type: "audio/mpeg" });
    await uploadBgm("", PROJECT_ID, CLIP_ID, file, { volume: 0.8, fadeInS: 1.5, fadeOutS: 2, onProgress });

    expect(sent.url).toBe(`/studio/clips/${PROJECT_ID}/${CLIP_ID}/audio/bgm`);
    const body = sent.body as unknown as FormData;
    expect(body.get("file")).toBeInstanceOf(File);
    expect((body.get("file") as unknown as File).name).toBe("bgm.mp3");
    expect(body.get("volume")).toBe("0.8");
    expect(body.get("fade_in_s")).toBe("1.5");
    expect(body.get("fade_out_s")).toBe("2");
    expect(onProgress).toHaveBeenCalledWith(50);
    vi.unstubAllGlobals();
  });

  it("422 detail 的 E_ 码映射成可读原因", async () => {
    stubXhr({ status: 422, responseText: JSON.stringify({ detail: "E_AUDIO_FILE_TOO_LARGE: BGM 超过 200MB 上限" }) });
    const file = new File(["audio"], "big.mp3", { type: "audio/mpeg" });
    await expect(uploadBgm("", PROJECT_ID, CLIP_ID, file)).rejects.toMatchObject({
      name: "InspectorApiError",
      code: "E_AUDIO_FILE_TOO_LARGE",
      message: "BGM 超过 200MB 上限",
    });
    vi.unstubAllGlobals();
  });

  it("网络中断给出可读原因（不吞错）", async () => {
    stubXhr({ fail: true });
    const file = new File(["audio"], "bgm.mp3", { type: "audio/mpeg" });
    await expect(uploadBgm("", PROJECT_ID, CLIP_ID, file)).rejects.toBeInstanceOf(InspectorApiError);
    vi.unstubAllGlobals();
  });
});
