import { VerifyEmail } from "@/components/auth/verify-email";
import { Suspense } from "react";

export default function VerifyEmailPage() {
  return (
    <div className="min-h-screen flex items-center justify-center bg-gray-50 py-12 px-4 sm:px-6 lg:px-8">
      <div className="max-w-md w-full">
        {/* VerifyEmail 从 URL 读取 token / callbackURL / email，需 Suspense 边界 */}
        <Suspense fallback={null}>
          <VerifyEmail />
        </Suspense>
      </div>
    </div>
  );
}
