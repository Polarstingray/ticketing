import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import Guide from "./Guide";

describe("Guide page", () => {
  it("renders the bundled markdown as headings and prose", () => {
    render(<Guide />);
    // The markdown's H1 becomes a real heading element.
    expect(
      screen.getByRole("heading", { name: /Stingray Tickets/i, level: 1 })
    ).toBeInTheDocument();
    // A section that should always be present.
    expect(screen.getByRole("heading", { name: /Running the app/i })).toBeInTheDocument();
  });
});
