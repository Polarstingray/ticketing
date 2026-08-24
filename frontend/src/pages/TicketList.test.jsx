import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import TicketList from "./TicketList";
import { api } from "../api";

vi.mock("../api", () => ({
  api: {
    listTickets: vi.fn(),
    listTicketTags: vi.fn(),
    listUsers: vi.fn(),
    listSavedViews: vi.fn(),
    createSavedView: vi.fn(),
    deleteSavedView: vi.fn(),
    updateTicket: vi.fn(),
    archiveTicket: vi.fn(),
  },
}));

// The unread-comment dots come from the notifications provider; the tests drive
// that set directly rather than standing up polling + auth.
const notifications = vi.hoisted(() => ({ unreadTicketIds: new Set() }));

vi.mock("../notifications/NotificationsContext", () => ({
  useNotifications: () => ({
    unreadCount: notifications.unreadTicketIds.size,
    unreadTicketIds: notifications.unreadTicketIds,
    refresh: vi.fn(),
  }),
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

// Exposes the live URL so tests can assert that filter state round-trips
// through the query string rather than living in component state.
let currentLocation;
function LocationProbe() {
  currentLocation = useLocation();
  return null;
}

function renderList(initialEntry = "/tickets") {
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <Routes>
        <Route
          path="/tickets"
          element={
            <>
              <TicketList />
              <LocationProbe />
            </>
          }
        />
      </Routes>
    </MemoryRouter>
  );
}

// The most recent object passed to listTickets (its filter/pagination args).
function lastListArgs() {
  return api.listTickets.mock.calls.at(-1)[0];
}

function search() {
  return new URLSearchParams(currentLocation.search);
}

beforeEach(() => {
  api.listUsers.mockResolvedValue([]);
  api.listTickets.mockResolvedValue({ items: [ticket()], total: 1 });
  api.listTicketTags.mockResolvedValue({ items: [] });
  api.listSavedViews.mockResolvedValue([]);
  api.updateTicket.mockImplementation((id, body) => Promise.resolve(ticket({ id, ...body })));
  api.archiveTicket.mockResolvedValue(null);
  notifications.unreadTicketIds = new Set();
  localStorage.clear();
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

  it("shows the no-match empty state when a filter is active", async () => {
    api.listTickets.mockResolvedValue({ items: [], total: 0 });
    renderList("/tickets?status=resolved");
    expect(await screen.findByText(/No tickets match these filters/i)).toBeInTheDocument();
  });

  it("refetches with the selected status filter", async () => {
    renderList();
    await screen.findByText("First ticket");

    fireEvent.change(screen.getByLabelText("Status"), { target: { value: "resolved" } });

    await waitFor(() => expect(lastListArgs()).toMatchObject({ status: "resolved" }));
  });

  it("debounces the search box into a single q-filtered refetch", async () => {
    renderList();
    await screen.findByText("First ticket");
    const before = api.listTickets.mock.calls.length;

    fireEvent.change(screen.getByLabelText("Search"), { target: { value: "bug" } });

    // Debounced (~300ms): the refetch is deferred, then fires once with q.
    await waitFor(() => expect(lastListArgs()).toMatchObject({ q: "bug" }));
    // One extra fetch, not one-per-keystroke.
    expect(api.listTickets.mock.calls.length).toBe(before + 1);
  });

  it("appends the next page on Load more", async () => {
    api.listTickets.mockResolvedValueOnce({
      items: [ticket({ id: 1, title: "First ticket" })],
      total: 2,
    });
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

  it("clears every filter but keeps the sort", async () => {
    renderList("/tickets?status=resolved&tag=bug&sort=priority");
    await screen.findByText("First ticket");

    fireEvent.click(screen.getAllByRole("button", { name: /Clear all/i })[0]);

    await waitFor(() => expect(lastListArgs()).toMatchObject({ status: "", sort: "priority" }));
    expect(search().getAll("tag")).toEqual([]);
    expect(search().get("sort")).toBe("priority");
  });
});

describe("TicketList URL state", () => {
  it("reads filters, tags and sort out of the query string on first render", async () => {
    renderList("/tickets?status=open&tag=bug&tag=ui&tag_match=any&sort=priority&order=asc");

    await waitFor(() =>
      expect(lastListArgs()).toMatchObject({
        status: "open",
        tag: ["bug", "ui"],
        tag_match: "any",
        sort: "priority",
        order: "asc",
      })
    );
  });

  it("writes filter changes back into the URL", async () => {
    renderList();
    await screen.findByText("First ticket");

    fireEvent.change(screen.getByLabelText("Priority"), { target: { value: "high" } });

    await waitFor(() => expect(search().get("priority")).toBe("high"));
  });

  it("keeps defaults out of the URL", async () => {
    renderList();
    await screen.findByText("First ticket");

    fireEvent.change(screen.getByLabelText("Status"), { target: { value: "open" } });
    await waitFor(() => expect(search().get("status")).toBe("open"));

    // Back to "All statuses" — the param goes away rather than becoming empty.
    fireEvent.change(screen.getByLabelText("Status"), { target: { value: "" } });
    await waitFor(() => expect(search().has("status")).toBe(false));
  });

  it("ignores an unknown sort in the URL instead of passing it to the API", async () => {
    renderList("/tickets?sort=bogus");
    // The backend would 422 on this; fall back to the default.
    await waitFor(() => expect(lastListArgs()).toMatchObject({ sort: "created" }));
  });

  it("drops a blank tag param so it cannot filter everything out", async () => {
    renderList("/tickets?tag=");
    await waitFor(() => expect(lastListArgs()).toMatchObject({ tag: [] }));
  });
});

describe("TicketList tag filtering", () => {
  beforeEach(() => {
    api.listTicketTags.mockResolvedValue({
      items: [
        { tag: "bug", count: 4 },
        { tag: "ui", count: 2 },
        { tag: "repo:ticketing", count: 9 },
      ],
    });
  });

  it("lists free tags with their counts and hides workflow tags behind a group", async () => {
    renderList();
    expect(await screen.findByLabelText("bug")).toBeInTheDocument();
    expect(screen.getByLabelText("ui")).toBeInTheDocument();

    // Reserved tags are collapsed by default so they don't drown the free ones.
    expect(screen.queryByLabelText("repo:ticketing")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Workflow tags/i }));
    expect(screen.getByLabelText("repo:ticketing")).toBeInTheDocument();
  });

  it("sends selected tags as an array and defaults to matching all of them", async () => {
    renderList();
    fireEvent.click(await screen.findByLabelText("bug"));
    await waitFor(() => expect(lastListArgs()).toMatchObject({ tag: ["bug"] }));

    fireEvent.click(screen.getByLabelText("ui"));
    await waitFor(() =>
      expect(lastListArgs()).toMatchObject({ tag: ["bug", "ui"], tag_match: "all" })
    );
  });

  it("switches to matching any tag", async () => {
    renderList("/tickets?tag=bug&tag=ui");
    await screen.findByText("First ticket");

    fireEvent.click(screen.getByRole("button", { name: "Any" }));

    await waitFor(() => expect(lastListArgs()).toMatchObject({ tag_match: "any" }));
  });

  it("does not send tag_match with fewer than two tags selected", async () => {
    renderList("/tickets?tag=bug&tag_match=any");
    await waitFor(() => expect(lastListArgs()).toMatchObject({ tag: ["bug"] }));
    expect(lastListArgs().tag_match).toBeUndefined();
  });

  it("deselects a tag when its chip is dismissed", async () => {
    renderList("/tickets?tag=bug&tag=ui");
    await screen.findByText("First ticket");

    fireEvent.click(screen.getByRole("button", { name: "Remove filter bug" }));

    await waitFor(() => expect(lastListArgs()).toMatchObject({ tag: ["ui"] }));
  });

  it("refetches the tag facets when the archived scope changes", async () => {
    renderList();
    await screen.findByText("First ticket");

    fireEvent.click(screen.getByLabelText(/Show archived/i));

    await waitFor(() =>
      expect(api.listTicketTags).toHaveBeenLastCalledWith({ archived: "true" })
    );
  });
});

describe("TicketList sorting and density", () => {
  it("puts the chosen sort and direction in the request", async () => {
    renderList();
    await screen.findByText("First ticket");

    fireEvent.change(screen.getByLabelText("Sort by"), { target: { value: "priority" } });
    await waitFor(() => expect(lastListArgs()).toMatchObject({ sort: "priority" }));

    fireEvent.click(screen.getByRole("button", { name: /switch to ascending/i }));
    await waitFor(() => expect(lastListArgs()).toMatchObject({ order: "asc" }));
  });

  it("remembers the density choice across mounts but keeps it out of the URL", async () => {
    const { unmount } = renderList();
    await screen.findByText("First ticket");

    fireEvent.click(screen.getByRole("button", { name: "Compact" }));
    expect(screen.getByRole("button", { name: "Compact" })).toHaveAttribute(
      "aria-pressed",
      "true"
    );
    expect(currentLocation.search).toBe("");
    unmount();

    renderList();
    await screen.findByText("First ticket");
    expect(screen.getByRole("button", { name: "Compact" })).toHaveAttribute(
      "aria-pressed",
      "true"
    );
  });
});

describe("TicketList saved views", () => {
  it("applies a saved view's query string to the URL", async () => {
    api.listSavedViews.mockResolvedValue([
      { id: 1, name: "My open bugs", query: "status=open&tag=bug" },
    ]);
    renderList();

    fireEvent.click(await screen.findByRole("button", { name: "My open bugs" }));

    await waitFor(() => expect(lastListArgs()).toMatchObject({ status: "open", tag: ["bug"] }));
  });

  it("saves the current query under a name", async () => {
    api.createSavedView.mockResolvedValue({ id: 2, name: "Urgent", query: "priority=critical" });
    renderList("/tickets?priority=critical");
    await screen.findByText("First ticket");

    fireEvent.click(screen.getByRole("button", { name: /Save current view/i }));
    fireEvent.change(screen.getByLabelText(/Name for this view/i), {
      target: { value: "Urgent" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^Save$/i }));

    await waitFor(() =>
      expect(api.createSavedView).toHaveBeenCalledWith({
        name: "Urgent",
        query: "priority=critical",
      })
    );
    expect(await screen.findByRole("button", { name: "Urgent" })).toBeInTheDocument();
  });

  it("offers nothing to save when no filters are active", async () => {
    renderList();
    await screen.findByText("First ticket");
    expect(screen.queryByRole("button", { name: /Save current view/i })).not.toBeInTheDocument();
  });

  it("surfaces a duplicate-name conflict instead of silently failing", async () => {
    api.createSavedView.mockRejectedValue(new Error("A saved view named 'Urgent' already exists"));
    renderList("/tickets?priority=critical");
    await screen.findByText("First ticket");

    fireEvent.click(screen.getByRole("button", { name: /Save current view/i }));
    fireEvent.change(screen.getByLabelText(/Name for this view/i), {
      target: { value: "Urgent" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^Save$/i }));

    expect(await screen.findByText(/already exists/i)).toBeInTheDocument();
  });
});

describe("TicketList row edits", () => {
  function statusTrigger(name = /Change status/i) {
    return screen.getByRole("button", { name });
  }

  // Scoped to the open menu: the filter panel's <select>s are full of <option>s
  // too, so an unscoped role="option" query matches those as well.
  function menuOption(name) {
    return within(screen.getByRole("listbox")).getByRole("option", { name });
  }

  it("changes a status from the row and merges the response without refetching", async () => {
    renderList();
    await screen.findByText("First ticket");
    const listCalls = api.listTickets.mock.calls.length;

    fireEvent.click(statusTrigger());
    fireEvent.click(menuOption("Resolved"));

    await waitFor(() => expect(api.updateTicket).toHaveBeenCalledWith(1, { status: "resolved" }));
    // The row re-badges itself off the response; the list is not re-queried.
    expect(await screen.findByRole("button", { name: /Status: Resolved/i })).toBeInTheDocument();
    expect(api.listTickets.mock.calls.length).toBe(listCalls);
  });

  it("shows a failed status change in the error banner and leaves the badge alone", async () => {
    api.updateTicket.mockRejectedValue(new Error("Status change rejected"));
    renderList();
    await screen.findByText("First ticket");

    fireEvent.click(statusTrigger());
    fireEvent.click(menuOption("Closed"));

    expect(await screen.findByText("Status change rejected")).toBeInTheDocument();
    expect(statusTrigger(/Status: Open/i)).toBeInTheDocument();
  });

  it("offers Archive only on a closed ticket that isn't archived yet", async () => {
    api.listTickets.mockResolvedValue({ items: [ticket({ status: "open" })], total: 1 });
    const { unmount } = renderList();
    await screen.findByText("First ticket");
    expect(screen.queryByRole("button", { name: "Archive" })).not.toBeInTheDocument();
    unmount();

    api.listTickets.mockResolvedValue({
      items: [ticket({ status: "closed", archived: true })],
      total: 1,
    });
    renderList();
    await screen.findByText("First ticket");
    expect(screen.queryByRole("button", { name: "Archive" })).not.toBeInTheDocument();
  });

  it("drops an archived row and keeps the total honest when archived is out of scope", async () => {
    api.listTickets.mockResolvedValue({
      items: [ticket({ status: "closed" }), ticket({ id: 2, title: "Second ticket" })],
      total: 2,
    });
    renderList();
    await screen.findByText("First ticket");

    fireEvent.click(screen.getByRole("button", { name: "Archive" }));

    await waitFor(() => expect(api.archiveTicket).toHaveBeenCalledWith(1));
    await waitFor(() => expect(screen.queryByText("First ticket")).not.toBeInTheDocument());
    expect(screen.getByText("(1)")).toBeInTheDocument();
    expect(screen.getByText("Second ticket")).toBeInTheDocument();
  });

  it("keeps the row and re-badges it when archived tickets are in scope", async () => {
    api.listTickets.mockResolvedValue({ items: [ticket({ status: "closed" })], total: 1 });
    renderList("/tickets?archived=true");
    await screen.findByText("First ticket");

    fireEvent.click(screen.getByRole("button", { name: "Archive" }));

    await waitFor(() => expect(api.archiveTicket).toHaveBeenCalledWith(1));
    const row = screen.getByText("First ticket").closest("a");
    expect(within(row).getByText("Archived")).toBeInTheDocument();
    expect(within(row).queryByRole("button", { name: "Archive" })).not.toBeInTheDocument();
    expect(screen.getByText("(1)")).toBeInTheDocument();
  });

  it("surfaces a failed archive instead of dropping the row", async () => {
    api.archiveTicket.mockRejectedValue(new Error("Archive failed"));
    api.listTickets.mockResolvedValue({ items: [ticket({ status: "closed" })], total: 1 });
    renderList();
    await screen.findByText("First ticket");

    fireEvent.click(screen.getByRole("button", { name: "Archive" }));

    expect(await screen.findByText("Archive failed")).toBeInTheDocument();
    expect(screen.getByText("First ticket")).toBeInTheDocument();
  });

  it("disables the row's controls while its request is in flight", async () => {
    let resolveUpdate;
    api.updateTicket.mockReturnValue(new Promise((r) => { resolveUpdate = r; }));
    api.listTickets.mockResolvedValue({ items: [ticket({ status: "closed" })], total: 1 });
    renderList();
    await screen.findByText("First ticket");

    fireEvent.click(statusTrigger());
    fireEvent.click(menuOption("Resolved"));

    await waitFor(() => expect(screen.getByRole("button", { name: "Archive" })).toBeDisabled());
    expect(statusTrigger()).toBeDisabled();

    resolveUpdate(ticket({ status: "resolved" }));
    await waitFor(() => expect(screen.getByRole("button", { name: /Change status/i })).toBeEnabled());
  });
});

describe("TicketList unread-comment dot", () => {
  it("marks only the rows with an unread notification", async () => {
    notifications.unreadTicketIds = new Set([2]);
    api.listTickets.mockResolvedValue({
      items: [ticket({ id: 1 }), ticket({ id: 2, title: "Second ticket" })],
      total: 2,
    });
    renderList();

    const unread = (await screen.findByText("Second ticket")).closest("a");
    const read = screen.getByText("First ticket").closest("a");
    expect(within(unread).getByLabelText("Unread comment")).toBeInTheDocument();
    expect(within(read).queryByLabelText("Unread comment")).toBeNull();
  });

  it("shows no dots when nothing is unread", async () => {
    renderList();
    await screen.findByText("First ticket");
    expect(screen.queryByLabelText("Unread comment")).toBeNull();
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
