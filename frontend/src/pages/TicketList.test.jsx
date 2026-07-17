import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import TicketList from "./TicketList";
import { api } from "../api";

vi.mock("../api", () => ({
  api: {
    listTickets: vi.fn(),
    listUsers: vi.fn(),
  },
}));

function ticket(overrides = {}) {
  return {
    id: 1,
    title: "First ticket",
    type: "task",
    status: "open",
    priority: "medium",
    assigned_to: null,
    tags: [],
    archived: false,
    due_date: null,
    updated_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

function renderList() {
  return render(
    <MemoryRouter>
      <TicketList />
    </MemoryRouter>
  );
}

// The most recent object passed to listTickets (its filter/pagination args).
function lastListArgs() {
  return api.listTickets.mock.calls.at(-1)[0];
}

beforeEach(() => {
  api.listUsers.mockResolvedValue([]);
  api.listTickets.mockResolvedValue({ items: [ticket()], total: 1 });
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("TicketList", () => {
  it("renders tickets and the total count", async () => {
    renderList();
    expect(await screen.findByText("First ticket")).toBeInTheDocument();
    expect(screen.getByText("(1)")).toBeInTheDocument();
  });

  it("shows the first-run empty state when there are no tickets and no filters", async () => {
    api.listTickets.mockResolvedValue({ items: [], total: 0 });
    renderList();
    expect(await screen.findByText(/No tickets yet/i)).toBeInTheDocument();
    expect(screen.getByText(/Create your first ticket/i)).toBeInTheDocument();
  });

  it("refetches with the selected status filter", async () => {
    renderList();
    await screen.findByText("First ticket");

    fireEvent.change(screen.getByDisplayValue("All statuses"), { target: { value: "resolved" } });

    await waitFor(() => expect(lastListArgs()).toMatchObject({ status: "resolved" }));
  });

  it("debounces the search box into a single q-filtered refetch", async () => {
    renderList();
    await screen.findByText("First ticket");
    const before = api.listTickets.mock.calls.length;

    fireEvent.change(screen.getByPlaceholderText(/Search title or description/i), {
      target: { value: "bug" },
    });

    // Debounced (~300ms): the refetch is deferred, then fires once with q.
    await waitFor(() => expect(lastListArgs()).toMatchObject({ q: "bug" }));
    // One extra fetch, not one-per-keystroke.
    expect(api.listTickets.mock.calls.length).toBe(before + 1);
  });

  it("appends the next page on Load more", async () => {
    api.listTickets.mockResolvedValueOnce({ items: [ticket({ id: 1, title: "First ticket" })], total: 2 });
    renderList();
    await screen.findByText("First ticket");

    api.listTickets.mockResolvedValueOnce({
      items: [ticket({ id: 2, title: "Second ticket" })],
      total: 2,
    });
    fireEvent.click(screen.getByRole("button", { name: /Load more/i }));

    expect(await screen.findByText("Second ticket")).toBeInTheDocument();
    // Second page requested with an offset past the first page.
    expect(lastListArgs()).toMatchObject({ offset: 1 });
  });

  it("clears all filters", async () => {
    renderList();
    await screen.findByText("First ticket");

    fireEvent.change(screen.getByDisplayValue("All statuses"), { target: { value: "resolved" } });
    await waitFor(() => expect(lastListArgs()).toMatchObject({ status: "resolved" }));

    fireEvent.click(screen.getByRole("button", { name: /^Clear$/i }));
    await waitFor(() => expect(lastListArgs()).toMatchObject({ status: "" }));
  });
});

describe("TicketList assignee labels", () => {
  it("labels an assigned ticket with the user's display name", async () => {
    api.listUsers.mockResolvedValue([{ id: 5, display_name: "Ada Lovelace" }]);
    api.listTickets.mockResolvedValue({
      items: [ticket({ assigned_to: 5 })],
      total: 1,
    });
    renderList();

    const row = (await screen.findByText("First ticket")).closest("a");
    expect(within(row).getByText("Ada Lovelace")).toBeInTheDocument();
  });
});
