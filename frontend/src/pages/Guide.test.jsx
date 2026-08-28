import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import Guide from "./Guide";

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
