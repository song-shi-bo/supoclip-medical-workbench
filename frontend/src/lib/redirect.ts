// 登录后回跳的安全校验：只允许站内路径，拒绝协议相对地址（//host）、
// 外链与任何带 scheme 的目标，防止 open redirect。
// 缺省回工作台：公网入口（3445）的 `/` 归 DSH 平台侧（303→DSH 登录），
// 不能作为登录后的缺省落点（阶段 5 生产试点实测）。

export function safeInternalRedirect(target: string | null | undefined, fallback = "/studio"): string {
  if (!target) {
    return fallback;
  }

  const trimmed = target.trim();
  if (!trimmed.startsWith("/")) {
    return fallback;
  }

  // "//host" 或 "/\" 会被浏览器当作协议相对/非常规地址
  if (trimmed.startsWith("//") || trimmed.startsWith("/\\")) {
    return fallback;
  }

  // 含反斜杠或控制字符一律拒绝，保守处理
  if (/[\u0000-\u001f\u007f]/.test(trimmed)) {
    return fallback;
  }

  return trimmed;
}
