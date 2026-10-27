"use client";

import { SessionGate } from "@/components/auth/session-gate";
import { StudioAccountBar } from "@/components/auth/studio-account-bar";

// 多账号阶段一：工作台入口会话门禁（仅隔离开发/测试环境启用）。
// - NEXT_PUBLIC_SESSION_GATE_ENABLED=true 时：
//   · 无会话（或会话过期）→ 跳 /studio/sign-in 并携带回跳地址与原因（expired / signed-out）
//   · 开启邮箱验证时，未验证账号在此被拦截，不渲染工作台（上传/渲染入口不可见）
// - 开关关闭（生产/共线 3107 默认）：门禁不生效，保持接入前行为，避免 DSH-only 用户被锁死；
//   但右下角账户条始终渲染：未登录显示 Sign in 入口（登录页在 /studio/sign-in），
//   已登录显示邮箱 + Sign out。
// 3107 是 dev 服务会热加载工作区代码，/studio/sign-in 与 /studio/api/auth/* 已通过
// rewrites 挂到 /studio 前缀下公网可达；门禁启用仍留待 M4/M4.5（账号就绪后）。
export default function StudioLayout({ children }: { children: React.ReactNode }) {
  return (
    <SessionGate>
      {children}
      <StudioAccountBar />
    </SessionGate>
  );
}
