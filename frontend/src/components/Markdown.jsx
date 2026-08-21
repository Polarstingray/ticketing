import { useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkBreaks from "remark-breaks";
import { highlight } from "../lib/highlight";
import styles from "../styles/Markdown.module.css";

// Pull the raw text out of a fence's <code> child. react-markdown hands us React
// elements, and the code text is always a plain string child of that element.
function fenceText(node) {
  const child = node?.props?.children;
  if (typeof child === "string") return child;
  if (Array.isArray(child)) return child.filter((c) => typeof c === "string").join("");
  return "";
}

function fenceLanguage(node) {
  const className = node?.props?.className || "";
  const match = /language-([\w+-]+)/.exec(className);
  return match ? match[1] : "";
}

function CopyButton({ text }) {
  const [copied, setCopied] = useState(false);

  async function copy() {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // Clipboard is unavailable (insecure origin, denied permission) — leave the
      // label alone rather than claiming a copy that did not happen.
    }
  }

  return (
    <button type="button" className={styles.copy} onClick={copy}>
      {copied ? "Copied" : "Copy"}
    </button>
  );
}

// Fenced code blocks. We override `pre` rather than `code` because react-markdown v9
// dropped the `inline` prop, so `pre` is the only reliable place to tell a fenced
// block from inline code. The language rides on the child <code>'s className.
function CodeFence({ children }) {
  const child = Array.isArray(children) ? children[0] : children;
  const text = fenceText(child);
  const language = fenceLanguage(child);
  const html = highlight(text.replace(/\n$/, ""), language);

  return (
    <div className={styles.fence}>
      <div className={styles.fenceHead}>
        <span className={styles.fenceLang}>{language || "plaintext"}</span>
        <CopyButton text={text} />
      </div>
      <pre className={styles.fenceCode}>
        <code dangerouslySetInnerHTML={{ __html: html }} />
      </pre>
    </div>
  );
}

function ExternalLink({ children, ...props }) {
  return (
    <a {...props} target="_blank" rel="noopener noreferrer">
      {children}
    </a>
  );
}

// Wide tables would otherwise blow out the page width — give each its own scroller.
function Table({ children, ...props }) {
  return (
    <div className={styles.tableWrap}>
      <table {...props}>{children}</table>
    </div>
  );
}

const COMPONENTS = { pre: CodeFence, a: ExternalLink, table: Table };

// Renders agent- and user-authored markdown (comment bodies, ticket descriptions).
// Note: no rehype-raw. Bodies are attacker-influenced — any user can comment — and
// react-markdown escapes raw HTML by default, which is what keeps this safe without
// a separate sanitizer. Do not add rehype-raw here.
//
// remark-breaks keeps single newlines as line breaks, so comments written before
// markdown rendering existed (against `white-space: pre-wrap`) still read correctly.
export default function Markdown({ children, className = "" }) {
  return (
    <div className={`${styles.md} ${className}`}>
      <ReactMarkdown remarkPlugins={[remarkGfm, remarkBreaks]} components={COMPONENTS}>
        {children}
      </ReactMarkdown>
    </div>
  );
}
