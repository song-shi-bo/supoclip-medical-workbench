"use client";

import { useState } from "react";
import { signIn, sendVerificationEmail } from "../../lib/auth-client";
import { track } from "@/lib/datafast";
import { Button } from "../ui/button";
import { Input } from "../ui/input";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../ui/card";
import { Alert, AlertDescription } from "../ui/alert";
import { useRouter, useSearchParams } from "next/navigation";
import { safeInternalRedirect } from "@/lib/redirect";
import { AUTH_PATHS } from "@/lib/auth-paths";
import { usernameLoginEnabled } from "@/lib/auth-flags";
import { Loader2 } from "lucide-react";

type SignInStatus = "idle" | "loading" | "error" | "unverified" | "success";

export type AuthLang = "zh" | "en";

const STRINGS = {
  zh: {
    emailPlaceholder: "邮箱",
    accountPlaceholder: "账号",
    passwordPlaceholder: "密码",
    submit: "登录",
    submitLoading: "登录中…",
    success: "登录成功！",
    errorFallback: "登录失败",
    expiredNotice: "会话已过期，请重新登录后继续。",
    signedOutNotice: "您已退出登录。",
    unverifiedTitle: "邮箱未验证",
    unverifiedMsg: "该邮箱尚未验证。请查收验证邮件，或在下方重新发送。",
    resend: "重新发送验证邮件",
    resendSending: "发送中…",
    resendSent: "验证邮件已发送，请查收邮箱。",
    resendError: "发送失败，请稍后重试。",
    forgot: "忘记密码？",
  },
  en: {
    emailPlaceholder: "Email",
    accountPlaceholder: "Username",
    passwordPlaceholder: "Password",
    submit: "Sign In",
    submitLoading: "Signing In...",
    success: "Signed in successfully!",
    errorFallback: "Failed to sign in",
    expiredNotice: "Your session has expired. Please sign in again to continue.",
    signedOutNotice: "You have signed out.",
    unverifiedTitle: "Email not verified",
    unverifiedMsg:
      "This email hasn't been verified yet. Check your inbox for the verification email, or resend it below.",
    resend: "Resend verification email",
    resendSending: "Sending...",
    resendSent: "Verification email sent. Please check your inbox.",
    resendError: "Failed to send. Try again later.",
    forgot: "Forgot password?",
  },
} as const;

/** 常见 Better Auth 错误码的中文文案；未命中时回退服务端原文 */
const ZH_ERROR_MAP: Array<[RegExp, string]> = [
  [/invalid[_ ]email[_ ]or[_ ]password|invalid email or password/i, "邮箱或密码错误"],
  [/invalid[_ ]username[_ ]or[_ ]password|invalid username or password/i, "账号或密码错误"],
  [/invalid password/i, "密码错误"],
  [/user not found/i, "该账号尚未注册"],
  [/username[_ ]?is[_ ]?too[_ ]?short/i, "账号名至少 3 个字符"],
  [/username[_ ]?is[_ ]?too[_ ]?long/i, "账号名至多 30 个字符"],
  [/invalid[_ ]username/i, "账号名格式无效（仅限字母、数字、下划线和点）"],
];

function zhErrorMessage(error: { status?: number; code?: string; message?: string }): string | null {
  const text = `${error.code ?? ""} ${error.message ?? ""}`;
  for (const [pattern, message] of ZH_ERROR_MAP) {
    if (pattern.test(text)) return message;
  }
  return null;
}

function isEmailNotVerified(error: {
  status?: number;
  code?: string;
  message?: string;
} | null | undefined): boolean {
  if (!error) return false;
  if (error.code === "EMAIL_NOT_VERIFIED") return true;
  return error.status === 403 && /not verified/i.test(error.message ?? "");
}

interface SignInFormProps {
  /** 原地登录（弹窗）场景：成功后回调，不做页面跳转 */
  onAuthenticated?: () => void;
  /** 弹窗场景隐藏"忘记密码"等站内跳转链接 */
  showForgotLink?: boolean;
  /** 登录成功后的站内跳转目标（页面模式用；弹窗模式忽略） */
  redirectTo?: string;
  /** 会话过期/已退出提示（页面模式由 URL 参数驱动） */
  notice?: "expired" | "signed-out" | null;
  /** 界面语言，默认英文（独立页面保持现状） */
  lang?: AuthLang;
  /** 登录凭据类型：email（默认，生产）| username（隔离环境账号名登录） */
  identifierMode?: "email" | "username";
}

/**
 * 登录表单核心（页面 /studio/sign-in 与工作台登录弹窗共用）。
 * 未传 onAuthenticated 时保持页面模式行为：成功后跳转 redirectTo（默认回首页）。
 */
export function SignInForm({
  onAuthenticated,
  showForgotLink = true,
  redirectTo = "/studio",
  notice = null,
  lang = "zh",
  identifierMode = usernameLoginEnabled ? "username" : "email",
}: SignInFormProps) {
  const t = STRINGS[lang];
  const [email, setEmail] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [status, setStatus] = useState<SignInStatus>("idle");
  const [message, setMessage] = useState("");
  const [resendState, setResendState] = useState<"idle" | "sending" | "sent" | "error">("idle");

  const sessionExpired = notice === "expired";
  const signedOut = notice === "signed-out";
  const usernameMode = identifierMode === "username";

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setStatus("loading");
    setMessage("");
    setResendState("idle");

    const response = usernameMode
      ? await signIn.username({
          // 服务端也会规范化（去空格+小写），这里先做一层以保证体验一致
          username: username.trim().toLowerCase(),
          password,
        })
      : await signIn.email({
          email,
          password,
        });

    if (response.error) {
      const err = response.error as { status?: number; code?: string; message?: string };
      if (isEmailNotVerified(err)) {
        setStatus("unverified");
        setMessage(t.unverifiedMsg);
      } else {
        setStatus("error");
        setMessage(
          (lang === "zh" ? zhErrorMessage(err) : null) ??
            err.message ??
            t.errorFallback
        );
      }
      return;
    }

    track("signin_completed", {
      auth_method: "email",
    });
    setStatus("success");
    setMessage(t.success);

    if (onAuthenticated) {
      // 原地登录：不跳转，交给调用方（如关闭弹窗 + 刷新）
      setTimeout(() => onAuthenticated(), 500);
      return;
    }

    // 页面模式：登录后回到原访问页面
    setTimeout(() => {
      window.location.href = redirectTo;
    }, 500);
  };

  const handleResendVerification = async () => {
    setResendState("sending");
    const response = await sendVerificationEmail({
      email: email.trim(),
      callbackURL: AUTH_PATHS.signIn,
    });
    if (response.error) {
      setResendState("error");
      return;
    }
    setResendState("sent");
  };

  return (
    <>
      {sessionExpired && status !== "success" && (
        <Alert className="mb-4" data-testid="signin-session-expired">
          <AlertDescription>{t.expiredNotice}</AlertDescription>
        </Alert>
      )}
      {signedOut && status !== "success" && (
        <Alert className="mb-4" data-testid="signin-signed-out">
          <AlertDescription>{t.signedOutNotice}</AlertDescription>
        </Alert>
      )}
      <form onSubmit={handleSubmit} className="space-y-4">
        {usernameMode ? (
          <Input
            type="text"
            placeholder={t.accountPlaceholder}
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            required
            autoComplete="username"
            disabled={status === "loading"}
            data-testid="signin-account"
          />
        ) : (
          <Input
            type="email"
            placeholder={t.emailPlaceholder}
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            required
            disabled={status === "loading"}
            data-testid="signin-email"
          />
        )}
        <Input
          type="password"
          placeholder={t.passwordPlaceholder}
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          required
          disabled={status === "loading"}
          autoComplete="current-password"
          data-testid="signin-password"
        />
        <Button type="submit" className="w-full" disabled={status === "loading"} data-testid="signin-submit">
          {status === "loading" ? (
            <>
              <Loader2 className="mr-2 h-4 w-4 animate-spin" aria-hidden />
              {t.submitLoading}
            </>
          ) : (
            t.submit
          )}
        </Button>
      </form>

      {status === "error" && message && (
        <p className="mt-4 text-sm text-red-600" role="alert" data-testid="signin-error">
          {message}
        </p>
      )}

      {status === "success" && (
        <p className="mt-4 text-sm text-green-600" role="status" data-testid="signin-success">
          {message}
        </p>
      )}

      {status === "unverified" && (
        <div
          className="mt-4 rounded-md border border-amber-300 bg-amber-50 p-3 text-sm"
          data-testid="signin-unverified"
        >
          <p className="font-medium text-amber-800" role="alert">
            {t.unverifiedTitle}
          </p>
          <p className="mt-1 text-amber-800">{message}</p>
          {resendState === "sent" ? (
            <p className="mt-2 text-green-700" role="status" data-testid="signin-resend-sent">
              {t.resendSent}
            </p>
          ) : usernameMode ? (
            <p className="mt-2 text-amber-800" data-testid="signin-unverified-hint">
              请打开注册时收到的验证邮件完成验证；也可通过「忘记密码」用注册邮箱重置。
            </p>
          ) : (
            <div className="mt-2 flex items-center gap-2">
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={handleResendVerification}
                disabled={resendState === "sending"}
                data-testid="signin-resend-verification"
              >
                {resendState === "sending" ? t.resendSending : t.resend}
              </Button>
              {resendState === "error" && (
                <span className="text-red-600" data-testid="signin-resend-error">
                  {t.resendError}
                </span>
              )}
            </div>
          )}
        </div>
      )}

      {showForgotLink && (
        <div className="mt-4 text-center">
          <a
            href={`${AUTH_PATHS.forgotPassword}${redirectTo !== "/studio" ? `?redirect=${encodeURIComponent(redirectTo)}` : ""}`}
            className="text-sm text-indigo-600 hover:text-indigo-500"
            data-testid="signin-forgot-password"
          >
            {t.forgot}
          </a>
        </div>
      )}
    </>
  );
}

/** 页面版登录（/studio/sign-in）：读取 URL 参数（回跳/原因提示），成功后跳转。 */
export function SignIn() {
  const router = useRouter();
  const searchParams = useSearchParams();

  // 登录成功后回到原访问页面（?redirect=），缺省回工作台（入口 `/` 归 DSH，不可作缺省）
  const redirectTo = safeInternalRedirect(searchParams.get("redirect"));
  // 会话过期提示（SessionGate / 登出流程跳转时带 ?reason=expired）
  const reason = searchParams.get("reason");
  const notice = reason === "expired" || reason === "signed-out" ? reason : null;

  const handleAuthenticated = () => {
    router.push(redirectTo);
    router.refresh();
  };

  return (
    <Card className="w-full max-w-md mx-auto">
      <CardHeader>
        <CardTitle>登录</CardTitle>
        <CardDescription>登录到你的账号</CardDescription>
      </CardHeader>
      <CardContent>
        <SignInForm
          onAuthenticated={handleAuthenticated}
          redirectTo={redirectTo}
          notice={notice}
        />
      </CardContent>
    </Card>
  );
}
