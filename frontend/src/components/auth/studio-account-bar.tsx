"use client";

import { useState } from "react";
import { signOut, useSession } from "@/lib/auth-client";
import { Button } from "@/components/ui/button";
import { LogOut } from "lucide-react";
import { markSignedOut } from "@/components/auth/session-gate";
import { SignInForm } from "@/components/auth/sign-in";
import { SignUpForm } from "@/components/auth/sign-up";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

type DialogTab = "signin" | "signup";

/**
 * 工作台账户条（多账号阶段一）：
 * 顶部居中常驻：
 * - 未登录 → 「登录 / 注册」按钮，点击弹出行内弹窗，在本页完成登录或注册（不跳转）
 * - 已登录 → 显示邮箱 + 「退出」
 * 由 studio/layout.tsx 渲染，不改动 studio/page.tsx。
 */
export function StudioAccountBar() {
  const { data: session } = useSession();
  const [open, setOpen] = useState(false);
  const [tab, setTab] = useState<DialogTab>("signin");

  if (!session?.user) {
    return (
      <>
        <div
          className="fixed top-[10px] left-1/2 z-50 -translate-x-1/2 flex items-center gap-2 rounded-full border bg-background/95 py-1 pl-3 pr-1 shadow-sm backdrop-blur"
          data-testid="studio-account-bar"
        >
          <span className="text-xs text-muted-foreground">多账号</span>
          <Button
            variant="ghost"
            size="sm"
            className="h-7 rounded-full px-2 text-xs"
            onClick={() => {
              setTab("signin");
              setOpen(true);
            }}
            data-testid="studio-account-signin"
          >
            登录
          </Button>
          <Button
            variant="ghost"
            size="sm"
            className="h-7 rounded-full px-2 text-xs"
            onClick={() => {
              setTab("signup");
              setOpen(true);
            }}
            data-testid="studio-account-signup"
          >
            注册
          </Button>
        </div>
        <Dialog open={open} onOpenChange={setOpen}>
          <DialogContent className="max-w-md" data-testid="studio-signin-dialog">
            <DialogHeader>
              <DialogTitle>{tab === "signin" ? "登录" : "注册账号"}</DialogTitle>
              <DialogDescription>
                {tab === "signin"
                  ? "登录后可使用工作台的账号功能。"
                  : "注册新账号；验证邮箱后即可登录使用。"}
              </DialogDescription>
            </DialogHeader>
            <div className="flex gap-1 rounded-full border p-1 text-sm" data-testid="studio-auth-tabs">
              <button
                type="button"
                onClick={() => setTab("signin")}
                className={`flex-1 rounded-full px-3 py-1 ${
                  tab === "signin" ? "bg-primary text-primary-foreground" : "text-muted-foreground"
                }`}
                data-testid="studio-auth-tab-signin"
              >
                登录
              </button>
              <button
                type="button"
                onClick={() => setTab("signup")}
                className={`flex-1 rounded-full px-3 py-1 ${
                  tab === "signup" ? "bg-primary text-primary-foreground" : "text-muted-foreground"
                }`}
                data-testid="studio-auth-tab-signup"
              >
                注册
              </button>
            </div>
            {tab === "signin" ? (
              <SignInForm
                lang="zh"
                showForgotLink={false}
                onAuthenticated={() => {
                  setOpen(false);
                  // 原地登录完成后整页刷新，账户条与会话状态一并更新
                  window.location.reload();
                }}
              />
            ) : (
              <SignUpForm lang="zh" />
            )}
          </DialogContent>
        </Dialog>
      </>
    );
  }

  const email = session.user.email;

  return (
    <div
      className="fixed top-[10px] left-1/2 z-50 -translate-x-1/2 flex items-center gap-2 rounded-full border bg-background/95 py-1 pl-3 pr-1 shadow-sm backdrop-blur"
      data-testid="studio-account-bar"
    >
      <span
        className="max-w-[220px] truncate text-xs text-muted-foreground"
        title={email}
        data-testid="studio-account-email"
      >
        {email}
      </span>
      <Button
        variant="ghost"
        size="sm"
        className="h-7 rounded-full px-2 text-xs"
        onClick={async () => {
          markSignedOut();
          await signOut();
          window.location.href = `/studio/sign-in?reason=signed-out`;
        }}
        data-testid="studio-signout"
      >
        <LogOut className="h-3.5 w-3.5" aria-hidden />
        退出
      </Button>
    </div>
  );
}
