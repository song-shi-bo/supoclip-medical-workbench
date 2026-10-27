/**
 * 内部更新条目（开发日志）接口。
 *
 * 内部条目不进客户端 bundle：updates.internal.json 仅在此服务端路由 import，
 * 需要登录会话（Better Auth）才能读取，保证「内部」不只是视觉过滤。
 */
import { headers } from "next/headers";
import { NextResponse } from "next/server";
import { auth } from "@/lib/auth";
import type { UpdateEntry } from "@/lib/updates";
import internalUpdatesJson from "../../../../../data/updates.internal.json";

const internalUpdates = internalUpdatesJson as unknown as UpdateEntry[];

export async function GET() {
  const session = await auth.api.getSession({ headers: await headers() });
  if (!session?.user) {
    return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  }
  return NextResponse.json(
    { entries: internalUpdates },
    { headers: { "cache-control": "no-store" } },
  );
}
