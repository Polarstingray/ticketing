import { fireEvent, render, screen } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { StatusBadge, PriorityBadge, StatusDropdown, TypeBadge } from "./Badges";

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

describe("StatusDropdown", () => {
  it("reports the picked status and closes", () => {
    const onChange = vi.fn();
    render(<StatusDropdown status="open" onChange={onChange} />);

    fireEvent.click(screen.getByRole("button", { name: /Change status/i }));
    fireEvent.click(screen.getByRole("option", { name: "Closed" }));

    expect(onChange).toHaveBeenCalledWith("closed");
    expect(screen.queryByRole("option")).not.toBeInTheDocument();
  });

  it("does not fire a no-op change when the current status is re-picked", () => {
    const onChange = vi.fn();
    render(<StatusDropdown status="open" onChange={onChange} />);

    fireEvent.click(screen.getByRole("button", { name: /Change status/i }));
    fireEvent.click(screen.getByRole("option", { name: "Open" }));

    expect(onChange).not.toHaveBeenCalled();
  });

  it("closes on Escape", () => {
    render(<StatusDropdown status="open" onChange={vi.fn()} />);

    fireEvent.click(screen.getByRole("button", { name: /Change status/i }));
    fireEvent.keyDown(document, { key: "Escape" });

    expect(screen.queryByRole("option")).not.toBeInTheDocument();
  });
});
