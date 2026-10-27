import { SignUp } from "@/components/auth/sign-up";
import Link from "next/link";
import { AUTH_PATHS } from "@/lib/auth-paths";

export default function SignUpPage() {
  return (
    <div className="min-h-screen flex items-center justify-center bg-gray-50 py-12 px-4 sm:px-6 lg:px-8">
      <div className="max-w-md w-full space-y-8">
        <SignUp />
        <div className="text-center">
          <p className="text-sm text-gray-600">
            已有账号？{" "}
            <Link href={AUTH_PATHS.signIn} className="font-medium text-indigo-600 hover:text-indigo-500">
              登录
            </Link>
          </p>
        </div>
      </div>
    </div>
  );
}
