import { useEffect, useRef, useState } from "react";
import { useChat } from "../chat/ChatContext";
import ChatMessageView from "./ChatMessageView";
import { formatUsd } from "../constants";
import styles from "../styles/ChatWidget.module.css";

// The floating assistant: a launcher in the corner and the popup it opens.
//
// Mounted in Layout, so it is present on every authenticated page — but it
// renders nothing at all unless the deployment has a model configured.
export default function ChatWidget() {
  const {
    config, open, setOpen, conversations, active, streaming, pending, toolEvents,
    error, ticketId, openThread, newThread, removeThread, send, stop,
  } = useChat();

  const [draft, setDraft] = useState("");
  const [showThreads, setShowThreads] = useState(false);
  const scrollRef = useRef(null);

  const messages = active?.messages || [];

  // Follow the transcript as it grows, including during a stream.
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages.length, pending, toolEvents.length, open]);

  if (!config.enabled) return null;

  function submit(event) {
    event.preventDefault();
    const text = draft.trim();
    if (!text || streaming) return;
    setDraft("");
    send(text);
  }

  function onKeyDown(event) {
    // Enter sends, Shift+Enter breaks the line — the convention every chat box
    // uses, and the composer is a textarea precisely so the latter works.
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      submit(event);
    }
  }

  if (!open) {
    return (
      <button
        type="button"
        className={styles.launcher}
        onClick={() => setOpen(true)}
        aria-label="Open the assistant"
      >
        💬
      </button>
    );
  }

  return (
    <section className={styles.panel} aria-label="Assistant">
      <header className={styles.head}>
        <button
          type="button"
          className={styles.headButton}
          onClick={() => setShowThreads((v) => !v)}
          aria-expanded={showThreads}
        >
          {active?.title || "Assistant"}
          <span className={styles.caret}>{showThreads ? "▴" : "▾"}</span>
        </button>
        <div className={styles.headActions}>
          <button type="button" onClick={newThread} title="New conversation">
            ＋
          </button>
          <button type="button" onClick={() => setOpen(false)} aria-label="Close">
            ✕
          </button>
        </div>
      </header>

      {showThreads && (
        <ul className={styles.threads}>
          {conversations.length === 0 && (
            <li className={styles.threadEmpty}>No conversations yet.</li>
          )}
          {conversations.map((c) => (
            <li key={c.id} className={styles.thread}>
              <button
                type="button"
                className={styles.threadOpen}
                onClick={() => {
                  openThread(c.id);
                  setShowThreads(false);
                }}
              >
                {c.title || "Untitled"}
                {c.ticket_id ? <span className={styles.threadTicket}>#{c.ticket_id}</span> : null}
              </button>
              <button
                type="button"
                className={styles.threadDelete}
                onClick={() => removeThread(c.id)}
                aria-label={`Delete conversation ${c.title || c.id}`}
              >
                🗑
              </button>
            </li>
          ))}
        </ul>
      )}

      <div className={styles.transcript} ref={scrollRef}>
        {messages.length === 0 && !pending && (
          <p className={styles.empty}>
            Ask about a ticket or a resolver run.
            {ticketId ? ` Ticket #${ticketId} is attached.` : ""}
          </p>
        )}
        {messages.map((m) => (
          <ChatMessageView key={m.id} message={m} />
        ))}
        {toolEvents.length > 0 && (
          // The live view of what it is looking at. The finished turn re-renders
          // this from `meta`, collapsed — see ChatMessageView.
          <ul className={styles.toolLive}>
            {toolEvents.map((call, i) => (
              <li key={i}>
                <code>{call.name}</code>
                {call.summary ? ` ✓ ${call.summary}` : " …"}
              </li>
            ))}
          </ul>
        )}
        {pending && (
          <div className={styles.turnAssistant}>
            <div className={styles.turnBody}>{pending}</div>
          </div>
        )}
        {streaming && !pending && toolEvents.length === 0 && (
          <div className={styles.thinking}>Thinking…</div>
        )}
      </div>

      {error && <p className={styles.error}>{error}</p>}

      <form className={styles.composer} onSubmit={submit}>
        <textarea
          className={styles.input}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder={ticketId ? `Ask about ticket #${ticketId}…` : "Ask a question…"}
          rows={2}
          aria-label="Message"
        />
        {streaming ? (
          <button type="button" onClick={stop} className={styles.stop}>
            Stop
          </button>
        ) : (
          <button type="submit" disabled={!draft.trim()}>
            Send
          </button>
        )}
      </form>

      <footer className={styles.foot}>
        <span>{config.model}</span>
        <span>
          {formatUsd(config.spent_today_usd || 0)}
          {config.daily_usd_limit > 0 ? ` of ${formatUsd(config.daily_usd_limit)} today` : " today"}
        </span>
      </footer>
    </section>
  );
}
