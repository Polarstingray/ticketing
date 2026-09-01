import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import TicketDetail from "./TicketDetail";
import { api } from "../api";

// useAuth is mocked; `authState.user` is swapped per test to exercise the
// permission gating (admin / creator / assignee / unrelated member).
const authState = vi.hoisted(() => ({ user: null }));
vi.mock("../auth/AuthContext", () => ({
  useAuth: () => ({ user: authState.user }),
}));

vi.mock("../api", () => ({
  api: {
    listUsers: vi.fn(),
    getTicket: vi.fn(),
    listComments: vi.fn(),
    listActivity: vi.fn(),
    listAgentRuns: vi.fn(),
    costRollup: vi.fn(),
    updateTicket: vi.fn(),
    addComment: vi.fn(),
    markTicketNotificationsRead: vi.fn(),
  },
}));

// Opening a ticket clears its unread notifications; the refresh spy lets the
// tests assert the badge/dot state is re-pulled afterwards.
const notifState = vi.hoisted(() => ({ refresh: vi.fn() }));
vi.mock("../notifications/NotificationsContext", () => ({
  useNotifications: () => ({ unreadCount: 0, unreadTicketIds: new Set(), refresh: notifState.refresh }),
}));

const CREATOR = { id: 10 };
const ASSIGNEE = { id: 20 };

function makeTicket(overrides = {}) {
  return {
    id: 42,
    type: "task",
    title: "Do the thing",
    description: "details",
    status: "open",
    priority: "medium",
    archived: false,
    created_by: CREATOR.id,
    assigned_to: ASSIGNEE.id,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    due_date: null,
    code_blocks: [],
    tags: [],
    ...overrides,
  };
}

function renderDetail() {
  return render(
    <MemoryRouter initialEntries={["/tickets/42"]}>
      <Routes>
        <Route path="/tickets/:id" element={<TicketDetail />} />
      </Routes>
    </MemoryRouter>
  );
}

// The page finished loading once the title heading is present.
async function waitForLoaded() {
  await screen.findByRole("heading", { name: /Do the thing/i });
}

beforeEach(() => {
  authState.user = null;
  api.listUsers.mockResolvedValue([]);
  api.getTicket.mockResolvedValue(makeTicket());
  api.listComments.mockResolvedValue({ items: [], total: 0 });
  api.listActivity.mockResolvedValue([]);
  api.listAgentRuns.mockResolvedValue([]);
  api.costRollup.mockResolvedValue(null);
  api.markTicketNotificationsRead.mockResolvedValue({ unread_count: 0 });
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("TicketDetail permissions", () => {
  it("lets an admin edit and delete", async () => {
    authState.user = { id: 999, role: "admin" };
    renderDetail();
    await waitForLoaded();

    expect(screen.getByPlaceholderText("Add tag…")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Delete ticket/i })).toBeInTheDocument();
    expect(
      screen.queryByText(/You can comment but not edit this ticket/i)
    ).not.toBeInTheDocument();
  });

  it("lets the creator edit but not delete", async () => {
    authState.user = { id: CREATOR.id, role: "member" };
    renderDetail();
    await waitForLoaded();

    expect(screen.getByPlaceholderText("Add tag…")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Delete ticket/i })).not.toBeInTheDocument();
  });

  it("lets the assignee edit", async () => {
    authState.user = { id: ASSIGNEE.id, role: "member" };
    renderDetail();
    await waitForLoaded();

    expect(screen.getByPlaceholderText("Add tag…")).toBeInTheDocument();
  });

  it("blocks an unrelated member from editing or deleting", async () => {
    authState.user = { id: 777, role: "member" };
    renderDetail();
    await waitForLoaded();

    expect(screen.getByText(/You can comment but not edit this ticket/i)).toBeInTheDocument();
    expect(screen.queryByPlaceholderText("Add tag…")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Delete ticket/i })).not.toBeInTheDocument();
    // The status control is present but disabled for read-only viewers.
    expect(screen.getByDisplayValue("Open")).toBeDisabled();
  });
});

describe("TicketDetail reserved tags", () => {
  it("shows reserved control tags read-only even for an admin", async () => {
    authState.user = { id: 999, role: "admin" };
    api.getTicket.mockResolvedValue(makeTicket({ tags: ["backend", "dangerous", "repo:ticketing"] }));
    renderDetail();
    await waitForLoaded();

    // Free tag is removable (has a remove button); reserved tags are not.
    expect(screen.getByRole("button", { name: /Remove tag backend/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Remove tag dangerous/i })).not.toBeInTheDocument();
    expect(screen.getByText("dangerous")).toBeInTheDocument();
    expect(screen.getByText("repo:ticketing")).toBeInTheDocument();
  });
});

describe("TicketDetail agent runs", () => {
  it("shows the empty state when there are no runs", async () => {
    authState.user = { id: 999, role: "admin" };
    renderDetail();
    await waitForLoaded();
    expect(screen.getByText(/No agent runs yet/i)).toBeInTheDocument();
  });

  it("renders a cost badge summing run cost", async () => {
    authState.user = { id: 999, role: "admin" };
    api.listAgentRuns.mockResolvedValue([
      { id: 1, phase: "plan", agent: "claude", model: "opus", status: "succeeded", cost_usd: 0.02, input_tokens: 100, output_tokens: 50, cache_read_tokens: 0, cache_write_tokens: 0, finished_at: "2026-01-01T00:00:00Z" },
      { id: 2, phase: "implement", agent: "claude", model: "opus", status: "succeeded", cost_usd: 0.03, input_tokens: 200, output_tokens: 80, cache_read_tokens: 0, cache_write_tokens: 0, finished_at: "2026-01-01T00:00:00Z" },
    ]);
    renderDetail();
    await waitForLoaded();

    await waitFor(() =>
      expect(screen.getByText(/🤖 \$0\.0500/)).toBeInTheDocument()
    );
  });
});

describe("TicketDetail resolver fix loop", () => {
  const REVIEW = {
    id: 1,
    author: 2, // the resolver bot that posted the findings
    body: "🔎 **Code review** (Stingray resolver)\n\nblocker: unchecked exit code",
    created_at: "2026-01-01T00:00:00Z",
  };
  const reviewed = (tags = ["repo:ticketing", "resolver:awaiting-fix"]) =>
    makeTicket({ type: "code_review", tags, status: "in_review" });

  it("offers Apply fixes on a reviewed ticket and hands it back to the bot", async () => {
    authState.user = { id: CREATOR.id, role: "member" };
    api.getTicket.mockResolvedValue(reviewed());
    api.listComments.mockResolvedValue({ items: [REVIEW], total: 1 });
    api.addComment.mockResolvedValue({ id: 2, author: CREATOR.id, body: "/fix" });
    api.updateTicket.mockResolvedValue(reviewed());
    renderDetail();
    await waitForLoaded();

    const button = await screen.findByRole("button", { name: /Apply fixes/i });
    expect(button).toBeEnabled();
    fireEvent.click(button);

    await waitFor(() => expect(api.addComment).toHaveBeenCalledWith("42", "/fix"));
    // Re-assigned to whoever authored the review, so it re-enters that bot's queue.
    expect(api.updateTicket).toHaveBeenCalledWith("42", { assigned_to: REVIEW.author });
  });

  it("disables Apply fixes when the ticket names no repo", async () => {
    authState.user = { id: CREATOR.id, role: "member" };
    api.getTicket.mockResolvedValue(reviewed(["resolver:awaiting-fix"]));
    api.listComments.mockResolvedValue({ items: [REVIEW], total: 1 });
    renderDetail();
    await waitForLoaded();

    expect(await screen.findByRole("button", { name: /Apply fixes/i })).toBeDisabled();
  });

  it("hides Apply fixes when the ticket is not awaiting a fix", async () => {
    authState.user = { id: CREATOR.id, role: "member" };
    api.getTicket.mockResolvedValue(reviewed(["repo:ticketing"]));
    api.listComments.mockResolvedValue({ items: [REVIEW], total: 1 });
    renderDetail();
    await waitForLoaded();

    expect(screen.queryByRole("button", { name: /Apply fixes/i })).not.toBeInTheDocument();
  });

  it("hides Apply fixes from a member who cannot modify the ticket", async () => {
    authState.user = { id: 777, role: "member" };
    api.getTicket.mockResolvedValue(reviewed());
    api.listComments.mockResolvedValue({ items: [REVIEW], total: 1 });
    renderDetail();
    await waitForLoaded();

    expect(screen.queryByRole("button", { name: /Apply fixes/i })).not.toBeInTheDocument();
  });
});

describe("TicketDetail markdown rendering", () => {
  // A realistic resolver review comment: bold marker, a severity list citing
  // file:line, and a fenced diff.
  const RESOLVER_COMMENT = {
    id: 7,
    author: 2,
    created_at: "2026-01-02T00:00:00Z",
    body: [
      "🔎 **Code review** (Stingray resolver)",
      "",
      "## blocker",
      "",
      "- `backend/auth.py:98` — session is never cleared.",
      "",
      "```diff",
      "-    session.pop(key)",
      "+    session.clear()",
      "```",
    ].join("\n"),
  };

  it("renders a resolver comment as markdown, not literal text", async () => {
    authState.user = { id: CREATOR.id, role: "member" };
    api.listComments.mockResolvedValue({ items: [RESOLVER_COMMENT], total: 1 });
    const { container } = renderDetail();
    await waitForLoaded();

    expect(screen.getByRole("heading", { name: "blocker" })).toBeInTheDocument();
    expect(screen.getByText("Code review").tagName).toBe("STRONG");
    expect(screen.getByRole("listitem").textContent).toContain("backend/auth.py:98");
    const pre = container.querySelector("pre");
    expect(pre.textContent).toContain("session.clear()");
    // The raw marker text must not leak through as literal markdown.
    expect(screen.queryByText(/\*\*Code review\*\*/)).not.toBeInTheDocument();
  });

  it("renders the ticket description as markdown", async () => {
    authState.user = { id: CREATOR.id, role: "member" };
    api.getTicket.mockResolvedValue(makeTicket({ description: "see **this** part" }));
    renderDetail();
    await waitForLoaded();

    expect(screen.getByText("this").tagName).toBe("STRONG");
  });

  it("previews the comment draft as markdown", async () => {
    authState.user = { id: CREATOR.id, role: "member" };
    renderDetail();
    await waitForLoaded();

    fireEvent.change(screen.getByPlaceholderText("Add a comment…"), {
      target: { value: "a **bold** draft" },
    });
    fireEvent.click(screen.getByRole("tab", { name: "Preview" }));

    expect(screen.getByText("bold").tagName).toBe("STRONG");
    expect(screen.queryByPlaceholderText("Add a comment…")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: "Write" }));
    expect(screen.getByPlaceholderText("Add a comment…")).toHaveValue("a **bold** draft");
  });
});

describe("TicketDetail unread notifications", () => {
  it("marks the ticket's notifications read on load and refreshes the badge", async () => {
    authState.user = { id: CREATOR.id, role: "member" };
    renderDetail();
    await waitForLoaded();

    await waitFor(() => expect(api.markTicketNotificationsRead).toHaveBeenCalledWith("42"));
    await waitFor(() => expect(notifState.refresh).toHaveBeenCalled());
  });

  it("still renders the page when the mark-read call fails", async () => {
    authState.user = { id: CREATOR.id, role: "member" };
    api.markTicketNotificationsRead.mockRejectedValue(new Error("boom"));
    renderDetail();
    await waitForLoaded();

    expect(screen.queryByText(/boom/i)).not.toBeInTheDocument();
    expect(notifState.refresh).not.toHaveBeenCalled();
  });
});
