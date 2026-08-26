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

    expect(await screen.findByText("Why?")).toBeInTheDocument();
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
