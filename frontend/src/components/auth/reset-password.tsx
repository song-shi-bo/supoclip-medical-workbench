"use client";

import { useEffect, useState } from "react";
import { resetPassword } from "../../lib/auth-client";
import { Button } from "../ui/button";
import { Input } from "../ui/input";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../ui/card";
import { Loader2 } from "lucide-react";
import Link from "next/link";
import { AUTH_PATHS } from "@/lib/auth-paths";

type ResetStatus = "idle" | "missing-token" | "loading" | "error" | "success";

export function ResetPassword() {
  const [token, setToken] = useState<string | null>(null);
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [status, setStatus] = useState<ResetStatus>("idle");
  const [message, setMessage] = useState("");

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const t = params.get("token");
    setToken(t);
    if (!t) {
      setStatus("missing-token");
      setMessage("缺少有效的重置链接。请在「忘记密码」页面重新申请。");
    }
  }, []);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!token) return;

    if (password !== confirmPassword) {
      setStatus("error");
      setMessage("两次输入的密码不一致");
      return;
    }

    setStatus("loading");
    setMessage("");

    const response = await resetPassword({
      newPassword: password,
      token,
    });

    if (response.error) {
      setStatus("error");
      setMessage(
        response.error.message ||
          "重置链接无效或已过期，请重新申请。"
      );
      return;
    }

    setStatus("success");
  };

  return (
    <Card className="w-full max-w-md mx-auto">
      <CardHeader>
        <CardTitle>重置密码</CardTitle>
        <CardDescription>为你的账号设置新密码</CardDescription>
      </CardHeader>
      <CardContent>
        {status === "success" ? (
          <div className="rounded-md border border-green-300 bg-green-50 p-3" data-testid="reset-password-success" role="status">
            <p className="font-medium text-green-800">密码已更新</p>
            <p className="mt-1 text-sm text-green-800">
              密码修改成功，请使用新密码登录。
            </p>
            <Link
              href={AUTH_PATHS.signIn}
              className="mt-2 inline-block font-medium text-indigo-600 hover:text-indigo-500"
              data-testid="reset-password-go-signin"
            >
              前往登录
            </Link>
          </div>
        ) : status === "missing-token" ? (
          <div className="rounded-md border border-amber-300 bg-amber-50 p-3" data-testid="reset-password-missing-token" role="alert">
            <p className="font-medium text-amber-800">缺少重置链接</p>
            <p className="mt-1 text-sm text-amber-800">{message}</p>
            <Link
              href={AUTH_PATHS.forgotPassword}
              className="mt-2 inline-block font-medium text-indigo-600 hover:text-indigo-500"
              data-testid="reset-password-go-forgot"
            >
              重新申请重置链接
            </Link>
          </div>
        ) : (
          <>
            <form onSubmit={handleSubmit} className="space-y-4">
              <Input
                type="password"
                placeholder="新密码"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required
                minLength={8}
                disabled={status === "loading"}
                data-testid="reset-password-new"
              />
              <Input
                type="password"
                placeholder="确认新密码"
                value={confirmPassword}
                onChange={(e) => setConfirmPassword(e.target.value)}
                required
                minLength={8}
                disabled={status === "loading"}
                data-testid="reset-password-confirm"
              />
              <Button
                type="submit"
                className="w-full"
                disabled={status === "loading"}
                data-testid="reset-password-submit"
              >
                {status === "loading" ? (
                  <>
                    <Loader2 className="mr-2 h-4 w-4 animate-spin" aria-hidden />
                    更新中…
                  </>
                ) : (
                  "更新密码"
                )}
              </Button>
            </form>
            {status === "error" && message && (
              <p className="mt-4 text-sm text-red-600" role="alert" data-testid="reset-password-error">
                {message}
              </p>
            )}
          </>
        )}
      </CardContent>
    </Card>
  );
}
