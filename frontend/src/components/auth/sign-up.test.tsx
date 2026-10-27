import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi } from "vitest";

import { signUp } from "../../lib/auth-client";

vi.mock("../../lib/auth-client", () => ({
  signUp: {
    email: vi.fn(),
  },
}));

// auth-flags 在模块加载时读取环境变量：每个用例组用 resetModules + stubEnv 控制开关
async function signUpElement(): Promise<React.ReactElement> {
  const { SignUp } = await import("./sign-up");
  return <SignUp />;
}

describe("SignUp (email verification required — isolated env)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    vi.stubEnv("NEXT_PUBLIC_REQUIRE_EMAIL_VERIFICATION", "true");
  });
  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("shows the awaiting-verification panel instead of signing in", async () => {
    vi.mocked(signUp.email).mockResolvedValue({ error: null } as never);
    const user = userEvent.setup();

    render(await signUpElement());

    await user.type(screen.getByTestId("signup-name"), "Jane Admin");
    await user.type(screen.getByTestId("signup-email"), "jane@example.com");
    await user.type(screen.getByTestId("signup-password"), "Password123!");
    await user.click(screen.getByTestId("signup-submit"));

    expect(await screen.findByTestId("signup-awaiting-verification")).toHaveTextContent(
      "请查收邮箱"
    );
    expect(screen.getByTestId("signup-awaiting-verification")).toHaveTextContent(
      "jane@example.com"
    );
    // 验证面板提供进入验证状态页的入口
    expect(screen.getByTestId("signup-go-verify")).toHaveAttribute(
      "href",
      "/studio/verify-email?email=jane%40example.com"
    );
  });

  it("shows API errors", async () => {
    vi.mocked(signUp.email).mockResolvedValue({
      error: { message: "Failed to create account" },
    } as never);
    const user = userEvent.setup();

    render(await signUpElement());

    await user.type(screen.getByTestId("signup-name"), "Jane Admin");
    await user.type(screen.getByTestId("signup-email"), "jane@example.com");
    await user.type(screen.getByTestId("signup-password"), "Password123!");
    await user.click(screen.getByTestId("signup-submit"));

    expect(await screen.findByTestId("signup-error")).toHaveTextContent(
      "Failed to create account"
    );
    expect(screen.queryByTestId("signup-awaiting-verification")).not.toBeInTheDocument();
  });
});

describe("SignUp (legacy behavior — verification off)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    vi.stubEnv("NEXT_PUBLIC_REQUIRE_EMAIL_VERIFICATION", "");
  });
  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("shows the legacy auto sign-in success state", async () => {
    vi.mocked(signUp.email).mockResolvedValue({ error: null } as never);
    const user = userEvent.setup();

    render(await signUpElement());

    await user.type(screen.getByTestId("signup-name"), "Jane Admin");
    await user.type(screen.getByTestId("signup-email"), "jane@example.com");
    await user.type(screen.getByTestId("signup-password"), "Password123!");
    await user.click(screen.getByTestId("signup-submit"));

    expect(await screen.findByTestId("signup-success")).toHaveTextContent(
      "注册成功"
    );
    expect(screen.queryByTestId("signup-awaiting-verification")).not.toBeInTheDocument();
  });
});
