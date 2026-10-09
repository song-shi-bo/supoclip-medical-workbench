import { NextResponse } from "next/server";

export const runtime = "nodejs";
export const maxDuration = 120;

const backendUrl = () => (
  process.env.BACKEND_INTERNAL_URL ||
  process.env.NEXT_PUBLIC_API_URL ||
  "http://localhost:8000"
).replace(/\/$/, "");

async function proxy(request: Request, projectId: string) {
  try {
    const upstream = await fetch(`${backendUrl()}/studio/timeline/${encodeURIComponent(projectId)}`, {
      method: request.method,
      headers: request.method === "PUT" ? { "Content-Type": "application/json" } : undefined,
      body: request.method === "PUT" ? await request.text() : undefined,
      cache: "no-store",
    });
    return new NextResponse(await upstream.text(), {
      status: upstream.status,
      headers: { "Content-Type": upstream.headers.get("content-type") || "application/json" },
    });
  } catch {
    return NextResponse.json({ detail: "本机时间线服务未连接，请确认 Docker 服务正在运行。" }, { status: 502 });
  }
}

export async function GET(request: Request, context: { params: Promise<{ projectId: string }> }) {
  return proxy(request, (await context.params).projectId);
}

export async function PUT(request: Request, context: { params: Promise<{ projectId: string }> }) {
  return proxy(request, (await context.params).projectId);
}
