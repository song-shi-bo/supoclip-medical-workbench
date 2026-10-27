import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  output: 'standalone',
  // 多账号隔离实例（3207）与本线（3107）共用同一项目目录：
  // dev 编译缓存必须隔离，否则 NEXT_PUBLIC_* 内联值会通过共享 .next 缓存互相污染。
  // 3107 保持默认 .next；3207 以 NEXT_DIST_DIR=.next-3207 启动。
  ...(process.env.NEXT_DIST_DIR ? { distDir: process.env.NEXT_DIST_DIR } : {}),
  // Skip ESLint during builds (generated Prisma code causes lint errors)
  eslint: {
    ignoreDuringBuilds: true,
  },
  // Skip TypeScript errors during builds for now
  typescript: {
    ignoreBuildErrors: false,
  },
  async rewrites() {
    // 多账号隔离环境：3207 以 VIDEO_API_ORIGIN=http://127.0.0.1:8207 启动时，
    // /video-api 转发到隔离后端；生产/共线（3107）未设置该变量 → 默认 8000，行为不变。
    const videoApiOrigin = process.env.VIDEO_API_ORIGIN || "http://127.0.0.1:8000";
    return [
      ...["studio", "api/jobs", "api/uploads"].map((path) => ({
        source: `/video-api/${path}/:path*`,
        destination: `${videoApiOrigin}/${path}/:path*`,
      })),
      // 多账号阶段 1：认证 UI 挂在 /studio 前缀下（公网入口仅路由 /studio 前缀，
      // 宿主路由不动即可达）。rewrite 后实际命中的仍是根级页面与 Better Auth 端点。
      ...["sign-in", "sign-up", "verify-email", "forgot-password", "reset-password"].map(
        (path) => ({
          source: `/studio/${path}`,
          destination: `/${path}`,
        })
      ),
      {
        source: "/studio/api/auth/:path*",
        destination: "/api/auth/:path*",
      },
      {
        source: "/js/script.js",
        destination: "https://datafa.st/js/script.js",
      },
      {
        source: "/api/events",
        destination: "https://datafa.st/api/events",
      },
    ];
  },
};

export default nextConfig;
