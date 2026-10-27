import { createAuthClient } from "better-auth/react";
import { usernameClient } from "better-auth/client/plugins";

// usernameClient 仅在前端补充 signIn.username / isUsernameAvailable 等方法，
// 是否可用仍由服务端插件开关（AUTH_USERNAME_LOGIN）决定，客户端常驻无副作用。
export const authClient = createAuthClient({
  // 公网入口仅转发 /studio/*：客户端统一走 /studio/api/auth 前缀
  // （next.config.ts 已有 rewrite → /api/auth/*）。
  // 必须用 basePath 而非 baseURL——baseURL 强制绝对 URL（SSR 即抛
  // BetterAuthError，阶段 5 试点实测）；basePath 会与 env BETTER_AUTH_URL
  // 或浏览器同源 origin 正确拼接。
  basePath: "/studio/api/auth",
  plugins: [usernameClient()],
});

export const {
  signIn,
  signOut,
  signUp,
  useSession,
  // 多账号阶段一：邮箱验证 / 密码重置（对应 Better Auth 内建端点）
  verifyEmail,
  sendVerificationEmail,
  requestPasswordReset,
  resetPassword,
} = authClient;
