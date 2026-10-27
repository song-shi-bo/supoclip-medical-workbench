import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi } from "vitest";

const { signInUsernameMock, signInEmailMock, sendVerificationEmailMock } = vi.hoisted(() => ({
  signInUsernameMock: vi.fn(),
  signInEmailMock: vi.fn(),
  sendVerificationEmailMock: vi.fn(),
}));

vi.mock("../../lib/auth-client", () => ({
  signIn: {
    email: signInEmailMock,
    username: signInUsernameMock,
  },
  sendVerificationEmail: sendVerificationEmailMock,
}));

import { SignInForm } from "./sign-in";

describe("SignInForm（账号名登录模式，隔离环境）", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("显示「账号」输入框而非邮箱，提交调用 signIn.username 并规范化", async () => {
    signInUsernameMock.mockResolvedValue({ error: null });
    const user = userEvent.setup();

    render(<SignInForm identifierMode="username" showForgotLink={false} onAuthenticated={() => {}} />);

    expect(screen.getByTestId("signin-account")).toHaveAttribute("placeholder", "账号");
    expect(screen.queryByTestId("signin-email")).not.toBeInTheDocument();

    await user.type(screen.getByTestId("signin-account"), "  User1  ");
    await user.type(screen.getByTestId("signin-password"), "Passw0rd!234");
    await user.click(screen.getByTestId("signin-submit"));

    expect(await screen.findByTestId("signin-success")).toHaveTextContent("登录成功");
    // 规范化：去空格 + 转小写
    expect(signInUsernameMock).toHaveBeenCalledWith({
      username: "user1",
      password: "Passw0rd!234",
    });
    expect(signInEmailMock).not.toHaveBeenCalled();
  });

  it("错误账号或密码显示中文提示（防枚举统一文案）", async () => {
    signInUsernameMock.mockResolvedValue({
      error: { status: 401, code: "INVALID_USERNAME_OR_PASSWORD", message: "Invalid username or password" },
    } as never);
    const user = userEvent.setup();

    render(<SignInForm identifierMode="username" showForgotLink={false} />);

    await user.type(screen.getByTestId("signin-account"), "ghost");
    await user.type(screen.getByTestId("signin-password"), "whatever123");
    await user.click(screen.getByTestId("signin-submit"));

    expect(await screen.findByTestId("signin-error")).toHaveTextContent("账号或密码错误");
  });

  it("未验证账号在账号模式下给出中文指引（邮箱不回显）", async () => {
    signInUsernameMock.mockResolvedValue({
      error: { status: 403, code: "EMAIL_NOT_VERIFIED", message: "Email not verified" },
    } as never);
    const user = userEvent.setup();

    render(<SignInForm identifierMode="username" showForgotLink={false} />);

    await user.type(screen.getByTestId("signin-account"), "newuser");
    await user.type(screen.getByTestId("signin-password"), "NewUser123!");
    await user.click(screen.getByTestId("signin-submit"));

    expect(await screen.findByTestId("signin-unverified")).toHaveTextContent("邮箱未验证");
    expect(screen.getByTestId("signin-unverified-hint")).toBeInTheDocument();
  });
});
