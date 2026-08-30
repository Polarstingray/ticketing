import { render, screen, fireEvent } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import Layout from "./Layout";

vi.mock("../auth/AuthContext", () => ({
  useAuth: () => ({
    user: { display_name: "Admin", role: "admin" },
    logout: vi.fn().mockResolvedValue(undefined),
  }),
}));

vi.mock("../notifications/NotificationsContext", () => ({
  useNotifications: () => ({ unreadCount: 0 }),
}));

// ChatWidget talks to the API; stub it out so Layout tests stay unit-level.
vi.mock("./ChatWidget", () => ({ default: () => null }));

function renderLayout() {
  return render(
    <MemoryRouter initialEntries={["/tickets"]}>
      <Layout />
    </MemoryRouter>
  );
}

describe("Layout hamburger menu", () => {
  it("hamburger button has aria-expanded=false initially", () => {
    renderLayout();
    const btn = screen.getByRole("button", { name: /menu/i });
    expect(btn).toHaveAttribute("aria-expanded", "false");
  });

  it("clicking hamburger toggles aria-expanded to true", () => {
    renderLayout();
    const btn = screen.getByRole("button", { name: /menu/i });
    fireEvent.click(btn);
    expect(btn).toHaveAttribute("aria-expanded", "true");
  });

  it("clicking hamburger twice closes the drawer", () => {
    renderLayout();
    const btn = screen.getByRole("button", { name: /menu/i });
    fireEvent.click(btn);
    fireEvent.click(btn);
    expect(btn).toHaveAttribute("aria-expanded", "false");
  });
});
