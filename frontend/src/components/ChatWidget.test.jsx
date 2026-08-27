import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import ChatWidget from "./ChatWidget";
import { ChatProvider, currentTicketId } from "../chat/ChatContext";
import { api } from "../api";

// useAuth is mocked: the provider only fetches config once a user is signed in.
const authState = vi.hoisted(() => ({ user: { id: 1, display_name: "Ada" } }));
vi.mock("../auth/AuthContext", () => ({
  useAuth: () => ({ user: authState.user }),
}));

vi.mock("../api", () => ({
  api: {
    chatConfig: vi.fn(),
    listConversations: vi.fn(),
    createConversation: vi.fn(),
    getConversation: vi.fn(),
    deleteConversation: vi.fn(),
    sendChatMessage: vi.fn(),
    // The endpoints a proposed-action card confirms through. There are no new
    // ones: the card calls what the rest of the app calls.
    createTicket: vi.fn(),
    updateTicket: vi.fn(),
    addComment: vi.fn(),
  },
}));

const ENABLED = {
  enabled: true,
  model: "test-model",
  daily_usd_limit: 0.5,
  spent_today_usd: 0.01,
};

function renderWidget({ path = "/tickets" } = {}) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <ChatProvider>
        <ChatWidget />
      </ChatProvider>
    </MemoryRouter>
  );
}

/** Drive the onEvent callback the way a real stream would. */
function streamsBack(frames) {
  api.sendChatMessage.mockImplementation(async (id, body, onEvent) => {
    frames.forEach((frame) => onEvent(frame));
  });
}

async function openPopup() {
  const launcher = await screen.findByLabelText("Open the assistant");
  fireEvent.click(launcher);
  return screen.findByLabelText("Assistant");
}

beforeEach(() => {
  api.chatConfig.mockResolvedValue(ENABLED);
  api.listConversations.mockResolvedValue([]);
  api.createConversation.mockResolvedValue({ id: 7, title: "", messages: [] });
  streamsBack([]);
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("the launcher", () => {
  it("renders nothing when the deployment has no model configured", async () => {
    api.chatConfig.mockResolvedValue({ enabled: false, model: "" });
    renderWidget();
    await waitFor(() => expect(api.chatConfig).toHaveBeenCalled());
    expect(screen.queryByLabelText("Open the assistant")).not.toBeInTheDocument();
  });

  it("renders nothing when the config request fails", async () => {
    // A transient failure means no launcher this session — the safe default.
    api.chatConfig.mockRejectedValue(new Error("boom"));
    renderWidget();
    await waitFor(() => expect(api.chatConfig).toHaveBeenCalled());
    expect(screen.queryByLabelText("Open the assistant")).not.toBeInTheDocument();
  });

  it("opens the popup when enabled", async () => {
    renderWidget();
    await openPopup();
    expect(screen.getByLabelText("Message")).toBeInTheDocument();
  });
});

describe("ticket awareness", () => {
  it("derives the current ticket from the URL", () => {
    expect(currentTicketId("/tickets/42")).toBe(42);
    expect(currentTicketId("/tickets/42/anything")).toBe(42);
    expect(currentTicketId("/tickets")).toBeNull();
    expect(currentTicketId("/tickets/new")).toBeNull();
    expect(currentTicketId("/profile")).toBeNull();
    expect(currentTicketId(undefined)).toBeNull();
  });

  it("attaches the ticket being viewed to the conversation and the turn", async () => {
    renderWidget({ path: "/tickets/42" });
    await openPopup();
    expect(screen.getByText(/Ticket #42 is attached/)).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Message"), { target: { value: "Why?" } });
    fireEvent.click(screen.getByText("Send"));

    await waitFor(() => expect(api.createConversation).toHaveBeenCalledWith(42));
    expect(api.sendChatMessage).toHaveBeenCalledWith(
      7,
      { content: "Why?", ticket_id: 42 },
      expect.any(Function),
      expect.objectContaining({ signal: expect.anything() })
    );
  });

  it("sends no ticket from a page that isn't a ticket", async () => {
    renderWidget({ path: "/tickets" });
    await openPopup();
    fireEvent.change(screen.getByLabelText("Message"), { target: { value: "Hi" } });
    fireEvent.click(screen.getByText("Send"));
    await waitFor(() => expect(api.createConversation).toHaveBeenCalledWith(null));
  });
});

describe("streaming a turn", () => {
  it("shows the question immediately and appends the streamed answer", async () => {
    streamsBack([
      { event: "token", data: { text: "Because " } },
      { event: "token", data: { text: "the verify step failed." } },
      {
        event: "done",
        data: {
          message_id: 91,
          conversation_id: 7,
          title: "Why?",
          usage: { model: "test-model", cost_usd: 0.0042 },
          spent_today_usd: 0.0142,
        },
      },
    ]);
    renderWidget({ path: "/tickets/42" });
    await openPopup();

    fireEvent.change(screen.getByLabelText("Message"), { target: { value: "Why?" } });
    fireEvent.click(screen.getByText("Send"));

    // "Why?" legitimately appears twice once the done frame lands: as the user's
    // turn in the transcript, and again in the header, because a thread's title
    // is derived from its first question. Assert both rather than picking one —
    // that duplication is the derived-title behaviour, not an accident.
    await waitFor(() => expect(screen.getAllByText("Why?")).toHaveLength(2));
    expect(
      await screen.findByText("Because the verify step failed.")
    ).toBeInTheDocument();
    // The done frame's usage lands on the turn, and the footer total updates.
    expect(await screen.findByText("$0.0042")).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByText(/\$0\.0142 of \$0\.5000 today/)).toBeInTheDocument()
    );
  });

  it("clears the composer on send", async () => {
    renderWidget();
    await openPopup();
    const input = screen.getByLabelText("Message");
    fireEvent.change(input, { target: { value: "Hello" } });
    fireEvent.click(screen.getByText("Send"));
    await waitFor(() => expect(input.value).toBe(""));
  });

  it("sends on Enter but not on Shift+Enter", async () => {
    renderWidget();
    await openPopup();
    const input = screen.getByLabelText("Message");

    fireEvent.change(input, { target: { value: "Shift line" } });
    fireEvent.keyDown(input, { key: "Enter", shiftKey: true });
    expect(api.sendChatMessage).not.toHaveBeenCalled();

    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => expect(api.sendChatMessage).toHaveBeenCalled());
  });

  it("refuses to send an empty or whitespace-only draft", async () => {
    renderWidget();
    await openPopup();
    const input = screen.getByLabelText("Message");
    expect(screen.getByText("Send")).toBeDisabled();

    fireEvent.change(input, { target: { value: "   " } });
    expect(screen.getByText("Send")).toBeDisabled();
    fireEvent.keyDown(input, { key: "Enter" });
    expect(api.sendChatMessage).not.toHaveBeenCalled();
  });

  it("surfaces an error frame without losing the question", async () => {
    streamsBack([
      { event: "error", data: { detail: "The model provider is rate-limiting requests." } },
    ]);
    renderWidget();
    await openPopup();
    fireEvent.change(screen.getByLabelText("Message"), { target: { value: "Hi" } });
    fireEvent.click(screen.getByText("Send"));

    expect(
      await screen.findByText("The model provider is rate-limiting requests.")
    ).toBeInTheDocument();
    expect(screen.getByText("Hi")).toBeInTheDocument();
  });

  it("surfaces a pre-stream refusal, which throws instead of framing", async () => {
    // The budget cap, an unreadable ticket and an unconfigured provider are all
    // checked before the stream opens, so they arrive as a rejected promise.
    const err = new Error("Daily chat budget reached: $0.5000 of $0.50 used.");
    err.status = 429;
    api.sendChatMessage.mockRejectedValue(err);

    renderWidget();
    await openPopup();
    fireEvent.change(screen.getByLabelText("Message"), { target: { value: "Hi" } });
    fireEvent.click(screen.getByText("Send"));

    expect(await screen.findByText(/Daily chat budget reached/)).toBeInTheDocument();
  });
});

describe("threads", () => {
  it("lists conversations and opens one", async () => {
    api.listConversations.mockResolvedValue([
      { id: 3, title: "An older question", ticket_id: 42 },
    ]);
    api.getConversation.mockResolvedValue({
      id: 3,
      title: "An older question",
      messages: [{ id: 1, role: "assistant", content: "An older answer", cost_usd: 0 }],
    });

    renderWidget();
    await openPopup();
    fireEvent.click(screen.getByText("Assistant"));

    fireEvent.click(await screen.findByText("An older question"));
    expect(await screen.findByText("An older answer")).toBeInTheDocument();
    expect(api.getConversation).toHaveBeenCalledWith(3);
  });

  it("deletes a conversation", async () => {
    api.listConversations.mockResolvedValue([{ id: 3, title: "Doomed", ticket_id: null }]);
    api.deleteConversation.mockResolvedValue(null);

    renderWidget();
    await openPopup();
    fireEvent.click(screen.getByText("Assistant"));
    fireEvent.click(await screen.findByLabelText("Delete conversation Doomed"));

    await waitFor(() => expect(api.deleteConversation).toHaveBeenCalledWith(3));
    await waitFor(() => expect(screen.queryByText("Doomed")).not.toBeInTheDocument());
  });
});

// --- Phase 3: tool calls and proposed actions -------------------------------
// `streamsBack` already drives onEvent synchronously, so a new frame type is
// just a longer list — no reader-level plumbing is involved.

const DONE = {
  event: "done",
  data: {
    message_id: 11,
    title: "T",
    usage: { model: "test-model", cost_usd: 0 },
    spent_today_usd: 0.02,
    meta: {},
  },
};

function doneWith(meta) {
  return { ...DONE, data: { ...DONE.data, meta } };
}

async function ask(text = "Why?", { fresh = true } = {}) {
  if (fresh) {
    renderWidget();
    await openPopup();
  }
  fireEvent.change(screen.getByLabelText("Message"), { target: { value: text } });
  fireEvent.click(screen.getByText("Send"));
}

describe("tool calls", () => {
  it("shows what it is looking at while the turn streams", async () => {
    // The stream is held open so the live list can be observed before `done`
    // clears it — the whole point of keeping toolEvents out of `active`.
    let finish;
    api.sendChatMessage.mockImplementation(async (id, body, onEvent) => {
      onEvent({ event: "tool_call", data: { name: "search_tickets", args: {} } });
      await new Promise((resolve) => {
        finish = () => {
          onEvent({
            event: "tool_result",
            data: { name: "search_tickets", summary: "3 tickets" },
          });
          onEvent(DONE);
          resolve();
        };
      });
    });
    await ask();

    expect(await screen.findByText("search_tickets")).toBeInTheDocument();
    await waitFor(() => expect(finish).toBeTypeOf("function"));
    finish();
    await waitFor(() => expect(screen.queryByText(/…/)).not.toBeInTheDocument());
  });

  it("renders a finished turn's tool calls in a collapsed disclosure", async () => {
    streamsBack([
      { event: "token", data: { text: "Here." } },
      doneWith({
        tool_calls: [
          { name: "search_tickets", args: {}, summary: "3 tickets" },
          { name: "get_ticket", args: { ticket_id: 4 }, summary: "1.2k chars" },
        ],
      }),
    ]);
    await ask();

    const disclosure = await screen.findByText("Looked at 2 things");
    // Collapsed by default: the <details> that owns the list is closed.
    expect(disclosure.closest("details")).not.toHaveAttribute("open");
    fireEvent.click(disclosure);
    expect(await screen.findByText(/3 tickets/)).toBeInTheDocument();
  });

  it("renders no disclosure on a turn that used no tools", async () => {
    streamsBack([{ event: "token", data: { text: "Plain." } }, doneWith({})]);
    await ask();
    await screen.findByText("Plain.");
    expect(screen.queryByText(/Looked at/)).not.toBeInTheDocument();
  });

  it("does not leak one turn's tool activity into the next", async () => {
    streamsBack([
      { event: "tool_call", data: { name: "search_tickets", args: {} } },
      { event: "tool_result", data: { name: "search_tickets", summary: "3 tickets" } },
      { event: "token", data: { text: "First." } },
      doneWith({ tool_calls: [{ name: "search_tickets", summary: "3 tickets" }] }),
    ]);
    await ask("First question");
    await screen.findByText("First.");

    streamsBack([{ event: "token", data: { text: "Second." } }, doneWith({})]);
    await ask("Second question", { fresh: false });
    await screen.findByText("Second.");
    // Exactly one disclosure — the first turn's. The live list was cleared.
    expect(screen.getAllByText(/Looked at/)).toHaveLength(1);
  });
});

describe("proposed actions", () => {
  const CREATE = {
    kind: "create_ticket",
    payload: { type: "task", title: "Proposed ticket", description: "why", priority: "medium", tags: [] },
    rationale: "You asked for one.",
  };

  async function propose(proposal) {
    streamsBack([
      { event: "token", data: { text: "Sure." } },
      doneWith({ proposed_actions: [proposal] }),
    ]);
    await ask();
    return screen.findByRole("button", { name: "Confirm" });
  }

  it("renders the proposal as a card without acting on it", async () => {
    await propose(CREATE);
    expect(screen.getByText("Proposed ticket")).toBeInTheDocument();
    expect(screen.getByText("You asked for one.")).toBeInTheDocument();
    // **The load-bearing assertion.** The assistant has no write path; nothing
    // happens until a human clicks.
    expect(api.createTicket).not.toHaveBeenCalled();
    expect(api.addComment).not.toHaveBeenCalled();
    expect(api.updateTicket).not.toHaveBeenCalled();
  });

  it("files the ticket through the existing endpoint on Confirm", async () => {
    api.createTicket.mockResolvedValue({ id: 99 });
    const confirm = await propose(CREATE);
    fireEvent.click(confirm);
    await waitFor(() => expect(api.createTicket).toHaveBeenCalledWith(CREATE.payload));
    expect(await screen.findByText("Filed #99")).toBeInTheDocument();
  });

  it("posts a comment through the existing endpoint on Confirm", async () => {
    api.addComment.mockResolvedValue({ id: 5 });
    const confirm = await propose({
      kind: "add_comment",
      payload: { ticket_id: 42, body: "Proposed comment" },
      rationale: "r",
    });
    fireEvent.click(confirm);
    await waitFor(() => expect(api.addComment).toHaveBeenCalledWith(42, "Proposed comment"));
  });

  it("changes the status through the existing endpoint on Confirm", async () => {
    api.updateTicket.mockResolvedValue({ id: 42 });
    const confirm = await propose({
      kind: "set_status",
      payload: { ticket_id: 42, status: "resolved" },
      rationale: "r",
    });
    fireEvent.click(confirm);
    await waitFor(() =>
      expect(api.updateTicket).toHaveBeenCalledWith(42, { status: "resolved" })
    );
  });

  it("sends the user to the ticket page for request_fix rather than acting", async () => {
    streamsBack([
      { event: "token", data: { text: "Sure." } },
      doneWith({
        proposed_actions: [
          { kind: "request_fix", payload: { ticket_id: 42 }, rationale: "r" },
        ],
      }),
    ]);
    await ask();
    // The real "Apply fixes" button owns the guards; this is a link to it.
    const link = await screen.findByRole("link", { name: "Open ticket #42" });
    expect(link).toHaveAttribute("href", "/tickets/42");
    expect(screen.queryByRole("button", { name: "Confirm" })).not.toBeInTheDocument();
  });

  it("shows the endpoint's error inline and keeps the card", async () => {
    api.createTicket.mockRejectedValue(new Error("Reserved tag not allowed"));
    const confirm = await propose(CREATE);
    fireEvent.click(confirm);
    expect(await screen.findByText("Reserved tag not allowed")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Confirm" })).toBeInTheDocument();
  });

  it("dismisses a proposal without calling anything", async () => {
    await propose(CREATE);
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    await waitFor(() =>
      expect(screen.queryByText("Proposed ticket")).not.toBeInTheDocument()
    );
    expect(api.createTicket).not.toHaveBeenCalled();
  });

  it("renders tool calls and proposals from a reopened thread", async () => {
    // Same output as the live path, because `done.meta` is the stored blob.
    api.listConversations.mockResolvedValue([{ id: 3, title: "Old", updated_at: "" }]);
    api.getConversation.mockResolvedValue({
      id: 3,
      title: "Old",
      messages: [
        { id: 1, role: "user", content: "Why?", cost_usd: 0 },
        {
          id: 2,
          role: "assistant",
          content: "Because.",
          cost_usd: 0,
          meta: {
            tool_calls: [{ name: "get_ticket", summary: "1.2k chars" }],
            proposed_actions: [CREATE],
          },
        },
      ],
    });
    renderWidget();
    await openPopup();
    fireEvent.click(screen.getByText("Assistant"));
    fireEvent.click(await screen.findByText("Old"));

    expect(await screen.findByText("Looked at 1 thing")).toBeInTheDocument();
    expect(screen.getByText("Proposed ticket")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Confirm" })).toBeInTheDocument();
  });
});
