import { SignIn } from "@/components/auth/sign-in";
import Link from "next/link";
import { AUTH_PATHS } from "@/lib/auth-paths";
import { Suspense } from "react";

export default function SignInPage() {
  return (
    <div className="min-h-screen flex items-center justify-center bg-gray-50 py-12 px-4 sm:px-6 lg:px-8">
      <div className="max-w-md w-full space-y-8">
        {/* SignIn 读取 useSearchParams（redirect / reason），需 Suspense 边界 */}
        <Suspense fallback={null}>
          <SignIn />
        </Suspense>
        <div className="text-center">
          <p className="text-sm text-gray-600">
            没有账号？{" "}
            <Link href={AUTH_PATHS.signUp} className="font-medium text-indigo-600 hover:text-indigo-500">
              注册
            </Link>
          </p>
        </div>
      </div>
    </div>
  );
}
