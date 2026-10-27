import { betterAuth } from "better-auth";
import { prismaAdapter } from "better-auth/adapters/prisma";
import { nextCookies } from "better-auth/next-js";
import { username } from "better-auth/plugins/username";
import { deliverTestMail, pageLinkFor, testMailerConfigured } from "./test-mailer";

const disableSignUp = ["1", "true", "yes"].includes(
  (process.env.DISABLE_SIGN_UP ?? "").toLowerCase()
);
// 多账号阶段一（隔离环境）：邮箱验证开关。生产不设置该变量 → 行为与现状完全一致。
const requireEmailVerification =
  !disableSignUp &&
  ["1", "true", "yes"].includes(
    (process.env.REQUIRE_EMAIL_VERIFICATION ?? "").toLowerCase()
  );
// 账号名登录（Better Auth username 插件）。仅在隔离环境启用（AUTH_USERNAME_LOGIN=true）。
// 规范化：去首尾空格 + 转小写（登录大小写不敏感；带空格的输入也能正确匹配）。
const usernameLoginEnabled = process.env.AUTH_USERNAME_LOGIN === "true";

// Prisma 生成产物按环境隔离（见 prisma/ma1/README.md）：
// - 生产/共线（默认）：src/generated/prisma —— 模型不含 username，查询不引用该列；
// - 隔离环境（AUTH_USERNAME_LOGIN=true）：src/generated/prisma-ma1 —— User 含 username/displayUsername。
// 字面量分支保证打包器可静态解析两条路径。
function loadPrisma() {
  if (usernameLoginEnabled) {
    // eslint-disable-next-line @typescript-eslint/no-require-imports
    const { PrismaClient } = require("../generated/prisma-ma1") as typeof import("../generated/prisma-ma1");
    return new PrismaClient();
  }
  // eslint-disable-next-line @typescript-eslint/no-require-imports
  const { PrismaClient } = require("../generated/prisma");
  return new PrismaClient();
}

type PrismaClientLike = ReturnType<typeof loadPrisma>;
const prisma = loadPrisma();

function toOrigin(value?: string) {
  if (!value) {
    return null;
  }

  try {
    return new URL(value).origin;
  } catch {
    return null;
  }
}

const trustedOrigins = Array.from(
  new Set(
    [
      toOrigin(process.env.NEXT_PUBLIC_APP_URL),
      toOrigin(process.env.BETTER_AUTH_URL),
      "http://localhost:3107",
      "http://sp.localhost:3107",
      "http://supoclip.localhost:3107",
    ].filter((origin): origin is string => Boolean(origin))
  )
);

export const auth = betterAuth({
  database: prismaAdapter(prisma as never, {
    provider: "postgresql",
  }),
  user: {
    deleteUser: {
      enabled: true,
    },
    additionalFields: {
      is_admin: {
        type: "boolean",
        input: false,
      },
    },
  },
  trustedOrigins,
  emailAndPassword: {
    enabled: true,
    disableSignUp,
    requireEmailVerification,
    // 无 SMTP：隔离环境把「重置密码邮件」写入本地测试邮箱（outbox），不发真实邮件。
    // 生产不启用测试邮箱（ENABLE_TEST_MAIL_ROUTES 未设置），回调保持 undefined。
    sendResetPassword: testMailerConfigured()
      ? async ({ user, url, token }) => {
          await deliverTestMail({
            kind: "reset-password",
            to: user.email,
            subject: "Reset your password",
            url: pageLinkFor(url),
            token,
          });
        }
      : undefined,
  },
  emailVerification: {
    // 无 SMTP：隔离环境把「验证邮件」写入本地测试邮箱（outbox），不发真实邮件。
    sendVerificationEmail: testMailerConfigured()
      ? async ({ user, url, token }) => {
          await deliverTestMail({
            kind: "verification",
            to: user.email,
            subject: "Verify your email",
            url: pageLinkFor(url),
            token,
          });
        }
      : undefined,
    // 要求验证时，注册即自动发送验证邮件（Better Auth 默认行为相同，这里显式声明）
    sendOnSignUp: requireEmailVerification || undefined,
  },
  plugins: [
    // 账号名登录（仅隔离环境启用；username 插件的 /sign-in/username 同样强制邮箱验证策略）
    ...(usernameLoginEnabled
      ? [
          username({
            usernameNormalization: (value: string) => value.trim().toLowerCase(),
            // 校验在规范化之后执行：去空格/大小写归一后的值才进入长度与字符集校验
            validationOrder: { username: "pre-normalization" },
          }),
        ]
      : []),
    nextCookies(), // Enable Next.js cookie handling
  ],
});

export type Session = typeof auth.$Infer.Session;
