"use client";

import { useCallback, useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { testMailboxEnabled } from "@/lib/auth-flags";

interface TestMail {
  id: string;
  kind: "verification" | "reset-password";
  to: string;
  subject: string;
  url: string;
  token: string;
  created_at: string;
}

/**
 * 测试邮箱（开发/隔离环境专用）：读取本地 outbox 的「测试邮件」，
 * 展示验证/重置链接，替代未配置的 SMTP。生产环境 API 返回 404，
 * 本页面显示未启用提示。不发任何真实邮件。
 */
export default function TestMailboxPage() {
  const [mails, setMails] = useState<TestMail[] | null>(null);
  const [notAvailable, setNotAvailable] = useState(false);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetch("/api/dev/test-mailbox");
      if (res.status === 404) {
        setNotAvailable(true);
        setMails(null);
        return;
      }
      const data = (await res.json()) as { mails: TestMail[] };
      setMails(data.mails);
      setNotAvailable(false);
    } catch {
      setNotAvailable(true);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const clearMails = async () => {
    await fetch("/api/dev/test-mailbox", { method: "DELETE" });
    void refresh();
  };

  return (
    <div className="min-h-screen bg-gray-50 py-10 px-4">
      <div className="max-w-2xl mx-auto space-y-4">
        <Card>
          <CardHeader>
            <CardTitle data-testid="test-mailbox-title">Test mailbox</CardTitle>
            <CardDescription>
              Dev-only mailbox backed by a local outbox file — no real emails are sent. Verify and
              reset links below work against the isolated environment.
            </CardDescription>
          </CardHeader>
          <CardContent className="flex gap-2">
            <Button size="sm" onClick={() => void refresh()} disabled={loading} data-testid="test-mailbox-refresh">
              {loading ? "Loading..." : "Refresh"}
            </Button>
            <Button size="sm" variant="outline" onClick={() => void clearMails()} data-testid="test-mailbox-clear">
              Clear
            </Button>
          </CardContent>
        </Card>

        {notAvailable && (
          <Card>
            <CardContent className="py-6 text-sm text-gray-600" data-testid="test-mailbox-disabled">
              Test mailbox is not enabled in this environment (ENABLE_TEST_MAIL_ROUTES is off).
            </CardContent>
          </Card>
        )}

        {mails && mails.length === 0 && (
          <Card>
            <CardContent className="py-6 text-sm text-gray-600" data-testid="test-mailbox-empty">
              No test emails yet. Sign up or request a password reset to generate one.
            </CardContent>
          </Card>
        )}

        {mails &&
          mails.map((mail) => (
            <Card key={mail.id} data-testid="test-mail-item">
              <CardHeader>
                <CardTitle className="text-base">{mail.subject}</CardTitle>
                <CardDescription>
                  To {mail.to} · {new Date(mail.created_at).toLocaleString()} · {mail.kind}
                </CardDescription>
              </CardHeader>
              <CardContent>
                <a
                  href={mail.url}
                  className="text-sm font-medium text-indigo-600 hover:text-indigo-500 break-all"
                  data-testid={`test-mail-link-${mail.kind}`}
                >
                  {mail.url}
                </a>
              </CardContent>
            </Card>
          ))}

        {!testMailboxEnabled && !notAvailable && mails === null && !loading && (
          <p className="text-xs text-gray-400">
            Note: this page only lists mails when the API route is enabled server-side.
          </p>
        )}
      </div>
    </div>
  );
}
