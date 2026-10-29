import { ResetPassword } from "@/components/auth/reset-password";
import { AuthShell } from "@/components/auth/auth-shell";

export default function ResetPasswordPage() {
  return (
    <AuthShell>
      <div className="w-full max-w-md">
        <ResetPassword />
      </div>
    </AuthShell>
  );
}
