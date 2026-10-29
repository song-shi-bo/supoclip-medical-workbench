import { SignIn } from "@/components/auth/sign-in";
import { AuthShell } from "@/components/auth/auth-shell";
import Link from "next/link";
import { AUTH_PATHS } from "@/lib/auth-paths";
import { Suspense } from "react";

export default function SignInPage() {
  return (
    <AuthShell>
      <div className="w-full max-w-md space-y-6">
        {/* SignIn 读取 useSearchParams（redirect / reason），需 Suspense 边界 */}
        <Suspense fallback={null}>
          <SignIn />
        </Suspense>
        <div className="text-center">
          <p className="text-sm text-violet-200">
            没有账号？{" "}
            <Link
              href={AUTH_PATHS.signUp}
              className="font-medium text-white underline-offset-4 hover:underline"
            >
              注册
            </Link>
          </p>
        </div>
      </div>
    </AuthShell>
  );
}
