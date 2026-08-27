import Markdown from "./Markdown";
import ProposedAction from "./ProposedAction";
import { formatUsd } from "../constants";
import styles from "../styles/ChatWidget.module.css";

// One turn in the transcript. Assistant turns render as Markdown (the model
// answers in it, and tickets are full of code) and carry their cost; user turns
// are shown as plain text, since echoing a user's own words through a Markdown
// renderer would let a stray backtick reformat what they typed.
//
// Tool calls and proposed actions come from `message.meta`, which the server
// sends on the `done` frame *and* stores — so a turn looks the same live as it
// does after the thread is reopened.
export default function ChatMessageView({ message }) {
  const isUser = message.role === "user";
  const toolCalls = message.meta?.tool_calls || [];
  const proposals = message.meta?.proposed_actions || [];

  return (
    <div className={isUser ? styles.turnUser : styles.turnAssistant}>
      {toolCalls.length > 0 && (
        // Native <details>: this is the app's first disclosure, and the element
        // brings its keyboard and screen-reader behaviour with it rather than
        // needing a convention invented for one widget.
        <details className={styles.tools}>
          <summary>
            Looked at {toolCalls.length} thing{toolCalls.length === 1 ? "" : "s"}
          </summary>
          <ul className={styles.toolList}>
            {toolCalls.map((call, i) => (
              <li key={i}>
                <code>{call.name}</code>
                {call.summary ? ` — ${call.summary}` : ""}
              </li>
            ))}
          </ul>
        </details>
      )}

      <div className={styles.turnBody}>
        {isUser ? message.content : <Markdown>{message.content}</Markdown>}
      </div>

      {proposals.map((proposal, i) => (
        <ProposedAction key={i} proposal={proposal} />
      ))}

      {!isUser && message.cost_usd > 0 && (
        <div className={styles.turnMeta}>
          <span title={message.model}>{message.model}</span>
          <span>{formatUsd(message.cost_usd)}</span>
        </div>
      )}
    </div>
  );
}
