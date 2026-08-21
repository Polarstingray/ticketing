import hljs from "highlight.js";

// Highlight a snippet with highlight.js, falling back to escaped plain text so a
// bad language name or a parser error can never break the surrounding render.
// Shared by CodeBlockViewer (ticket code_blocks) and Markdown (fenced blocks).
export function highlight(content, language) {
  try {
    if (language && hljs.getLanguage(language)) {
      return hljs.highlight(content, { language }).value;
    }
    return hljs.highlightAuto(content).value;
  } catch {
    // Fall back to escaped plain text.
    const div = document.createElement("div");
    div.textContent = content;
    return div.innerHTML;
  }
}
