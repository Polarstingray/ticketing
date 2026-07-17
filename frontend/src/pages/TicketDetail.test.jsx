import { render, screen, waitFor } from "@testing-library/react";
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
  },
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
  api.listComments.mockResolvedValue([]);
  api.listActivity.mockResolvedValue([]);
  api.listAgentRuns.mockResolvedValue([]);
  api.costRollup.mockResolvedValue(null);
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
