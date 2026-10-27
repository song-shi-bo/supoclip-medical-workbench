"use client";

import { useState } from "react";
import { requestPasswordReset } from "../../lib/auth-client";
import { Button } from "../ui/button";
import { Input } from "../ui/input";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../ui/card";
import { Loader2 } from "lucide-react";
import Link from "next/link";
import { AUTH_PATHS } from "@/lib/auth-paths";

type ForgotStatus = "idle" | "loading" | "error" | "success";

export function ForgotPassword() {
  const [email, setEmail] = useState("");
  const [status, setStatus] = useState<ForgotStatus>("idle");
  const [message, setMessage] = useState("");

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setStatus("loading");
    setMessage("");

    const response = await requestPasswordReset({
      email: email.trim(),
      // 重置链接回到站内 /studio/reset-password（Better Auth 会带上 token）
      redirectTo:
        typeof window !== "undefined"
          ? `${window.location.origin}${AUTH_PATHS.resetPassword}`
          : AUTH_PATHS.resetPassword,
    });

    if (response.error) {
      setStatus("error");
      setMessage(response.error.message || "重置邮件发送失败");
      return;
    }

    // 统一成功文案：不暴露邮箱是否存在（防枚举）
    setStatus("success");
  };

  return (
    <Card className="w-full max-w-md mx-auto">
      <CardHeader>
        <CardTitle>忘记密码</CardTitle>
        <CardDescription>输入邮箱，我们会发送密码重置链接。</CardDescription>
      </CardHeader>
      <CardContent>
        {status === "success" ? (
          <div className="rounded-md border border-green-300 bg-green-50 p-3" data-testid="forgot-password-success" role="status">
            <p className="font-medium text-green-800">请查收邮箱</p>
            <p className="mt-1 text-sm text-green-800">
              如果 {email} 对应的账号存在，重置链接已发送。链接 1 小时内有效。
            </p>
            <Link
              href={AUTH_PATHS.signIn}
              className="mt-2 inline-block font-medium text-indigo-600 hover:text-indigo-500"
              data-testid="forgot-password-back"
            >
              返回登录
            </Link>
          </div>
        ) : (
          <>
            <form onSubmit={handleSubmit} className="space-y-4">
              <Input
                type="email"
                placeholder="邮箱"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                required
                disabled={status === "loading"}
                data-testid="forgot-password-email"
              />
              <Button
                type="submit"
                className="w-full"
                disabled={status === "loading"}
                data-testid="forgot-password-submit"
              >
                {status === "loading" ? (
                  <>
                    <Loader2 className="mr-2 h-4 w-4 animate-spin" aria-hidden />
                    发送中…
                  </>
                ) : (
                  "发送重置链接"
                )}
              </Button>
            </form>
            {status === "error" && message && (
              <p className="mt-4 text-sm text-red-600" role="alert" data-testid="forgot-password-error">
                {message}
              </p>
            )}
            <p className="mt-4 text-center text-sm text-gray-600">
              想起密码了？{" "}
              <Link href={AUTH_PATHS.signIn} className="font-medium text-indigo-600 hover:text-indigo-500">
                去登录
              </Link>
            </p>
          </>
        )}
      </CardContent>
    </Card>
  );
}
