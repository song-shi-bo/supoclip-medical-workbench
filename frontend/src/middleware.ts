import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

const PUBLIC_FILE = /\.(.*)$/;

// 注意：本项目使用 src/ 目录，middleware 必须放在 src/ 下才会生效
// （根目录 middleware.ts 会被 Next.js 静默忽略）。
export function middleware(request: NextRequest) {
  // 本地工作本部署：根路径直接进剪辑工作台（如需恢复营销首页，删除此块即可）
  if (request.nextUrl.pathname === "/") {
    const studio = request.nextUrl.clone();
    studio.pathname = "/studio";
    studio.search = "";
    return NextResponse.redirect(studio);
  }

  const isLandingOnlyModeEnabled =
    process.env.NEXT_PUBLIC_LANDING_ONLY_MODE === "true";

  if (!isLandingOnlyModeEnabled) {
    return NextResponse.next();
  }

  const { pathname } = request.nextUrl;

  if (
    pathname.startsWith("/_next") ||
    pathname.startsWith("/api/billing/webhook") ||
    PUBLIC_FILE.test(pathname)
  ) {
    return NextResponse.next();
  }

  if (pathname.startsWith("/api")) {
    return NextResponse.json(
      { error: "SupoClip is in landing-page-only mode." },
      { status: 503 }
    );
  }

  const url = request.nextUrl.clone();
  url.pathname = "/";
  url.search = "";
  return NextResponse.redirect(url);
}

export const config = {
  matcher: "/:path*",
};
