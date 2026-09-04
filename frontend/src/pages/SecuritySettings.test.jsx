import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import SecuritySettings from "./SecuritySettings";
import { api } from "../api";

const authState = vi.hoisted(() => ({ user: { username: "admin", role: "admin" } }));
const loginSpy = vi.hoisted(() => vi.fn());
vi.mock("../auth/AuthContext", () => ({
  useAuth: () => ({ user: authState.user, login: loginSpy }),
}));

vi.mock("../api", () => ({
  api: {
    getSecuritySettings: vi.fn(),
    updateSecuritySettings: vi.fn(),
  },
}));

function reauthError() {
  const err = new Error("reauth_required");
  err.status = 401;
  return err;
}

function settingsResponse(overrides = {}) {
  return {
    settings: {
      webhook_allowed_hosts: [],
      allow_insecure_webhooks: false,
      dispatcher_paused: false,
      min_lease_ttl: 5,
      max_lease_ttl: 3600,
      default_lease_ttl: 300,
      max_webhooks_per_user: 20,
      ...overrides,
    },
    updated_at: null,
    updated_by: null,
  };
}

function renderPage() {
  return render(
    <MemoryRouter>
      <SecuritySettings />
    </MemoryRouter>
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  authState.user = { username: "admin", role: "admin" };
});

describe("SecuritySettings", () => {
  it("renders the settings form on a fresh session", async () => {
    api.getSecuritySettings.mockResolvedValue(settingsResponse());
    renderPage();
    expect(await screen.findByText("Webhooks")).toBeInTheDocument();
    expect(screen.getByText("Ticket lease TTL policy window")).toBeInTheDocument();
  });

  it("round-trips a saved value", async () => {
    api.getSecuritySettings.mockResolvedValue(settingsResponse());
    api.updateSecuritySettings.mockResolvedValue(
      settingsResponse({ max_webhooks_per_user: 5, updated_by: 1 })
    );
    renderPage();

    const input = await screen.findByLabelText("Max webhooks / user");
    fireEvent.change(input, { target: { value: "5" } });
    fireEvent.click(screen.getByRole("button", { name: /save/i }));

    await waitFor(() => expect(api.updateSecuritySettings).toHaveBeenCalled());
    const payload = api.updateSecuritySettings.mock.calls[0][0];
    expect(payload.max_webhooks_per_user).toBe(5);
    expect(await screen.findByText("Saved")).toBeInTheDocument();
  });

  it("shows an inline re-login prompt instead of the form on reauth_required", async () => {
    api.getSecuritySettings.mockRejectedValue(reauthError());
    renderPage();

    expect(await screen.findByText(/Confirm it.s you/)).toBeInTheDocument();
    expect(screen.queryByText("Webhooks")).not.toBeInTheDocument();
  });

  it("returns to the form after a successful re-login", async () => {
    api.getSecuritySettings
      .mockRejectedValueOnce(reauthError())
      .mockResolvedValueOnce(settingsResponse());
    loginSpy.mockResolvedValue({ username: "admin", role: "admin" });
    renderPage();

    await screen.findByText(/Confirm it.s you/);
    const passwordInput = document.querySelector('input[type="password"]');
    fireEvent.change(passwordInput, { target: { value: "admin" } });
    fireEvent.click(screen.getByRole("button", { name: /confirm/i }));

    await waitFor(() => expect(loginSpy).toHaveBeenCalledWith("admin", "admin"));
    expect(await screen.findByText("Webhooks")).toBeInTheDocument();
  });

  it("shows a generic error for a non-reauth failure", async () => {
    api.getSecuritySettings.mockRejectedValue(new Error("boom"));
    renderPage();
    expect(await screen.findByText("boom")).toBeInTheDocument();
    expect(screen.queryByText(/Confirm it.s you/)).not.toBeInTheDocument();
  });
});
