import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import { StatusBadge, PriorityBadge, TypeBadge } from "./Badges";

describe("badges", () => {
  it("renders a human label for a known status", () => {
    render(<StatusBadge status="in_review" />);
    expect(screen.getByText("In Review")).toBeInTheDocument();
  });

  it("falls back to the raw value for an unknown status", () => {
    render(<StatusBadge status="weird" />);
    expect(screen.getByText("weird")).toBeInTheDocument();
  });

  it("renders priority and type labels", () => {
    render(
      <>
        <PriorityBadge priority="high" />
        <TypeBadge type="code_review" />
      </>
    );
    expect(screen.getByText("High")).toBeInTheDocument();
    expect(screen.getByText("Code Review")).toBeInTheDocument();
  });
});
