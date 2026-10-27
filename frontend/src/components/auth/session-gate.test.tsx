import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi } from "vitest";

const { useSessionMock, signOutMock, sendVerificationEmailMock, replaceMock } = vi.hoisted(() => ({
  useSessionMock: vi.fn(),
  signOutMock: vi.fn(),
  sendVerificationEmailMock: vi.fn(),
  replaceMock: vi.fn(),
}));

vi.mock("@/lib/auth-client", () => ({
  useSession: useSessionMock,
  signOut: signOutMock,
  sendVerificationEmail: sendVerificationEmailMock,
}));

function stubLocation(path: string) {
  Object.defineProperty(window, "location", {
    value: {
      pathname: path,
      search: "",
      origin: "http://localhost:3000",
      href: `http://localhost:3000${path}`,
      replace: replaceMock,
    },
    writable: true,
    configurable: true,
  });
}

function setSession(session: unknown, isPending = false) {
  useSessionMock.mockReturnValue({ data: session, isPending, error: null });
}

const verifiedSession = {
  user: { id: "u1", email: "u@example.com", name: "U", emailVerified: true },
  session: { id: "s1" },
};
const unverifiedSession = {
  user: { id: "u2", email: "v@example.com", name: "V", emailVerified: false },
  session: { id: "s2" },
};

async function gateElement(): Promise<React.ReactElement> {
  const { SessionGate } = await import("./session-gate");
  return (
    <SessionGate>
      <div data-testid="gate-children">workbench</div>
    </SessionGate>
  );
}

describe("SessionGate", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
    sessionStorage.clear();
    stubLocation("/studio");
    // 门禁行为用例默认在「开关开启」环境下运行
    vi.stubEnv("NEXT_PUBLIC_SESSION_GATE_ENABLED", "true");
  });

  it("renders children without any session checks when the gate is disabled (production default)", async () => {
    vi.stubEnv("NEXT_PUBLIC_SESSION_GATE_ENABLED", "");
    setSession(null);
    render(await gateElement());
    expect(screen.getByTestId("gate-children")).toBeInTheDocument();
    expect(replaceMock).not.toHaveBeenCalled();
    // 关闭时不发起会话查询
    expect(useSessionMock).not.toHaveBeenCalled();
  });

  it("shows a loading state while the session is pending", async () => {
    setSession(null, true);
    render(await gateElement());
    expect(screen.getByTestId("session-gate-loading")).toBeInTheDocument();
    expect(screen.queryByTestId("gate-children")).not.toBeInTheDocument();
  });

  it("renders children for a signed-in verified user", async () => {
    vi.stubEnv("NEXT_PUBLIC_REQUIRE_EMAIL_VERIFICATION", "true");
    setSession(verifiedSession);
    render(await gateElement());
    expect(screen.getByTestId("gate-children")).toBeInTheDocument();
    expect(sessionStorage.getItem("supoclip.hadSession")).toBe("1");
  });

  it("blocks unverified users from the workbench when verification is required", async () => {
    vi.stubEnv("NEXT_PUBLIC_REQUIRE_EMAIL_VERIFICATION", "true");
    sendVerificationEmailMock.mockResolvedValue({ error: null });
    setSession(unverifiedSession);
    render(await gateElement());

    expect(screen.getByTestId("session-gate-unverified")).toHaveTextContent(
      "需要验证邮箱"
    );
    expect(screen.getByTestId("unverified-status")).toHaveTextContent("当前状态：未验证");
    expect(screen.queryByTestId("gate-children")).not.toBeInTheDocument();

    // 重发验证邮件
    const user = userEvent.setup();
    await user.click(screen.getByTestId("unverified-resend"));
    expect(await screen.findByTestId("unverified-resend-sent")).toBeInTheDocument();
    expect(sendVerificationEmailMock).toHaveBeenCalledWith(
      expect.objectContaining({ email: "v@example.com" })
    );
  });

  it("renders children for unverified users when verification is not required (production default)", async () => {
    vi.stubEnv("NEXT_PUBLIC_REQUIRE_EMAIL_VERIFICATION", "");
    setSession(unverifiedSession);
    render(await gateElement());
    expect(screen.getByTestId("gate-children")).toBeInTheDocument();
  });

  it("redirects first-time visitors to sign-in carrying the original path", async () => {
    vi.stubEnv("NEXT_PUBLIC_REQUIRE_EMAIL_VERIFICATION", "");
    setSession(null);
    render(await gateElement());
    expect(replaceMock).toHaveBeenCalledWith("/studio/sign-in?redirect=%2Fstudio");
  });

  it("reports an expired session after previously having one", async () => {
    vi.stubEnv("NEXT_PUBLIC_REQUIRE_EMAIL_VERIFICATION", "");
    sessionStorage.setItem("supoclip.hadSession", "1");
    setSession(null);
    render(await gateElement());
    expect(replaceMock).toHaveBeenCalledWith(
      "/studio/sign-in?redirect=%2Fstudio&reason=expired"
    );
  });

  it("reports a signed-out state after explicit sign-out", async () => {
    vi.stubEnv("NEXT_PUBLIC_REQUIRE_EMAIL_VERIFICATION", "");
    sessionStorage.setItem("supoclip.justSignedOut", "1");
    setSession(null);
    render(await gateElement());
    expect(replaceMock).toHaveBeenCalledWith(
      "/studio/sign-in?redirect=%2Fstudio&reason=signed-out"
    );
  });
});
