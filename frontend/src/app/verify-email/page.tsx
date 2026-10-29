import { VerifyEmail } from "@/components/auth/verify-email";
import { AuthShell } from "@/components/auth/auth-shell";
import { Suspense } from "react";

export default function VerifyEmailPage() {
  return (
    <AuthShell>
      <div className="w-full max-w-md">
        {/* VerifyEmail 从 URL 读取 token / callbackURL / email，需 Suspense 边界 */}
        <Suspense fallback={null}>
          <VerifyEmail />
        </Suspense>
      </div>
    </AuthShell>
  );
}
