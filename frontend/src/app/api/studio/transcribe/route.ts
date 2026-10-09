import { NextResponse } from "next/server";

export const runtime = "nodejs";
export const maxDuration = 900;

/**
 * Local Studio proxy. It intentionally has no account-session requirement:
 * the backend is bound to localhost by docker-compose and this route is used
 * only by the local visual clipping workspace.
 */
export async function POST(request: Request) {
  const formData = await request.formData();
  const video = formData.get("video");
  if (!(video instanceof File)) {
    return NextResponse.json({ error: "缺少视频文件" }, { status: 400 });
  }

  const apiUrl = (
    process.env.BACKEND_INTERNAL_URL ||
    process.env.NEXT_PUBLIC_API_URL ||
    "http://localhost:8000"
  ).replace(/\/$/, "");
  const upstreamForm = new FormData();
  upstreamForm.append("video", video, video.name);

  try {
    const upstream = await fetch(`${apiUrl}/studio/transcribe`, {
      method: "POST",
      body: upstreamForm,
      signal: AbortSignal.timeout(15 * 60 * 1000),
    });
    const payload = await upstream.text();
    return new NextResponse(payload, {
      status: upstream.status,
      headers: { "Content-Type": upstream.headers.get("content-type") || "application/json" },
    });
  } catch {
    return NextResponse.json(
      { error: "本机转写服务未连接，请确认 Docker 服务正在运行。" },
      { status: 502 },
    );
  }
}
