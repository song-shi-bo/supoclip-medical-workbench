import Link from "next/link";
import { headers } from "next/headers";
import { auth } from "@/lib/auth";
import prisma from "@/lib/prisma";
import { AdminUserToggle } from "@/components/admin/admin-user-toggle";
import {
  RuntimeSettingsForm,
  type RuntimeSetting,
} from "@/components/admin/runtime-settings-form";
import { Badge } from "@/components/ui/badge";
import { fetchBackend } from "@/server/backend-api";

export default async function AdminPage() {
  const session = await auth.api.getSession({ headers: await headers() });

  if (!session?.user) {
    return (
      <main className="mx-auto max-w-2xl px-6 py-16">
        <h1 className="text-2xl font-semibold">Admin</h1>
        <p className="mt-3 text-sm text-gray-600">You need to sign in to view this page.</p>
        <Link href="/sign-in" className="mt-6 inline-block text-sm font-medium text-black underline">
          Go to sign in
        </Link>
      </main>
    );
  }

  const isAdmin = Boolean((session.user as { is_admin?: boolean }).is_admin);

  if (!isAdmin) {
    return (
      <main className="mx-auto max-w-2xl px-6 py-16">
        <h1 className="text-2xl font-semibold">Admin</h1>
        <p className="mt-3 text-sm text-gray-600">You are signed in, but your account is not an admin.</p>
      </main>
    );
  }

  const adminUserId = session.user.id;

  async function loadRuntimeSettings(): Promise<{
    settings: RuntimeSetting[];
    error: string | null;
  }> {
    try {
      const response = await fetchBackend("/admin/runtime-settings", {
        method: "GET",
        userId: adminUserId,
        cache: "no-store",
      });
      if (!response.ok) {
        return { settings: [], error: "Unable to load runtime settings." };
      }
      const payload = (await response.json()) as { settings?: RuntimeSetting[] };
      return { settings: payload.settings ?? [], error: null };
    } catch {
      return { settings: [], error: "Unable to reach the backend settings API." };
    }
  }

  const [runtimeSettings, totalUsers, adminUsers, recentUsers] = await Promise.all([
    loadRuntimeSettings(),
    prisma.user.count(),
    prisma.user.count({ where: { is_admin: true } }),
    prisma.user.findMany({
      orderBy: { createdAt: "desc" },
      take: 30,
      select: {
        id: true,
        email: true,
        name: true,
        is_admin: true,
        plan: true,
        createdAt: true,
      },
    }),
  ]);

  return (
    <main className="mx-auto max-w-6xl px-6 py-10">
      <div className="flex items-center justify-between gap-4">
        <div>
          <h1 className="text-3xl font-semibold">Admin Dashboard</h1>
          <p className="mt-2 text-sm text-gray-600">Manage users and platform runtime settings.</p>
        </div>
        <Link href="/studio" className="text-sm font-medium text-black underline">
          Back to Studio
        </Link>
      </div>

      <section className="mt-8 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <div className="rounded-lg border border-gray-200 bg-white p-4">
          <p className="text-xs uppercase tracking-wide text-gray-500">Total users</p>
          <p className="mt-2 text-2xl font-semibold text-black">{totalUsers}</p>
        </div>
        <div className="rounded-lg border border-gray-200 bg-white p-4">
          <p className="text-xs uppercase tracking-wide text-gray-500">Admins</p>
          <p className="mt-2 text-2xl font-semibold text-black">{adminUsers}</p>
        </div>
      </section>

      <section className="mt-8 rounded-lg border border-gray-200 bg-white">
        <div className="border-b border-gray-200 px-4 py-3">
          <h2 className="text-lg font-medium">Runtime Settings</h2>
          <p className="text-sm text-gray-600">Configure provider keys and model settings without editing env files.</p>
        </div>
        {runtimeSettings.error ? (
          <div className="px-4 py-5 text-sm text-red-700">{runtimeSettings.error}</div>
        ) : (
          <RuntimeSettingsForm settings={runtimeSettings.settings} />
        )}
      </section>

      <section className="mt-8 rounded-lg border border-gray-200 bg-white">
        <div className="border-b border-gray-200 px-4 py-3">
          <h2 className="text-lg font-medium">Users</h2>
          <p className="text-sm text-gray-600">Most recent users. Toggle admin access.</p>
        </div>

        <div className="overflow-x-auto">
          <table className="min-w-full divide-y divide-gray-200">
            <thead className="bg-gray-50">
              <tr>
                <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wide text-gray-500">User</th>
                <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wide text-gray-500">Plan</th>
                <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wide text-gray-500">Role</th>
                <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wide text-gray-500">Created</th>
                <th className="px-4 py-3 text-right text-xs font-medium uppercase tracking-wide text-gray-500">Action</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-200 bg-white">
              {recentUsers.map((user) => (
                <tr key={user.id}>
                  <td className="px-4 py-3">
                    <p className="text-sm font-medium text-black">{user.name || "Unnamed user"}</p>
                    <p className="text-xs text-gray-600">{user.email}</p>
                  </td>
                  <td className="px-4 py-3">
                    <Badge variant="outline" className="capitalize">
                      {user.plan}
                    </Badge>
                  </td>
                  <td className="px-4 py-3">
                    {user.is_admin ? (
                      <Badge className="bg-black text-white">Admin</Badge>
                    ) : (
                      <Badge variant="outline">User</Badge>
                    )}
                  </td>
                  <td className="px-4 py-3 text-sm text-gray-600">{user.createdAt.toLocaleDateString()}</td>
                  <td className="px-4 py-3 text-right">
                    <AdminUserToggle
                      userId={user.id}
                      isAdmin={user.is_admin}
                      isCurrentUser={user.id === session.user.id}
                    />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </main>
  );
}
