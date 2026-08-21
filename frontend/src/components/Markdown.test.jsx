import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import Markdown from "./Markdown";

describe("Markdown", () => {
  it("renders emphasis and headings as real elements", () => {
    render(<Markdown>{"## Findings\n\nA **blocker** was found."}</Markdown>);
    expect(screen.getByRole("heading", { name: "Findings" })).toBeInTheDocument();
    expect(screen.getByText("blocker").tagName).toBe("STRONG");
  });

  it("renders GFM tables", () => {
    const md = ["| Item | Points |", "| --- | --- |", "| Tests | 20 |"].join("\n");
    render(<Markdown>{md}</Markdown>);
    expect(screen.getByRole("table")).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Points" })).toBeInTheDocument();
  });

  it("renders GFM task lists as checkboxes", () => {
    render(<Markdown>{"- [ ] #12 stub one\n- [x] #13 stub two"}</Markdown>);
    const boxes = screen.getAllByRole("checkbox");
    expect(boxes).toHaveLength(2);
    expect(boxes[1]).toBeChecked();
  });

  it("syntax-highlights a fenced code block and labels its language", () => {
    const { container } = render(
      <Markdown>{"```python\ndef add(a, b):\n    return a + b\n```"}</Markdown>
    );
    const pre = container.querySelector("pre");
    expect(pre).toBeInTheDocument();
    expect(pre.textContent).toContain("def add(a, b):");
    // highlight.js wraps tokens in .hljs-* spans.
    expect(pre.querySelector("span[class^='hljs-']")).toBeInTheDocument();
    expect(screen.getByText("python")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Copy" })).toBeInTheDocument();
  });

  it("escapes raw HTML instead of rendering it", () => {
    // Comment bodies are attacker-influenced; rehype-raw must stay out.
    const { container } = render(
      <Markdown>{'<img src=x onerror="alert(1)"> and <b>bold</b>'}</Markdown>
    );
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("b")).toBeNull();
    expect(container.textContent).toContain("<img src=x");
  });

  it("keeps single newlines as line breaks for pre-markdown comments", () => {
    const { container } = render(<Markdown>{"line one\nline two"}</Markdown>);
    expect(container.querySelector("br")).toBeInTheDocument();
  });

  it("opens links in a new tab safely", () => {
    render(<Markdown>{"[docs](https://example.com)"}</Markdown>);
    const link = screen.getByRole("link", { name: "docs" });
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", expect.stringContaining("noopener"));
  });
});
