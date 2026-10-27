// 认证页面统一站内路径（多账号阶段 1）。
// 公网入口只路由 /studio 前缀，因此认证页面统一走 /studio/* 变体，
// 由 next.config.ts rewrites 映射到根级页面与 /api/auth/*；
// 隔离环境（3207）同样适用，双环境行为一致。

export const AUTH_PATHS = {
  signIn: "/studio/sign-in",
  signUp: "/studio/sign-up",
  verifyEmail: "/studio/verify-email",
  forgotPassword: "/studio/forgot-password",
  resetPassword: "/studio/reset-password",
} as const;
