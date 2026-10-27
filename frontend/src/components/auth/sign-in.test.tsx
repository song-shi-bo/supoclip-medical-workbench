import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi } from "vitest";

import { SignIn } from "./sign-in";
import { signIn, sendVerificationEmail } from "../../lib/auth-client";

const pushMock = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({
    push: pushMock,
    refresh: vi.fn(),
  }),
  useSearchParams: () => searchParamsMock(),
}));

let currentParams = new URLSearchParams();
function searchParamsMock() {
  return currentParams;
}

vi.mock("../../lib/auth-client", () => ({
  signIn: {
    email: vi.fn(),
  },
  sendVerificationEmail: vi.fn(),
}));

describe("SignIn", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    currentParams = new URLSearchParams();
  });

  it("shows an error message when sign in fails", async () => {
    vi.mocked(signIn.email).mockResolvedValue({
      error: { message: "Invalid credentials" },
    } as never);
    const user = userEvent.setup();

    render(<SignIn />);

    await user.type(screen.getByTestId("signin-email"), "user@example.com");
    await user.type(screen.getByTestId("signin-password"), "Password123!");
    await user.click(screen.getByTestId("signin-submit"));

    expect(await screen.findByTestId("signin-error")).toHaveTextContent(
      "Invalid credentials"
    );
  });

  it("shows a success state and redirects to the default page", async () => {
    vi.mocked(signIn.email).mockResolvedValue({ error: null } as never);
    const user = userEvent.setup();

    render(<SignIn />);

    await user.type(screen.getByTestId("signin-email"), "user@example.com");
    await user.type(screen.getByTestId("signin-password"), "Password123!");
    await user.click(screen.getByTestId("signin-submit"));

    expect(await screen.findByTestId("signin-success")).toHaveTextContent(
      "登录成功"
    );
    await vi.waitFor(() => {
      expect(pushMock).toHaveBeenCalledWith("/studio");
    });
  });

  it("redirects to the ?redirect= target after successful sign in", async () => {
    currentParams = new URLSearchParams("redirect=%2Fstudio%3Fproject%3Dabc");
    vi.mocked(signIn.email).mockResolvedValue({ error: null } as never);
    const user = userEvent.setup();

    render(<SignIn />);

    await user.type(screen.getByTestId("signin-email"), "user@example.com");
    await user.type(screen.getByTestId("signin-password"), "Password123!");
    await user.click(screen.getByTestId("signin-submit"));

    await vi.waitFor(() => {
      expect(pushMock).toHaveBeenCalledWith("/studio?project=abc");
    });
  });

  it("ignores unsafe redirect targets", async () => {
    currentParams = new URLSearchParams("redirect=https%3A%2F%2Fevil.example.com");
    vi.mocked(signIn.email).mockResolvedValue({ error: null } as never);
    const user = userEvent.setup();

    render(<SignIn />);

    await user.type(screen.getByTestId("signin-email"), "user@example.com");
    await user.type(screen.getByTestId("signin-password"), "Password123!");
    await user.click(screen.getByTestId("signin-submit"));

    await vi.waitFor(() => {
      expect(pushMock).toHaveBeenCalledWith("/studio");
    });
  });

  it("shows a session expired notice when reason=expired", () => {
    currentParams = new URLSearchParams("reason=expired");
    render(<SignIn />);
    expect(screen.getByTestId("signin-session-expired")).toHaveTextContent(
      "会话已过期"
    );
  });

  it("shows a signed out notice when reason=signed-out", () => {
    currentParams = new URLSearchParams("reason=signed-out");
    render(<SignIn />);
    expect(screen.getByTestId("signin-signed-out")).toHaveTextContent("已退出登录");
  });

  it("shows the unverified state with resend action for EMAIL_NOT_VERIFIED", async () => {
    vi.mocked(signIn.email).mockResolvedValue({
      error: { status: 403, code: "EMAIL_NOT_VERIFIED", message: "Email is not verified" },
    } as never);
    vi.mocked(sendVerificationEmail).mockResolvedValue({ error: null } as never);
    const user = userEvent.setup();

    render(<SignIn />);

    await user.type(screen.getByTestId("signin-email"), "user@example.com");
    await user.type(screen.getByTestId("signin-password"), "Password123!");
    await user.click(screen.getByTestId("signin-submit"));

    expect(await screen.findByTestId("signin-unverified")).toHaveTextContent(
      "邮箱未验证"
    );
    expect(screen.queryByTestId("signin-error")).not.toBeInTheDocument();

    await user.click(screen.getByTestId("signin-resend-verification"));
    expect(await screen.findByTestId("signin-resend-sent")).toBeInTheDocument();
    expect(sendVerificationEmail).toHaveBeenCalledWith(
      expect.objectContaining({ email: "user@example.com" })
    );
  });

  it("links to the forgot password page (studio prefix)", () => {
    render(<SignIn />);
    expect(screen.getByTestId("signin-forgot-password")).toHaveAttribute(
      "href",
      "/studio/forgot-password"
    );
  });
});
