import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import ResolverSettings from "./ResolverSettings";
import { api } from "../api";

vi.mock("../api", () => ({
  api: {
    listResolvers: vi.fn(),
    listAgents: vi.fn(),
    getResolverSettings: vi.fn(),
    updateResolverSettings: vi.fn(),
  },
}));

function settings() {
  return { bot_user_id: null, settings: {}, secrets: [], updated_at: null, updated_by: null };
}

function agent(overrides = {}) {
  return {
    user_id: 9,
    username: "triage",
    display_name: "Triage worker",
    is_resolver_bot: false,
    name: "triage",
    label: "prod-us-east",
    agent: "custom",
    model: "gpt-x",
    last_seen_at: new Date().toISOString(),
    ...overrides,
  };
}

function renderPage() {
  return render(
    <MemoryRouter>
      <ResolverSettings />
    </MemoryRouter>
  );
}

describe("ResolverSettings — external agents panel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.listResolvers.mockResolvedValue([]);
    api.listAgents.mockResolvedValue([]);
    api.getResolverSettings.mockResolvedValue(settings());
  });

  it("renders an empty state when no agent has checked in", async () => {
    renderPage();
    expect(await screen.findByText("External agents")).toBeInTheDocument();
    expect(screen.getByText("No external agent has checked in yet.")).toBeInTheDocument();
  });

  it("lists a registered agent with its runtime and freshness", async () => {
    api.listAgents.mockResolvedValue([agent()]);
    renderPage();
    expect(await screen.findByText("triage")).toBeInTheDocument();
    expect(screen.getByText("prod-us-east")).toBeInTheDocument();
    expect(screen.getByText(/custom.*gpt-x/)).toBeInTheDocument();
  });

  it("omits resolver bots, which the roster above already lists as edit scopes", async () => {
    api.listAgents.mockResolvedValue([agent({ is_resolver_bot: true, name: "claude-bot" })]);
    renderPage();
    expect(await screen.findByText("No external agent has checked in yet.")).toBeInTheDocument();
    expect(screen.queryByText("claude-bot")).not.toBeInTheDocument();
  });

  it("still renders the settings form when the agent registry is unreachable", async () => {
    api.listAgents.mockRejectedValue(new Error("boom"));
    renderPage();
    // The failure is swallowed on purpose: liveness info is not load-bearing.
    await waitFor(() => expect(screen.getByText("Models")).toBeInTheDocument());
    expect(screen.queryByText("boom")).not.toBeInTheDocument();
  });
});
