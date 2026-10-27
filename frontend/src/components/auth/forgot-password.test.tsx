import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi } from "vitest";

import { requestPasswordReset } from "../../lib/auth-client";

vi.mock("../../lib/auth-client", () => ({
  requestPasswordReset: vi.fn(),
}));

import { ForgotPassword } from "./forgot-password";

describe("ForgotPassword", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("shows a generic success message after submitting", async () => {
    vi.mocked(requestPasswordReset).mockResolvedValue({ error: null } as never);
    const user = userEvent.setup();

    render(<ForgotPassword />);

    await user.type(screen.getByTestId("forgot-password-email"), "user@example.com");
    await user.click(screen.getByTestId("forgot-password-submit"));

    expect(await screen.findByTestId("forgot-password-success")).toHaveTextContent(
      "请查收邮箱"
    );
    // 防枚举：无论邮箱是否存在文案一致
    expect(requestPasswordReset).toHaveBeenCalledWith(
      expect.objectContaining({ email: "user@example.com" })
    );
  });

  it("shows an error when the request fails", async () => {
    vi.mocked(requestPasswordReset).mockResolvedValue({
      error: { message: "Reset email is not enabled" },
    } as never);
    const user = userEvent.setup();

    render(<ForgotPassword />);

    await user.type(screen.getByTestId("forgot-password-email"), "user@example.com");
    await user.click(screen.getByTestId("forgot-password-submit"));

    expect(await screen.findByTestId("forgot-password-error")).toHaveTextContent(
      "Reset email is not enabled"
    );
  });
});
