import ReactMarkdown from "react-markdown";
import guide from "../content/guide.md?raw";
import styles from "../styles/Guide.module.css";

// A static, in-app operator guide. The prose is bundled markdown (guide.md),
// rendered with react-markdown so it stays easy to edit and consistent with the
// repo README/resolver docs it summarizes.
export default function Guide() {
  return (
    <div className={styles.wrap}>
      <article className={`card ${styles.doc}`}>
        <ReactMarkdown>{guide}</ReactMarkdown>
      </article>
    </div>
  );
}
