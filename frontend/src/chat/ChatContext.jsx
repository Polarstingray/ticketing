import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { useLocation } from "react-router-dom";
import { api } from "../api";
import { useAuth } from "../auth/AuthContext";

// State for the chat popup: whether it's open, which thread is active, the
// transcript, and the in-flight stream.
//
// The feature is optional, so `config.enabled` gates everything — the launcher
// never renders on a deployment without a model configured, and no chat request
// is ever made there.
const ChatContext = createContext(null);

// Derives the ticket the user is currently looking at from the URL. The widget
// lives in Layout, which sits *outside* the `/tickets/:id` route, so it can't
// reach useParams — and threading a ticket id down through Layout would couple
// every page to a feature only one of them cares about.
const TICKET_PATH = /^\/tickets\/(\d+)/;

export function currentTicketId(pathname) {
  const match = TICKET_PATH.exec(pathname || "");
  return match ? Number(match[1]) : null;
}

export function ChatProvider({ children }) {
  const { user } = useAuth();
  const { pathname } = useLocation();

  const [config, setConfig] = useState({ enabled: false, model: "" });
  const [open, setOpen] = useState(false);
  const [conversations, setConversations] = useState([]);
  const [active, setActive] = useState(null); // { id, title, messages: [] }
  const [streaming, setStreaming] = useState(false);
  // The assistant's partial answer while it streams. Kept apart from `active`
  // so each token doesn't rewrite the stored transcript.
  const [pending, setPending] = useState("");
  const [error, setError] = useState("");
  // What the assistant looked at during the turn currently streaming. Kept out
  // of `active` for the same reason `pending` is: it belongs to the live turn,
  // and the finished turn carries its own copy in `meta`.
  const [toolEvents, setToolEvents] = useState([]);
  const abortRef = useRef(null);

  const ticketId = currentTicketId(pathname);

  useEffect(() => {
    if (!user) {
      setConfig({ enabled: false, model: "" });
      setOpen(false);
      return;
    }
    api
      .chatConfig()
      .then(setConfig)
      // A deployment without the assistant answers this fine; a transient
      // failure just means no launcher this session, which is the safe default.
      .catch(() => setConfig({ enabled: false, model: "" }));
  }, [user]);

  const refreshConversations = useCallback(async () => {
    try {
      setConversations(await api.listConversations());
    } catch {
      // best-effort: the list is a convenience, not the transcript
    }
  }, []);

  useEffect(() => {
    if (config.enabled && open) refreshConversations();
  }, [config.enabled, open, refreshConversations]);

  const openThread = useCallback(async (id) => {
    setError("");
    try {
      setActive(await api.getConversation(id));
    } catch (err) {
      setError(err.message);
    }
  }, []);

  const newThread = useCallback(() => {
    // Created lazily on the first question: an empty thread the user abandons
    // would otherwise litter the list.
    setActive(null);
    setPending("");
    setError("");
  }, []);

  const removeThread = useCallback(
    async (id) => {
      try {
        await api.deleteConversation(id);
        setConversations((prev) => prev.filter((c) => c.id !== id));
        setActive((prev) => (prev && prev.id === id ? null : prev));
      } catch (err) {
        setError(err.message);
      }
    },
    []
  );

  const send = useCallback(
    async (text) => {
      const question = (text || "").trim();
      if (!question || streaming) return;
      setError("");

      let conversation = active;
      try {
        if (!conversation) {
          conversation = await api.createConversation(ticketId);
          setActive(conversation);
        }
      } catch (err) {
        setError(err.message);
        return;
      }

      // Show the question immediately; the server stores its own copy.
      const asked = {
        id: `local-${Date.now()}`,
        role: "user",
        content: question,
        cost_usd: 0,
      };
      setActive((prev) => ({ ...prev, messages: [...(prev?.messages || []), asked] }));
      setStreaming(true);
      setPending("");
      setToolEvents([]);

      const controller = new AbortController();
      abortRef.current = controller;
      let answer = "";

      try {
        await api.sendChatMessage(
          conversation.id,
          { content: question, ticket_id: ticketId },
          ({ event, data }) => {
            if (event === "token") {
              answer += data.text;
              setPending(answer);
            } else if (event === "tool_call") {
              setToolEvents((prev) => [
                ...prev,
                { name: data.name, args: data.args, summary: null },
              ]);
            } else if (event === "tool_result") {
              // Fills in the most recent unfinished entry for this tool: calls
              // and results arrive strictly paired and in order. A result with
              // no open call to match is dropped on purpose — it would mean the
              // server sent a frame for a tool it never announced, and inventing
              // a row for it would show the user something that never ran.
              setToolEvents((prev) => {
                const next = [...prev];
                for (let i = next.length - 1; i >= 0; i -= 1) {
                  if (next[i].name === data.name && next[i].summary === null) {
                    next[i] = { ...next[i], summary: data.summary };
                    break;
                  }
                }
                return next;
              });
            } else if (event === "error") {
              setError(data.detail || "The assistant failed to answer.");
            } else if (event === "done") {
              setActive((prev) => ({
                ...prev,
                title: data.title || prev.title,
                messages: [
                  ...(prev?.messages || []),
                  {
                    id: data.message_id,
                    role: "assistant",
                    content: answer,
                    // The same blob the server stored, so this turn renders
                    // identically now and after a reload.
                    meta: data.meta || {},
                    ...data.usage,
                  },
                ],
              }));
              setConfig((prev) => ({ ...prev, spent_today_usd: data.spent_today_usd }));
            }
          },
          { signal: controller.signal }
        );
      } catch (err) {
        setError(err.message);
      } finally {
        setStreaming(false);
        setPending("");
        setToolEvents([]);
        abortRef.current = null;
        refreshConversations();
      }
    },
    [active, streaming, ticketId, refreshConversations]
  );

  const stop = useCallback(() => {
    abortRef.current?.abort();
  }, []);

  // Abort an in-flight stream if the popup unmounts (logout, page teardown), so
  // the fetch doesn't outlive the component that was rendering it.
  useEffect(() => () => abortRef.current?.abort(), []);

  const value = useMemo(
    () => ({
      config,
      open,
      setOpen,
      conversations,
      active,
      streaming,
      pending,
      toolEvents,
      error,
      ticketId,
      openThread,
      newThread,
      removeThread,
      send,
      stop,
    }),
    [
      config, open, conversations, active, streaming, pending, toolEvents, error, ticketId,
      openThread, newThread, removeThread, send, stop,
    ]
  );

  return <ChatContext.Provider value={value}>{children}</ChatContext.Provider>;
}

export function useChat() {
  const ctx = useContext(ChatContext);
  if (!ctx) throw new Error("useChat must be used inside a ChatProvider");
  return ctx;
}
