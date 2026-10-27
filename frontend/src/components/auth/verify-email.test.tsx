import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi } from "vitest";

import { verifyEmail, sendVerificationEmail } from "../../lib/auth-client";

vi.mock("../../lib/auth-client", () => ({
  verifyEmail: vi.fn(),
  sendVerificationEmail: vi.fn(),
}));

import { VerifyEmail } from "./verify-email";

function gotoUrl(search: string) {
  window.history.replaceState(null, "", search);
}

describe("VerifyEmail", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    gotoUrl("/verify-email");
  });

  it("starts verifying when a token is present and shows success", async () => {
    gotoUrl("/verify-email?token=tok123");
    vi.mocked(verifyEmail).mockResolvedValue({ error: null } as never);

    render(<VerifyEmail />);

    expect(await screen.findByTestId("verify-email-success")).toHaveTextContent(
      "邮箱验证成功"
    );
    expect(verifyEmail).toHaveBeenCalledWith({ query: { token: "tok123" } });
    expect(screen.getByTestId("verify-email-continue")).toHaveAttribute("href", "/studio/sign-in");
  });

  it("uses callbackURL as the continue target when provided", async () => {
    gotoUrl("/verify-email?token=tok123&callbackURL=%2Fstudio");
    vi.mocked(verifyEmail).mockResolvedValue({ error: null } as never);

    render(<VerifyEmail />);

    expect(await screen.findByTestId("verify-email-success")).toBeInTheDocument();
    expect(screen.getByTestId("verify-email-continue")).toHaveAttribute("href", "/studio");
  });

  it("shows the failure state for an invalid or expired token and offers resend", async () => {
    gotoUrl("/verify-email?token=expired-token");
    vi.mocked(verifyEmail).mockResolvedValue({
      error: { message: "Invalid token" },
    } as never);
    vi.mocked(sendVerificationEmail).mockResolvedValue({ error: null } as never);
    const user = userEvent.setup();

    render(<VerifyEmail />);

    expect(await screen.findByTestId("verify-email-error")).toHaveTextContent(
      "无效或已过期"
    );

    await user.type(screen.getByTestId("verify-email-input"), "user@example.com");
    await user.click(screen.getByTestId("verify-email-resend"));
    expect(await screen.findByTestId("verify-email-resend-sent")).toBeInTheDocument();
    expect(sendVerificationEmail).toHaveBeenCalledWith(
      expect.objectContaining({ email: "user@example.com" })
    );
  });

  it("prefills the email from ?email= and resends in the idle state", async () => {
    gotoUrl("/verify-email?email=prefilled@example.com");
    vi.mocked(sendVerificationEmail).mockResolvedValue({ error: null } as never);
    const user = userEvent.setup();

    render(<VerifyEmail />);

    expect(await screen.findByTestId("verify-email-input")).toHaveValue(
      "prefilled@example.com"
    );
    await user.click(screen.getByTestId("verify-email-resend"));
    expect(await screen.findByTestId("verify-email-resend-sent")).toBeInTheDocument();
  });
});
