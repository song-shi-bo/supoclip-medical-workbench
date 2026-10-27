import { ForgotPassword } from "@/components/auth/forgot-password";

export default function ForgotPasswordPage() {
  return (
    <div className="min-h-screen flex items-center justify-center bg-gray-50 py-12 px-4 sm:px-6 lg:px-8">
      <div className="max-w-md w-full">
        <ForgotPassword />
      </div>
    </div>
  );
}
