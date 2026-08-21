import { useState } from "react";
import Markdown from "./Markdown";
import styles from "../styles/Markdown.module.css";

// A textarea with a Write/Preview toggle, so an author can check formatting before
// posting. Used by both the new-comment form and the inline comment editor.
export default function MarkdownEditor({ value, onChange, placeholder }) {
  const [preview, setPreview] = useState(false);

  return (
    <div className={styles.editor}>
      <div className={styles.tabs} role="tablist">
        <button
          type="button"
          role="tab"
          aria-selected={!preview}
          className={preview ? "" : styles.tabActive}
          onClick={() => setPreview(false)}
        >
          Write
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={preview}
          className={preview ? styles.tabActive : ""}
          onClick={() => setPreview(true)}
        >
          Preview
        </button>
      </div>
      {preview ? (
        <div className={styles.previewPane}>
          {value.trim() ? (
            <Markdown>{value}</Markdown>
          ) : (
            <span className="muted">Nothing to preview.</span>
          )}
        </div>
      ) : (
        <textarea
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={placeholder}
        />
      )}
    </div>
  );
}
