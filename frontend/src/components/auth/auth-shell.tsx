import { Scissors } from "lucide-react";

/**
 * 认证页统一外壳（阶段 5 后 UI 迭代，用户指定）：
 * 紫色渐变背景 linear-gradient(135deg, #7C3AED, #4C1D95, #2E1065)；
 * 左上角「AI剪辑工作台」品牌标识；登录框上方剪刀徽章（代表剪辑）。
 * sign-in / sign-up / verify-email / forgot-password / reset-password 共用。
 */
export function AuthShell({ children }: { children: React.ReactNode }) {
  return (
    <div
      className="relative min-h-screen"
      style={{ background: "linear-gradient(135deg, #7C3AED, #4C1D95, #2E1065)" }}
    >
      {/* 左上角品牌标识 */}
      <div className="absolute left-6 top-6 flex items-center gap-2.5">
        <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-white/15 ring-1 ring-white/25 backdrop-blur">
          <Scissors className="h-4 w-4 text-white" aria-hidden />
        </span>
        <span className="text-base font-semibold tracking-wide text-white">
          AI剪辑工作台
        </span>
      </div>

      {/* 中央：剪刀徽章 + 表单卡片 */}
      <div className="flex min-h-screen flex-col items-center justify-center px-4 py-16">
        <div
          className="mb-6 flex h-16 w-16 items-center justify-center rounded-2xl bg-white/15 shadow-lg ring-1 ring-white/25 backdrop-blur"
          aria-hidden
        >
          <Scissors className="h-8 w-8 text-white" />
        </div>
        {children}
      </div>
    </div>
  );
}
