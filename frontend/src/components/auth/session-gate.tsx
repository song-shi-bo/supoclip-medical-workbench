"use client";

import { useEffect, useRef, useState } from "react";
import { signOut, sendVerificationEmail, useSession } from "@/lib/auth-client";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Loader2 } from "lucide-react";
import { emailVerificationRequired, sessionGateEnabled } from "@/lib/auth-flags";
import { safeInternalRedirect } from "@/lib/redirect";
import { AUTH_PATHS } from "@/lib/auth-paths";

const HAD_SESSION_KEY = "supoclip.hadSession";
const JUST_SIGNED_OUT_KEY = "supoclip.justSignedOut";

/** 显式登出时调用：登出跳转应显示「已退出」而不是「会话过期」。 */
export function markSignedOut() {
  try {
    sessionStorage.setItem(JUST_SIGNED_OUT_KEY, "1");
  } catch {
    // sessionStorage 不可用时跳过：回退为「会话过期」文案
  }
}

function readFlag(key: string): boolean {
  try {
    return sessionStorage.getItem(key) === "1";
  } catch {
    return false;
  }
}

export function SessionGate({ children }: { children: React.ReactNode }) {
  // 总开关关闭（生产/共线环境默认）：不挂任何 hook、不发会话请求，
  // 保持接入门禁前的旧行为。登录/过期/未验证流程只在显式开启
  // NEXT_PUBLIC_SESSION_GATE_ENABLED 的隔离环境生效。
  if (!sessionGateEnabled) {
    return <>{children}</>;
  }

  return <SessionGateActive>{children}</SessionGateActive>;
}

function SessionGateActive({ children }: { children: React.ReactNode }) {
  const { data: session, isPending } = useSession();
  const redirectedRef = useRef(false);
  const [resendState, setResendState] = useState<"idle" | "sending" | "sent" | "error">("idle");

  const user = session?.user;
  const emailVerified = Boolean(user?.emailVerified);
  const needsVerification = emailVerificationRequired && !emailVerified;

  useEffect(() => {
    if (isPending || redirectedRef.current) return;

    if (user) {
      try {
        sessionStorage.setItem(HAD_SESSION_KEY, "1");
      } catch {
        // ignore
      }
      try {
        sessionStorage.removeItem(JUST_SIGNED_OUT_KEY);
      } catch {
        // ignore
      }
      return;
    }

    // 无会话：一律送回登录页并携带回跳地址；原因区分「已退出 / 会话过期 / 未登录」
    redirectedRef.current = true;
    const current = safeInternalRedirect(
      window.location.pathname + window.location.search,
      "/studio"
    );
    const justSignedOut = readFlag(JUST_SIGNED_OUT_KEY);
    const hadSession = readFlag(HAD_SESSION_KEY);
    const reason = justSignedOut ? "signed-out" : hadSession ? "expired" : null;
    const query = `redirect=${encodeURIComponent(current)}${reason ? `&reason=${reason}` : ""}`;
    window.location.replace(`${AUTH_PATHS.signIn}?${query}`);
  }, [isPending, user]);

  // 会话过期后 Better Auth 不再返回用户；此时历史标记不再清除，直到下次登录
  if (isPending) {
    return (
      <div
        className="min-h-screen flex items-center justify-center bg-gray-50"
        data-testid="session-gate-loading"
        role="status"
      >
        <Loader2 className="h-6 w-6 animate-spin text-gray-400" aria-hidden />
        <span className="ml-2 text-sm text-gray-500">正在检查登录状态…</span>
      </div>
    );
  }

  if (user && needsVerification) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-gray-50 px-4">
        <Card className="w-full max-w-md" data-testid="session-gate-unverified">
          <CardHeader>
            <CardTitle>需要验证邮箱</CardTitle>
            <CardDescription>
              账号 <span className="font-medium">{user.email}</span> 尚未完成邮箱验证。
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div
              className="rounded-md border border-amber-300 bg-amber-50 p-3 text-sm text-amber-800"
              data-testid="unverified-status"
              role="alert"
            >
              <p className="font-medium">当前状态：未验证</p>
              <ul className="mt-1 list-disc pl-5">
                <li>工作台登录入口已停用</li>
                <li>邮箱验证完成前，上传 / 渲染入口保持锁定</li>
              </ul>
            </div>
            {resendState === "sent" ? (
              <p className="text-sm text-green-700" role="status" data-testid="unverified-resend-sent">
                验证邮件已发送，请查收邮箱。
              </p>
            ) : (
              <div className="flex items-center gap-2">
                <Button
                  type="button"
                  onClick={async () => {
                    setResendState("sending");
                    const response = await sendVerificationEmail({
                      email: user.email,
                      callbackURL: "/studio",
                    });
                    setResendState(response.error ? "error" : "sent");
                  }}
                  disabled={resendState === "sending"}
                  data-testid="unverified-resend"
                >
                  {resendState === "sending" ? "发送中…" : "重新发送验证邮件"}
                </Button>
                {resendState === "error" && (
                  <span className="text-sm text-red-600" data-testid="unverified-resend-error">
                    发送失败，请稍后重试。
                  </span>
                )}
              </div>
            )}
            <Button
              type="button"
              variant="outline"
              className="w-full"
              onClick={async () => {
                markSignedOut();
                await signOut();
                window.location.href = `${AUTH_PATHS.signIn}?reason=signed-out`;
              }}
              data-testid="unverified-signout"
            >
              退出登录
            </Button>
          </CardContent>
        </Card>
      </div>
    );
  }

  return <>{children}</>;
}
