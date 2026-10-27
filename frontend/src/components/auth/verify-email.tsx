"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { sendVerificationEmail, verifyEmail } from "../../lib/auth-client";
import { Button } from "../ui/button";
import { Input } from "../ui/input";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../ui/card";
import { Loader2 } from "lucide-react";
import Link from "next/link";
import { AUTH_PATHS } from "@/lib/auth-paths";

type VerifyStatus = "idle" | "verifying" | "success" | "error";

export function VerifyEmail() {
  const [status, setStatus] = useState<VerifyStatus>("idle");
  const [message, setMessage] = useState("");
  const [email, setEmail] = useState("");
  const [resendState, setResendState] = useState<"idle" | "sending" | "sent" | "error">("idle");
  const verifyStartedRef = useRef(false);

  // 从 URL 取 token / email（page.tsx 已用 Suspense 包裹本组件）
  const [params, setParams] = useState<URLSearchParams | null>(null);
  useEffect(() => {
    setParams(new URLSearchParams(window.location.search));
  }, []);

  const token = params?.get("token") ?? "";
  const callbackURL = params?.get("callbackURL") ?? "";

  useEffect(() => {
    setEmail(params?.get("email") ?? "");
  }, [params]);

  const runVerify = useCallback(async (t: string) => {
    setStatus("verifying");
    setMessage("");
    const response = await verifyEmail({ query: { token: t } });
    if (response.error) {
      setStatus("error");
      setMessage("验证链接无效或已过期。请在下方重新申请验证邮件。");
      return;
    }
    setStatus("success");
  }, []);

  useEffect(() => {
    if (token && !verifyStartedRef.current) {
      verifyStartedRef.current = true;
      void runVerify(token);
    }
  }, [token, runVerify]);

  const handleResend = async () => {
    if (!email.trim()) return;
    setResendState("sending");
    const response = await sendVerificationEmail({
      email: email.trim(),
      callbackURL: callbackURL || AUTH_PATHS.signIn,
    });
    if (response.error) {
      setResendState("error");
      return;
    }
    setResendState("sent");
  };

  return (
    <Card className="w-full max-w-md mx-auto" data-testid="verify-email-card">
      <CardHeader>
        <CardTitle>邮箱验证</CardTitle>
        <CardDescription>验证邮箱地址后即可解锁工作台</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {status === "verifying" && (
          <div className="flex items-center gap-3 text-sm text-gray-600" data-testid="verify-email-verifying" role="status">
            <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
            正在验证邮箱…
          </div>
        )}

        {status === "success" && (
          <div className="rounded-md border border-green-300 bg-green-50 p-3" data-testid="verify-email-success" role="status">
            <p className="font-medium text-green-800">邮箱验证成功！</p>
            <p className="mt-1 text-sm text-green-800">
              邮箱已验证，现在可以登录并使用工作台。
            </p>
            <a
              href={callbackURL || AUTH_PATHS.signIn}
              className="mt-2 inline-block font-medium text-indigo-600 hover:text-indigo-500"
              data-testid="verify-email-continue"
            >
              前往登录
            </a>
          </div>
        )}

        {status === "error" && (
          <div className="rounded-md border border-red-300 bg-red-50 p-3" data-testid="verify-email-error" role="alert">
            <p className="font-medium text-red-800">验证失败</p>
            <p className="mt-1 text-sm text-red-800">{message}</p>
          </div>
        )}

        {(status === "idle" || status === "error") && (
          <div className="space-y-3" data-testid="verify-email-resend-form">
            {status === "idle" && (
              <p className="text-sm text-gray-600">
                等待邮箱验证。输入邮箱以重新发送验证链接。
              </p>
            )}
            <Input
              type="email"
              placeholder="邮箱"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              data-testid="verify-email-input"
            />
            {resendState === "sent" ? (
              <p className="text-sm text-green-700" role="status" data-testid="verify-email-resend-sent">
                验证邮件已发送，请查收邮箱。
              </p>
            ) : (
              <div className="flex items-center gap-2">
                <Button
                  type="button"
                  onClick={handleResend}
                  disabled={resendState === "sending" || !email.trim()}
                  data-testid="verify-email-resend"
                >
                  {resendState === "sending" ? "发送中…" : "重新发送验证邮件"}
                </Button>
                {resendState === "error" && (
                  <span className="text-sm text-red-600" data-testid="verify-email-resend-error">
                    发送失败，请稍后重试。
                  </span>
                )}
              </div>
            )}
          </div>
        )}

        <p className="text-sm text-gray-500">
          已经验证？{" "}
          <Link href={AUTH_PATHS.signIn} className="font-medium text-indigo-600 hover:text-indigo-500">
            去登录
          </Link>
        </p>
      </CardContent>
    </Card>
  );
}
