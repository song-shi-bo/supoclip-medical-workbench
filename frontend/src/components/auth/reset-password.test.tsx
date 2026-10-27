import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi } from "vitest";

import { resetPassword } from "../../lib/auth-client";

vi.mock("../../lib/auth-client", () => ({
  resetPassword: vi.fn(),
}));

import { ResetPassword } from "./reset-password";

describe("ResetPassword", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.history.replaceState(null, "", "/reset-password");
  });

  it("shows the missing-token notice when no token is in the URL", async () => {
    render(<ResetPassword />);
    expect(await screen.findByTestId("reset-password-missing-token")).toHaveTextContent(
      "缺少重置链接"
    );
  });

  it("rejects mismatched passwords without calling the API", async () => {
    window.history.replaceState(null, "", "/reset-password?token=tok123");
    const user = userEvent.setup();

    render(<ResetPassword />);

    await user.type(screen.getByTestId("reset-password-new"), "NewPass123!");
    await user.type(screen.getByTestId("reset-password-confirm"), "NewPass456!");
    await user.click(screen.getByTestId("reset-password-submit"));

    expect(await screen.findByTestId("reset-password-error")).toHaveTextContent(
      "两次输入的密码不一致"
    );
    expect(resetPassword).not.toHaveBeenCalled();
  });

  it("resets the password and links back to sign-in", async () => {
    window.history.replaceState(null, "", "/reset-password?token=tok123");
    vi.mocked(resetPassword).mockResolvedValue({ error: null } as never);
    const user = userEvent.setup();

    render(<ResetPassword />);

    await user.type(screen.getByTestId("reset-password-new"), "NewPass123!");
    await user.type(screen.getByTestId("reset-password-confirm"), "NewPass123!");
    await user.click(screen.getByTestId("reset-password-submit"));

    expect(await screen.findByTestId("reset-password-success")).toHaveTextContent(
      "密码已更新"
    );
    expect(resetPassword).toHaveBeenCalledWith({ newPassword: "NewPass123!", token: "tok123" });
    expect(screen.getByTestId("reset-password-go-signin")).toHaveAttribute("href", "/studio/sign-in");
  });

  it("surfaces API errors for expired tokens", async () => {
    window.history.replaceState(null, "", "/reset-password?token=expired");
    vi.mocked(resetPassword).mockResolvedValue({
      error: { message: "Token expired" },
    } as never);
    const user = userEvent.setup();

    render(<ResetPassword />);

    await user.type(screen.getByTestId("reset-password-new"), "NewPass123!");
    await user.type(screen.getByTestId("reset-password-confirm"), "NewPass123!");
    await user.click(screen.getByTestId("reset-password-submit"));

    await waitFor(() => {
      expect(screen.getByTestId("reset-password-error")).toHaveTextContent("Token expired");
    });
  });
});
