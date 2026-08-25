import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Webhooks from "./Webhooks";
import { api } from "../api";

vi.mock("../api", () => ({
  api: {
    listWebhooks: vi.fn(),
    createWebhook: vi.fn(),
    updateWebhook: vi.fn(),
    deleteWebhook: vi.fn(),
    rotateWebhookSecret: vi.fn(),
    listWebhookDeliveries: vi.fn(),
    redeliverWebhookDelivery: vi.fn(),
  },
}));

const SECRET = "s3cret-token-value";

function hook(overrides = {}) {
  return {
    id: 1,
    user_id: 7,
    name: "ci",
    url: "https://example.com/hook",
    event_types: ["ticket.created"],
    tag_filter: ["repo:foo"],
    active: true,
    consecutive_failures: 0,
    secret_prefix: SECRET.slice(0, 8),
    created_at: "2026-08-01T00:00:00Z",
    updated_at: "2026-08-01T00:00:00Z",
    ...overrides,
  };
}

function renderPage() {
  return render(
    <MemoryRouter>
      <Webhooks />
    </MemoryRouter>
  );
}

async function fillAndSubmit() {
  fireEvent.change(screen.getByLabelText("Name"), { target: { value: "ci" } });
  fireEvent.change(screen.getByLabelText("URL"), {
    target: { value: "https://example.com/hook" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Create webhook" }));
}

describe("Webhooks", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.listWebhooks.mockResolvedValue([]);
  });

  it("shows the secret once after create and never again from the list", async () => {
    api.createWebhook.mockResolvedValue({ ...hook(), secret: SECRET });
    // The list refresh after create goes through a read endpoint, which never
    // carries the plaintext.
    api.listWebhooks.mockResolvedValueOnce([]).mockResolvedValue([hook()]);

    renderPage();
    await waitFor(() => expect(api.listWebhooks).toHaveBeenCalled());
    await fillAndSubmit();

    await screen.findByText(SECRET);
    expect(screen.getByText(/shown/i)).toBeInTheDocument();

    // Dismissing is final: the refreshed list shows only the prefix.
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    await waitFor(() => expect(screen.queryByText(SECRET)).not.toBeInTheDocument());
    expect(screen.getByText(/secret s3cret-t…/)).toBeInTheDocument();
  });

  it("surfaces the SSRF rejection message verbatim", async () => {
    const err = new Error("webhook URL rejected: host is a loopback address");
    err.status = 422;
    api.createWebhook.mockRejectedValue(err);

    renderPage();
    await waitFor(() => expect(api.listWebhooks).toHaveBeenCalled());
    await fillAndSubmit();

    expect(
      await screen.findByText("webhook URL rejected: host is a loopback address")
    ).toBeInTheDocument();
  });

  it("opens the delivery log and redelivers a row", async () => {
    api.listWebhooks.mockResolvedValue([hook()]);
    api.listWebhookDeliveries.mockResolvedValue({
      items: [
        {
          id: 11,
          webhook_id: 1,
          event_id: 3,
          event_type: "ticket.created",
          ticket_id: 42,
          attempt_count: 2,
          next_attempt_at: null,
          status_code: 500,
          response_snippet: "boom",
          error: "",
          state: "failed",
          created_at: "2026-08-02T00:00:00Z",
          updated_at: "2026-08-02T00:00:00Z",
        },
      ],
      total: 1,
      limit: 20,
      offset: 0,
    });
    api.redeliverWebhookDelivery.mockResolvedValue({});

    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /Delivery log/ }));

    expect(await screen.findByText("ticket.created")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "#42" })).toHaveAttribute("href", "/tickets/42");
    // Scoped to the row: "failed" is also one of the filter <option>s.
    const row = screen.getByRole("link", { name: "#42" }).closest("tr");
    expect(within(row).getByText("failed")).toBeInTheDocument();
    expect(within(row).getByText("500")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Redeliver" }));
    await waitFor(() => expect(api.redeliverWebhookDelivery).toHaveBeenCalledWith(1, 11));
  });
});
