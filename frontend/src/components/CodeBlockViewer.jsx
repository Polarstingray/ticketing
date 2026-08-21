import { useMemo } from "react";
import { highlight } from "../lib/highlight";
import styles from "../styles/CodeBlockViewer.module.css";

export default function CodeBlockViewer({ block }) {
  const { filename, line_start, line_end, content, language } = block;

  // Highlight the whole block, then split into lines so we can attach a gutter
  // and per-line range highlighting. We highlight per-line to keep numbering
  // simple; this is fine for the short snapshots stored in tickets.
  const lines = useMemo(() => {
    const raw = (content ?? "").replace(/\n$/, "");
    return raw.split("\n").map((line) => highlight(line === "" ? " " : line, language));
  }, [content, language]);

  const start = line_start || 1;

  return (
    <div className={styles.viewer}>
      <div className={styles.header}>
        <span className={styles.filename}>{filename}</span>
        <span className={styles.meta}>
          {language || "plaintext"} · lines {line_start}–{line_end}
        </span>
      </div>
      <pre className={styles.code}>
        <code>
          {lines.map((html, i) => {
            const lineNo = start + i;
            const inRange = lineNo >= line_start && lineNo <= line_end;
            return (
              <span
                key={i}
                className={`${styles.line} ${inRange ? styles.highlighted : ""}`}
              >
                <span className={styles.gutter}>{lineNo}</span>
                <span
                  className={styles.lineContent}
                  dangerouslySetInnerHTML={{ __html: html }}
                />
              </span>
            );
          })}
        </code>
      </pre>
    </div>
  );
}
