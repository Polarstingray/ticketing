import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Login from "./Login";
import { api } from "../api";

// useAuth is mocked: Login only reads `user`/`login`/`loading` from it, and no
// test here needs a real session — `login` is a spy so onSubmit is exercised.
const authState = vi.hoisted(() => ({ user: null, loading: false }));
const loginSpy = vi.hoisted(() => vi.fn());
vi.mock("../auth/AuthContext", () => ({
  useAuth: () => ({ user: authState.user, loading: authState.loading, login: loginSpy }),
}));

vi.mock("../api", () => ({
  api: { appConfig: vi.fn() },
}));

function renderLogin() {
  return render(
    <MemoryRouter initialEntries={["/login"]}>
      <Login />
    </MemoryRouter>
  );
}

// The username/password <label>s aren't associated with their <input>s (no
// htmlFor/id — a pre-existing gap in Login.jsx, not something this feature
// introduces), so getByLabelText can't find them. autoComplete is reliable
// and already present on both.
function usernameInput(container) {
  return container.querySelector('input[autocomplete="username"]');
}
function passwordInput(container) {
  return container.querySelector('input[autocomplete="current-password"]');
}

beforeEach(() => {
  vi.clearAllMocks();
  authState.user = null;
  authState.loading = false;
});

describe("the demo credentials hint", () => {
  it("renders nothing extra on an ordinary deployment", async () => {
    api.appConfig.mockResolvedValue({
      read_only: false, demo_username: null, demo_password: null,
    });
    renderLogin();
    await waitFor(() => expect(api.appConfig).toHaveBeenCalled());
    expect(screen.queryByText(/read-only/i)).not.toBeInTheDocument();
    expect(screen.queryByText("admin")).not.toBeInTheDocument();
  });

  it("shows the credentials and a read-only note when both are present", async () => {
    api.appConfig.mockResolvedValue({
      read_only: true, demo_username: "admin", demo_password: "demopass123",
    });
    renderLogin();
    expect(await screen.findByText("admin")).toBeInTheDocument();
    expect(screen.getByText("demopass123")).toBeInTheDocument();
    expect(screen.getByText(/read-only public demo/i)).toBeInTheDocument();
  });

  it("shows the credentials without the read-only note when only shown, not locked", async () => {
    api.appConfig.mockResolvedValue({
      read_only: false, demo_username: "admin", demo_password: "demopass123",
    });
    renderLogin();
    expect(await screen.findByText("admin")).toBeInTheDocument();
    expect(screen.queryByText(/read-only public demo/i)).not.toBeInTheDocument();
  });

  it("does not break the login form if the config fetch rejects", async () => {
    api.appConfig.mockRejectedValue(new Error("network error"));
    const { container } = renderLogin();
    await waitFor(() => expect(api.appConfig).toHaveBeenCalled());
    // The form itself is still usable regardless of the hint's fate.
    expect(usernameInput(container)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Sign in" })).toBeInTheDocument();
  });
});

describe("signing in", () => {
  it("calls login with the entered credentials", async () => {
    api.appConfig.mockResolvedValue({ read_only: false, demo_username: null, demo_password: null });
    loginSpy.mockResolvedValue({ id: 1 });
    const { container } = renderLogin();

    fireEvent.change(usernameInput(container), { target: { value: "admin" } });
    fireEvent.change(passwordInput(container), { target: { value: "demopass123" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

    await waitFor(() => expect(loginSpy).toHaveBeenCalledWith("admin", "demopass123"));
  });

  it("shows the read-only guard's message when a write-triggering login fails that way", async () => {
    api.appConfig.mockResolvedValue({ read_only: false, demo_username: null, demo_password: null });
    loginSpy.mockRejectedValue(new Error("This is a read-only public demo, so nothing here writes."));
    const { container } = renderLogin();

    fireEvent.change(usernameInput(container), { target: { value: "admin" } });
    fireEvent.change(passwordInput(container), { target: { value: "wrong" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByText(/read-only public demo/i)).toBeInTheDocument();
  });
});
