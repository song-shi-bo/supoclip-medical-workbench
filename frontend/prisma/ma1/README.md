# 隔离环境 Prisma schema（多账号阶段一）

- 用途：隔离开发/测试实例（3207，库 5433/supoclip_test）的账号名登录（Better Auth username 插件）。
- 与生产 schema（../schema.prisma）的唯一差异：User 模型多 `username`(unique) / `displayUsername` 两个可空字段，生成目录为 `src/generated/prisma-ma1`。
- 生成：`DATABASE_URL=postgresql://ma1admin@127.0.0.1:5433/supoclip_test pnpm exec prisma generate --schema prisma/ma1/schema.prisma`
- 运行时选择：`AUTH_USERNAME_LOGIN=true` 时 `src/lib/auth.ts` 加载 prisma-ma1 客户端；生产（3107）始终加载 `src/generated/prisma`（不含 username 查询）。
- 生产数据库（5432）不使用本 schema；生产迁移（含 username 列）属 M3 窗口，需单独批准。
