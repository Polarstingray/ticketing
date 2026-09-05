import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import ResolverSettings, { byStation, freshness, staleAfter } from "./ResolverSettings";
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

describe("freshness is sized from the reported cadence", () => {
  const ago = (minutes) => new Date(Date.now() - minutes * 60_000).toISOString();

  it("keeps a sweep-only resolver live across a 30-minute timer", () => {
    // The regression: the window was a hardcoded 25 minutes, tuned to a
    // ~10-minute sweep. Moving timers to 30 minutes made every healthy
    // resolver display as stale, because a sweep-only worker is silent for
    // the whole interval by design.
    expect(freshness(ago(30), 0).cls).toBe("live");
  });

  it("calls a listener stale after it misses a few of its own beats", () => {
    expect(freshness(ago(5), 300).cls).toBe("live");    // one beat late
    expect(freshness(ago(20), 300).cls).toBe("stale");  // four beats missed
  });

  it("derives the window from the cadence, not a constant", () => {
    expect(staleAfter(300)).toBe(900_000);
    expect(staleAfter(60)).toBe(180_000);
    // 0 means "only speaks while sweeping" — no cadence to go on.
    expect(staleAfter(0)).toBe(staleAfter(null));
  });

  it("treats a missing or unparseable timestamp as never seen", () => {
    expect(freshness(null, 300).cls).toBe("never");
    expect(freshness("not-a-date", 300).cls).toBe("never");
  });
});

describe("byStation", () => {
  const at = (station, id) => ({ bot_user_id: id, station });

  it("groups by host, in name order", () => {
    const groups = byStation([at("b", 1), at("a", 2), at("b", 3)]);
    expect(groups.map(([name]) => name)).toEqual(["a", "b"]);
    expect(groups[1][1].map((e) => e.bot_user_id)).toEqual([1, 3]);
  });

  it("sorts workers that report no station last", () => {
    const groups = byStation([at(null, 1), at("a", 2)]);
    expect(groups.map(([name]) => name)).toEqual(["a", ""]);
  });

  it("survives a roster where nothing reports a station", () => {
    // Every row written by a resolver older than this feature.
    expect(byStation([at(null, 1), at(undefined, 2)])).toHaveLength(1);
  });
});
