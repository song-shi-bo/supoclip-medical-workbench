// 多账号阶段一（隔离环境）：前端认证 UI 开关。
// 生产默认全部关闭 —— 只有隔离开发/测试环境显式设置环境变量后才启用新流程，
// 保证本阶段代码合入后、未部署前生产行为零变化。

/**
 * 服务端 + 客户端共用：是否要求邮箱验证后才能登录/使用工作台。
 * 由 Better Auth 服务端配置（REQUIRE_EMAIL_VERIFICATION）与
 * 前端构建期内联变量（NEXT_PUBLIC_REQUIRE_EMAIL_VERIFICATION）共同表达，
 * 隔离环境两者同时打开。
 */
export const emailVerificationRequired =
  process.env.NEXT_PUBLIC_REQUIRE_EMAIL_VERIFICATION === "true";

/** 是否启用「测试邮箱」开发工具（读取本地 outbox，不发送真实邮件）。 */
export const testMailboxEnabled =
  process.env.NEXT_PUBLIC_ENABLE_TEST_MAILBOX === "true";

/**
 * 账号名登录（隔离环境）：登录页用「账号 + 密码」而不是「邮箱 + 密码」，
 * 注册表单额外要求账号名。生产/共线不设置 → 保持邮箱登录。
 */
export const usernameLoginEnabled =
  process.env.NEXT_PUBLIC_AUTH_USERNAME_LOGIN === "true";

/**
 * 工作台 SessionGate 总开关（阶段 1 教训）：
 * 3107 是 dev 服务会热加载工作区代码，公网入口的 /studio 也由它承载；
 * 门禁重定向依赖 /sign-in 与 /api/auth/* 的入口路由（M3 部署窗口才就绪）。
 * 因此门禁必须显式开启才生效：生产/共线环境不设置 → 完全保持旧行为。
 */
export const sessionGateEnabled =
  process.env.NEXT_PUBLIC_SESSION_GATE_ENABLED === "true";
