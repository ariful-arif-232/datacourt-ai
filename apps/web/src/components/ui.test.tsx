import { render, screen } from "@testing-library/react";
import { colorFor, SERIES } from "@/components/charts";
import { Button, EmptyState, KindTag, Notice, VerdictBadge } from "@/components/ui";

describe("evidence and status UI", () => {
  it("labels every evidence type in words, not colour alone", () => {
    const kinds = { measured: "Measured", heuristic: "Heuristic", model_prediction: "Model prediction", model_estimate: "Model estimate", llm: "LLM explanation", human: "Human decision" };
    for (const [kind, label] of Object.entries(kinds)) {
      const { unmount } = render(<KindTag kind={kind} />);
      expect(screen.getByText(label)).toBeInTheDocument();
      unmount();
    }
  });

  it("shows verdicts as readable text", () => {
    render(<VerdictBadge verdict="POSSIBLE_RELABEL" />);
    expect(screen.getByText(/relabel/i)).toBeInTheDocument();
  });

  it("opens external links safely and internal links in-app", () => {
    render(
      <>
        <Button href="https://github.com/x/y" target="_blank">
          GitHub
        </Button>
        <Button href="/docs">Docs</Button>
      </>,
    );
    const ext = screen.getByText("GitHub").closest("a")!;
    expect(ext).toHaveAttribute("target", "_blank");
    expect(ext.getAttribute("rel")).toContain("noopener");
    expect(screen.getByText("Docs").closest("a")).toHaveAttribute("href", "/docs");
  });

  it("renders empty states and notices", () => {
    render(
      <>
        <EmptyState title="No exports yet" />
        <Notice tone="warn">2 disputed cases</Notice>
      </>,
    );
    expect(screen.getByText("No exports yet")).toBeInTheDocument();
    expect(screen.getByText("2 disputed cases")).toBeInTheDocument();
  });
});

describe("categorical colours", () => {
  it("assign slots by fixed order and fold extras into Other", () => {
    const order = Array.from({ length: 10 }, (_, i) => `c${i}`);
    expect(colorFor("c0", order)).toBe(SERIES[0]);
    expect(colorFor("c7", order)).toBe(SERIES[7]);
    expect(colorFor("c8", order)).toBe("var(--series-other)");
    expect(colorFor("missing", order)).toBe("var(--series-other)");
  });
});
