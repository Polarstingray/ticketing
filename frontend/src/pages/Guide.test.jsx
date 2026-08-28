import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import Guide, { parseGuide } from "./Guide";

describe("Guide page", () => {
  it("renders the bundled markdown intro as a heading", () => {
    render(<Guide />);
    // The markdown's H1 becomes a real heading element.
    expect(
      screen.getByRole("heading", { name: /Stingray Tickets/i, level: 1 })
    ).toBeInTheDocument();
  });

  it("renders each H2 as a collapsible section", () => {
    const { container } = render(<Guide />);
    expect(container.querySelectorAll("details").length).toBeGreaterThan(1);
    // Sections start collapsed and carry a slug id for deep linking.
    const running = container.querySelector("#running-the-app");
    expect(running).toBeTruthy();
    expect(running.open).toBe(false);
    expect(within(running).getByText(/Running the app/i)).toBeInTheDocument();
  });

  it("lists every section in the appendix sidebar", () => {
    const { container } = render(<Guide />);
    const links = screen.getAllByRole("link", { name: /Running the app/i });
    expect(links[0]).toHaveAttribute("href", "#running-the-app");
    // One appendix link per section.
    const tocLinks = container.querySelectorAll("aside a");
    expect(tocLinks.length).toBe(container.querySelectorAll("details").length);
  });

  it("opens a section when its appendix link is clicked", () => {
    const { container } = render(<Guide />);
    fireEvent.click(screen.getAllByRole("link", { name: /Running the app/i })[0]);
    expect(container.querySelector("#running-the-app").open).toBe(true);
  });

  it("expands and collapses every section from the sidebar button", () => {
    const { container } = render(<Guide />);
    const total = container.querySelectorAll("details").length;

    fireEvent.click(screen.getByRole("button", { name: /Expand all/i }));
    expect(container.querySelectorAll("details[open]").length).toBe(total);

    fireEvent.click(screen.getByRole("button", { name: /Collapse all/i }));
    expect(container.querySelectorAll("details[open]").length).toBe(0);
  });
});

describe("parseGuide", () => {
  it("keeps everything before the first H2 as the intro", () => {
    const { intro, sections } = parseGuide("# Title\n\nlead in\n");
    expect(intro).toContain("lead in");
    expect(sections).toEqual([]);
  });

  it("gives a punctuation-only title a usable, non-empty slug", () => {
    const { sections } = parseGuide("# T\n\n## ```\n\nbody\n");
    expect(sections).toHaveLength(1);
    expect(sections[0].slug).toBe("section");
  });

  it("disambiguates titles that slugify to the same value", () => {
    const { sections } = parseGuide("# T\n\n## The App\n\na\n\n## the-app\n\nb\n");
    expect(sections.map((s) => s.slug)).toEqual(["the-app", "the-app-2"]);
  });

  it("does not let a generated slug collide with a later real one", () => {
    const md = "# T\n\n## Dup\n\na\n\n## Dup\n\nb\n\n## Dup 2\n\nc\n";
    const slugs = parseGuide(md).sections.map((s) => s.slug);
    expect(new Set(slugs).size).toBe(slugs.length);
  });

  it("handles an H2 with no body", () => {
    const { sections } = parseGuide("# T\n\n## Empty");
    expect(sections).toEqual([{ title: "Empty", slug: "empty", content: "" }]);
  });
});
