import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi } from "vitest";

const { useSessionMock, signOutMock, signInEmailMock, signUpEmailMock, reloadMock } = vi.hoisted(
  () => ({
    useSessionMock: vi.fn(),
    signOutMock: vi.fn(),
    signInEmailMock: vi.fn(),
    signUpEmailMock: vi.fn(),
    reloadMock: vi.fn(),
  })
);

vi.mock("@/lib/auth-client", () => ({
  useSession: useSessionMock,
  signOut: signOutMock,
  signIn: { email: signInEmailMock },
  signUp: { email: signUpEmailMock },
}));

// 组件内登录成功后整页刷新
Object.defineProperty(window, "location", {
  value: { ...window.location, reload: reloadMock, href: "http://localhost/studio" },
  writable: true,
  configurable: true,
});

import { StudioAccountBar } from "./studio-account-bar";

describe("StudioAccountBar（未登录，中文）", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    sessionStorage.clear();
    useSessionMock.mockReturnValue({ data: null, isPending: false, error: null });
  });

  it("顶部账户条提供「登录」「注册」按钮", () => {
    render(<StudioAccountBar />);
    expect(screen.getByTestId("studio-account-signin")).toHaveTextContent("登录");
    expect(screen.getByTestId("studio-account-signup")).toHaveTextContent("注册");
    expect(screen.queryByTestId("studio-signout")).not.toBeInTheDocument();
  });

  it("登录弹窗为中文表单，可原地登录（成功后刷新本页，不跳转）", async () => {
    signInEmailMock.mockResolvedValue({ error: null });
    const user = userEvent.setup();

    render(<StudioAccountBar />);
    await user.click(screen.getByTestId("studio-account-signin"));

    // 默认登录 Tab，中文占位
    expect(await screen.findByTestId("studio-signin-dialog")).toBeInTheDocument();
    expect(screen.getByTestId("studio-auth-tab-signin")).toHaveTextContent("登录");
    expect(screen.getByTestId("signin-email")).toHaveAttribute("placeholder", "邮箱");
    expect(screen.getByTestId("signin-password")).toHaveAttribute("placeholder", "密码");
    expect(screen.getByTestId("signin-submit")).toHaveTextContent("登录");

    await user.type(screen.getByTestId("signin-email"), "u@example.com");
    await user.type(screen.getByTestId("signin-password"), "Passw0rd!234");
    await user.click(screen.getByTestId("signin-submit"));

    expect(await screen.findByTestId("signin-success")).toHaveTextContent("登录成功");
    expect(signInEmailMock).toHaveBeenCalledWith({ email: "u@example.com", password: "Passw0rd!234" });
    await vi.waitFor(() => {
      expect(reloadMock).toHaveBeenCalled();
    });
  });

  it("登录失败时弹窗内显示中文错误（不离开页面）", async () => {
    signInEmailMock.mockResolvedValue({
      error: { code: "INVALID_EMAIL_OR_PASSWORD", message: "Invalid email or password" },
    } as never);
    const user = userEvent.setup();

    render(<StudioAccountBar />);
    await user.click(screen.getByTestId("studio-account-signin"));

    await user.type(await screen.findByTestId("signin-email"), "u@example.com");
    await user.type(screen.getByTestId("signin-password"), "wrong");
    await user.click(screen.getByTestId("signin-submit"));

    expect(await screen.findByTestId("signin-error")).toHaveTextContent("邮箱或密码错误");
    expect(reloadMock).not.toHaveBeenCalled();
  });

  it("注册 Tab 为中文表单，提交走注册接口", async () => {
    signUpEmailMock.mockResolvedValue({ error: null } as never);
    const user = userEvent.setup();

    render(<StudioAccountBar />);
    await user.click(screen.getByTestId("studio-account-signup"));

    expect(await screen.findByTestId("studio-signin-dialog")).toBeInTheDocument();
    expect(screen.getByTestId("signup-name")).toHaveAttribute("placeholder", "姓名");
    expect(screen.getByTestId("signup-submit")).toHaveTextContent("注册");

    await user.type(screen.getByTestId("signup-name"), "张三");
    await user.type(screen.getByTestId("signup-email"), "zhang@example.com");
    await user.type(screen.getByTestId("signup-password"), "Passw0rd!234");
    await user.click(screen.getByTestId("signup-submit"));

    expect(signUpEmailMock).toHaveBeenCalledWith({
      email: "zhang@example.com",
      password: "Passw0rd!234",
      name: "张三",
    });
  });

  it("注册被总闸拦截时显示中文错误（生产 DISABLE_SIGN_UP）", async () => {
    signUpEmailMock.mockResolvedValue({
      error: {
        status: 400,
        code: "EMAIL_PASSWORD_SIGN_UP_DISABLED",
        message: "Email and password sign up is not enabled",
      },
    } as never);
    const user = userEvent.setup();

    render(<StudioAccountBar />);
    await user.click(screen.getByTestId("studio-account-signup"));

    await user.type(await screen.findByTestId("signup-name"), "张三");
    await user.type(screen.getByTestId("signup-email"), "zhang@example.com");
    await user.type(screen.getByTestId("signup-password"), "Passw0rd!234");
    await user.click(screen.getByTestId("signup-submit"));

    expect(await screen.findByTestId("signup-error")).toHaveTextContent("当前未开放注册");
  });
});

describe("StudioAccountBar（已登录，中文）", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    sessionStorage.clear();
  });

  it("显示邮箱与「退出」按钮", async () => {
    signOutMock.mockResolvedValue(undefined);
    useSessionMock.mockReturnValue({
      data: { user: { id: "u1", email: "u@example.com", emailVerified: true } },
      isPending: false,
      error: null,
    });
    render(<StudioAccountBar />);

    expect(screen.getByTestId("studio-account-email")).toHaveTextContent("u@example.com");
    expect(screen.queryByTestId("studio-account-signin")).not.toBeInTheDocument();

    const user = userEvent.setup();
    await user.click(screen.getByTestId("studio-signout"));
    expect(signOutMock).toHaveBeenCalled();
    expect(sessionStorage.getItem("supoclip.justSignedOut")).toBe("1");
  });
});

describe("StudioAccountBar（账号名登录模式，NEXT_PUBLIC_AUTH_USERNAME_LOGIN=true）", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    sessionStorage.clear();
    useSessionMock.mockReturnValue({ data: null, isPending: false, error: null });
    vi.stubEnv("NEXT_PUBLIC_AUTH_USERNAME_LOGIN", "true");
  });
  afterEach(() => {
    vi.unstubAllEnvs();
  });

  async function barElement(): Promise<React.ReactElement> {
    const mod = await import("./studio-account-bar");
    return <mod.StudioAccountBar />;
  }

  it("登录弹窗显示「账号」字段；注册弹窗显示「账号名」字段", async () => {
    const user = userEvent.setup();
    render(await barElement());

    await user.click(screen.getByTestId("studio-account-signin"));
    expect(await screen.findByTestId("signin-account")).toHaveAttribute("placeholder", "账号");
    expect(screen.queryByTestId("signin-email")).not.toBeInTheDocument();

    await user.click(screen.getByTestId("studio-auth-tab-signup"));
    expect(await screen.findByTestId("signup-username")).toHaveAttribute("placeholder", "账号名");
  });

  it("重复账号名注册在弹窗内显示中文错误", async () => {
    signUpEmailMock.mockResolvedValue({
      error: {
        status: 400,
        code: "USERNAME_IS_ALREADY_TAKEN",
        message: "Username is already taken. Please try another.",
      },
    } as never);
    const user = userEvent.setup();
    render(await barElement());

    await user.click(screen.getByTestId("studio-account-signup"));
    await user.type(await screen.findByTestId("signup-username"), "user1");
    await user.type(screen.getByTestId("signup-name"), "张三");
    await user.type(screen.getByTestId("signup-email"), "fresh@example.com");
    await user.type(screen.getByTestId("signup-password"), "Passw0rd!234");
    await user.click(screen.getByTestId("signup-submit"));

    expect(await screen.findByTestId("signup-error")).toHaveTextContent("该账号名已被占用");
  });
});
