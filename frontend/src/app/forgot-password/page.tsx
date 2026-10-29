import { ForgotPassword } from "@/components/auth/forgot-password";
import { AuthShell } from "@/components/auth/auth-shell";

export default function ForgotPasswordPage() {
  return (
    <AuthShell>
      <div className="w-full max-w-md">
        <ForgotPassword />
      </div>
    </AuthShell>
  );
}
