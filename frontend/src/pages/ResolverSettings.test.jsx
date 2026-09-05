import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import ResolverSettings, { byStation, freshness, staleAfter } from "./ResolverSettings";
import { api } from "../api";

vi.mock("../api", () => ({
  api: {
    listResolvers: vi.fn(),
    listAgents: vi.fn(),
    listEnrollments: vi.fn(),
    createEnrollment: vi.fn(),
    revokeEnrollment: vi.fn(),
    listApiKeys: vi.fn(),
    revokeApiKey: vi.fn(),
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
    api.listEnrollments.mockResolvedValue([]);
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

describe("station enrolment", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.listResolvers.mockResolvedValue([]);
    api.listAgents.mockResolvedValue([]);
    api.getResolverSettings.mockResolvedValue(settings());
    api.listEnrollments.mockResolvedValue([]);
  });

  it("shows the minted token once, with a warning that it will not be shown again", async () => {
    api.createEnrollment.mockResolvedValue({
      id: 1,
      username: "gemini-bot",
      token: "st_abcdef",
      expires_at: new Date(Date.now() + 3_600_000).toISOString(),
    });
    renderPage();

    const field = await screen.findByPlaceholderText("gemini-bot");
    fireEvent.change(field, { target: { value: "gemini-bot" } });
    fireEvent.click(screen.getByRole("button", { name: "Mint token" }));

    expect(await screen.findByText("st_abcdef")).toBeInTheDocument();
    expect(screen.getByText(/not shown again/i)).toBeInTheDocument();
  });

  it("explains the re-login rule rather than reporting a bare 401", async () => {
    // `require_recent_admin` is the gate the whole feature rests on, so a stale
    // session is an expected outcome to explain, not an error to apologise for.
    const err = new Error("reauth_required");
    err.status = 401;
    api.createEnrollment.mockRejectedValue(err);
    renderPage();

    const field = await screen.findByPlaceholderText("gemini-bot");
    fireEvent.change(field, { target: { value: "x" } });
    fireEvent.click(screen.getByRole("button", { name: "Mint token" }));

    expect(await screen.findByText(/last 15 minutes/)).toBeInTheDocument();
    expect(screen.queryByText("reauth_required")).not.toBeInTheDocument();
  });

  it("offers revoke on a pending enrolment and not on a spent one", async () => {
    api.listEnrollments.mockResolvedValue([
      {
        id: 1, username: "pending", token_prefix: "st_aaaaaaa",
        created_at: new Date().toISOString(),
        expires_at: new Date(Date.now() + 3_600_000).toISOString(),
        redeemed_at: null, redeemed_user_id: null, station: "",
      },
      {
        id: 2, username: "spent", token_prefix: "st_bbbbbbb",
        created_at: new Date().toISOString(),
        expires_at: new Date(Date.now() + 3_600_000).toISOString(),
        redeemed_at: new Date().toISOString(), redeemed_user_id: 7,
        station: "ubvm.home.lab",
      },
    ]);
    renderPage();

    expect(await screen.findByText("pending")).toBeInTheDocument();
    expect(screen.getByText(/redeemed on ubvm.home.lab/)).toBeInTheDocument();
    // One row is revocable; the redeemed one is a record, not a live token.
    expect(screen.getAllByRole("button", { name: "Revoke" })).toHaveLength(1);
  });

  it("keeps the settings form usable when enrolments cannot be listed", async () => {
    api.listEnrollments.mockRejectedValue(new Error("boom"));
    renderPage();
    await waitFor(() => expect(screen.getByText("Models")).toBeInTheDocument());
    expect(screen.queryByText("boom")).not.toBeInTheDocument();
  });
});

function resolverRow(overrides = {}) {
  return {
    bot_user_id: 7,
    username: "station-test",
    display_name: "Station test bot",
    is_bot: true,
    has_settings: false,
    name: "station-test",
    label: ".env.station-test",
    agent: "claude",
    model: "",
    last_seen_at: null,
    effective_config: null,
    station: null,
    heartbeat_seconds: 0,
    ...overrides,
  };
}

function apiKey(overrides = {}) {
  return {
    id: 3,
    name: "resolver",
    key_prefix: "sk_abcdefgh",
    created_at: new Date().toISOString(),
    last_used_at: null,
    expires_at: null,
    revoked: false,
    scopes: [],
    ...overrides,
  };
}

describe("revoking a resolver bot's access", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.listResolvers.mockResolvedValue([resolverRow()]);
    api.listAgents.mockResolvedValue([]);
    api.getResolverSettings.mockResolvedValue(settings());
    api.listEnrollments.mockResolvedValue([]);
    api.listApiKeys.mockResolvedValue([apiKey()]);
  });

  it("offers revoke on a live key once a resolver is selected", async () => {
    renderPage();
    fireEvent.click(await screen.findByText("station-test"));

    expect(await screen.findByText("sk_abcdefgh…")).toBeInTheDocument();
    expect(api.listApiKeys).toHaveBeenCalledWith(7);
    expect(screen.getByRole("button", { name: "Revoke" })).toBeInTheDocument();
  });

  it("shows a revoked key as spent rather than revocable again", async () => {
    api.listApiKeys.mockResolvedValue([apiKey({ revoked: true })]);
    renderPage();
    fireEvent.click(await screen.findByText("station-test"));

    expect(await screen.findByText("revoked")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Revoke" })).not.toBeInTheDocument();
  });

  it("sends a redeemed enrolment to the bot's keys instead of a dead revoke", async () => {
    // The 409 from the API says "revoke the bot's API key instead"; before this
    // the row said `bot #7` and named an action the UI did not offer anywhere.
    api.listEnrollments.mockResolvedValue([
      {
        id: 2, username: "station-test", token_prefix: "st_bbbbbbb",
        created_at: new Date().toISOString(),
        expires_at: new Date(Date.now() + 3_600_000).toISOString(),
        redeemed_at: new Date().toISOString(), redeemed_user_id: 7,
        station: "ubvm.home.lab",
      },
    ]);
    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: /bot #7/ }));
    expect(await screen.findByText("sk_abcdefgh…")).toBeInTheDocument();
  });

  it("reports a key listing failure without blanking the page", async () => {
    api.listApiKeys.mockRejectedValue(new Error("nope"));
    renderPage();
    fireEvent.click(await screen.findByText("station-test"));

    expect(await screen.findByText("nope")).toBeInTheDocument();
    expect(screen.getByText("Models")).toBeInTheDocument();
  });
});
