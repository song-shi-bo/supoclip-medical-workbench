import { SignUp } from "@/components/auth/sign-up";
import { AuthShell } from "@/components/auth/auth-shell";
import Link from "next/link";
import { AUTH_PATHS } from "@/lib/auth-paths";

export default function SignUpPage() {
  return (
    <AuthShell>
      <div className="w-full max-w-md space-y-6">
        <SignUp />
        <div className="text-center">
          <p className="text-sm text-violet-200">
            已有账号？{" "}
            <Link
              href={AUTH_PATHS.signIn}
              className="font-medium text-white underline-offset-4 hover:underline"
            >
              登录
            </Link>
          </p>
        </div>
      </div>
    </AuthShell>
  );
}
