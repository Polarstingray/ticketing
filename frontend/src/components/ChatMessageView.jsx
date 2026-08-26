import Markdown from "./Markdown";
import { formatUsd } from "../constants";
import styles from "../styles/ChatWidget.module.css";

// One turn in the transcript. Assistant turns render as Markdown (the model
// answers in it, and tickets are full of code) and carry their cost; user turns
// are shown as plain text, since echoing a user's own words through a Markdown
// renderer would let a stray backtick reformat what they typed.
export default function ChatMessageView({ message }) {
  const isUser = message.role === "user";
  return (
    <div className={isUser ? styles.turnUser : styles.turnAssistant}>
      <div className={styles.turnBody}>
        {isUser ? message.content : <Markdown>{message.content}</Markdown>}
      </div>
      {!isUser && message.cost_usd > 0 && (
        <div className={styles.turnMeta}>
          <span title={message.model}>{message.model}</span>
          <span>{formatUsd(message.cost_usd)}</span>
        </div>
      )}
    </div>
  );
}
