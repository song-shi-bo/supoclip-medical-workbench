"use client";

import { useState } from "react";
import { signUp } from "../../lib/auth-client";
import { track } from "@/lib/datafast";
import { Button } from "../ui/button";
import { Input } from "../ui/input";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../ui/card";
import { Loader2 } from "lucide-react";
import { emailVerificationRequired } from "@/lib/auth-flags";
import Link from "next/link";
import { AUTH_PATHS } from "@/lib/auth-paths";
import type { AuthLang } from "./sign-in";
import { usernameLoginEnabled } from "@/lib/auth-flags";

type SignUpStatus = "idle" | "loading" | "error" | "success" | "awaiting-verification";

const STRINGS = {
  zh: {
    usernamePlaceholder: "账号名",
    namePlaceholder: "姓名",
    emailPlaceholder: "邮箱",
    passwordPlaceholder: "密码",
    submit: "注册",
    submitLoading: "注册中…",
    success: "注册成功！正在登录…",
    errorFallback: "注册失败",
    signupDisabled: "当前未开放注册",
    awaitingTitle: "请查收邮箱",
    awaitingDescPrefix: "验证邮件已发送至",
    awaitingBody:
      "点击邮件中的链接完成验证，链接 1 小时内有效。验证完成前，登录与工作台上传/渲染保持不可用。",
    goVerify: "前往验证状态页",
    alreadyVerified: "已验证？去登录",
    orSignIn: "已有账号？登录",
  },
  en: {
    usernamePlaceholder: "Username",
    namePlaceholder: "Full Name",
    emailPlaceholder: "Email",
    passwordPlaceholder: "Password",
    submit: "Sign Up",
    submitLoading: "Creating Account...",
    success: "Account created successfully! Signing you in...",
    errorFallback: "Failed to create account",
    signupDisabled: "Sign-up is currently closed",
    awaitingTitle: "Check your inbox",
    awaitingDescPrefix: "We sent a verification email to",
    awaitingBody:
      "Click the link in the email to verify your address. The link expires in 1 hour. Until your email is verified, sign-in and workspace uploads/renders stay disabled.",
    goVerify: "Go to verification status page",
    alreadyVerified: "Already verified? Sign in",
    orSignIn: "Already have an account? Sign in",
  },
} as const;

const ZH_ERROR_MAP: Array<[RegExp, string]> = [
  [
    /EMAIL_PASSWORD_SIGN_UP_DISABLED|disable[_ ]?sign[_ ]?up|sign[_ ]?up is (not enabled|disabled)|registration.*(disabled|closed)/i,
    "当前未开放注册",
  ],
  [/user already exists|already registered|account already exists/i, "该邮箱已注册，请直接登录"],
  [/username[_ ]?is[_ ]?already[_ ]?taken/i, "该账号名已被占用，请换一个"],
  [/username[_ ]?is[_ ]?too[_ ]?short/i, "账号名至少 3 个字符"],
  [/username[_ ]?is[_ ]?too[_ ]?long/i, "账号名至多 30 个字符"],
  [/invalid[_ ]username/i, "账号名格式无效（仅限字母、数字、下划线和点）"],
  [/password.*too short/i, "密码长度过短（至少 8 位）"],
];

function zhErrorMessage(error: { status?: number; code?: string; message?: string }): string | null {
  const text = `${error.code ?? ""} ${error.message ?? ""}`;
  for (const [pattern, message] of ZH_ERROR_MAP) {
    if (pattern.test(text)) return message;
  }
  return null;
}

interface SignUpFormProps {
  /** 界面语言，默认英文（独立页面保持现状） */
  lang?: AuthLang;
  /** 是否要求填写账号名（隔离环境账号名登录配套） */
  showUsername?: boolean;
}

/**
 * 注册表单核心（页面 /studio/sign-up 与工作台注册弹窗共用）。
 * 开启邮箱验证的环境：注册成功不建会话，进入「请查收邮箱」面板；
 * 未开启时保持旧行为：注册即登录并回首页。
 */
export function SignUpForm({ lang = "zh", showUsername = usernameLoginEnabled }: SignUpFormProps) {
  const t = STRINGS[lang];
  const [name, setName] = useState("");
  const [username, setUsername] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [status, setStatus] = useState<SignUpStatus>("idle");
  const [message, setMessage] = useState("");

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setStatus("loading");
    setMessage("");

    const response = await signUp.email({
      email,
      password,
      name,
      ...(showUsername ? { username: username.trim().toLowerCase() } : {}),
    });

    if (response.error) {
      const err = response.error as { status?: number; code?: string; message?: string };
      setStatus("error");
      setMessage(
        (lang === "zh" ? zhErrorMessage(err) : null) ?? err.message ?? t.errorFallback
      );
      return;
    }

    track("signup_completed", {
      auth_method: "email",
    });

    if (emailVerificationRequired) {
      // 邮箱验证流程（隔离环境）：注册成功但不建立会话，提示去邮箱完成验证。
      setStatus("awaiting-verification");
      return;
    }

    // 旧行为（未开启邮箱验证）：注册即登录
    setStatus("success");
    setMessage(t.success);

    // Automatically sign in after successful sign up
    setTimeout(() => {
      // 缺省回工作台：公网入口 `/` 归 DSH 平台侧，不可作注册后落点
      window.location.href = "/studio";
    }, 1000);
  };

  if (status === "awaiting-verification") {
    return (
      <div className="space-y-3" data-testid="signup-awaiting-verification">
        <p className="font-medium">
          {t.awaitingTitle}
        </p>
        <p className="text-sm text-gray-600">
          {t.awaitingDescPrefix} <span className="font-medium">{email}</span>。
        </p>
        <p className="text-sm text-gray-600">{t.awaitingBody}</p>
        <p className="text-sm">
          <a
            href={`${AUTH_PATHS.verifyEmail}?email=${encodeURIComponent(email.trim())}`}
            className="font-medium text-indigo-600 hover:text-indigo-500"
            data-testid="signup-go-verify"
          >
            {t.goVerify}
          </a>
        </p>
        <p className="text-sm text-gray-500">
          <button
            type="button"
            className="font-medium text-indigo-600 hover:text-indigo-500"
            onClick={() => {
              setStatus("idle");
              setMessage("");
            }}
            data-testid="signup-back"
          >
            {t.orSignIn}
          </button>
        </p>
      </div>
    );
  }

  return (
    <div>
      <form onSubmit={handleSubmit} className="space-y-4">
        {showUsername && (
          <Input
            type="text"
            placeholder={t.usernamePlaceholder}
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            required
            minLength={3}
            autoComplete="username"
            disabled={status === "loading"}
            data-testid="signup-username"
          />
        )}
        <Input
          type="text"
          placeholder={t.namePlaceholder}
          value={name}
          onChange={(e) => setName(e.target.value)}
          required
          disabled={status === "loading"}
          data-testid="signup-name"
        />
        <Input
          type="email"
          placeholder={t.emailPlaceholder}
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          required
          disabled={status === "loading"}
          data-testid="signup-email"
        />
        <Input
          type="password"
          placeholder={t.passwordPlaceholder}
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          required
          disabled={status === "loading"}
          minLength={8}
          data-testid="signup-password"
        />
        <Button type="submit" className="w-full" disabled={status === "loading"} data-testid="signup-submit">
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
        <p className="mt-4 text-sm text-red-600" role="alert" data-testid="signup-error">
          {message}
        </p>
      )}
      {status === "success" && (
        <p className="mt-4 text-sm text-green-600" role="status" data-testid="signup-success">
          {message}
        </p>
      )}
    </div>
  );
}

/** 页面版注册（/studio/sign-up）：英文界面，行为与此前一致。 */
export function SignUp() {
  return (
    <Card className="w-full max-w-md mx-auto">
      <CardHeader>
        <CardTitle>注册</CardTitle>
        <CardDescription>创建新账号开始使用</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <SignUpForm lang="zh" />
        <p className="text-sm text-gray-600">
          已有账号？{" "}
          <Link href={AUTH_PATHS.signIn} className="font-medium text-indigo-600 hover:text-indigo-500">
            登录
          </Link>
        </p>
      </CardContent>
    </Card>
  );
}
