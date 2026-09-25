import { bytes, duration, fixed, humanize, pct, signed, titleCase } from "@/lib/format";

describe("format helpers", () => {
  it("renders missing values as an em dash, never as NaN", () => {
    for (const f of [pct, fixed, signed]) {
      expect(f(null)).toBe("—");
      expect(f(undefined)).toBe("—");
      expect(f(Number.NaN)).toBe("—");
    }
    expect(bytes(undefined)).toBe("—");
    expect(duration(null)).toBe("—");
    expect(humanize("")).toBe("—");
  });

  it("formats percentages, signed deltas and fixed decimals", () => {
    expect(pct(0.9386, 1)).toBe("93.9%");
    expect(signed(0.03072)).toBe("+0.031");
    expect(signed(-0.00361)).toBe("-0.004");
    expect(signed(0)).toBe("0.000");
    expect(fixed(0.7435, 2)).toBe("0.74");
  });

  it("formats byte sizes and durations", () => {
    expect(bytes(0)).toBe("0 B");
    expect(bytes(1536)).toBe("1.5 KB");
    expect(bytes(15 * 1024 * 1024)).toBe("15 MB");
    expect(duration(47.8)).toBe("48s");
    expect(duration(125)).toBe("2m 5s");
  });

  it("humanizes enum-style identifiers", () => {
    expect(humanize("POSSIBLE_RELABEL")).toBe("Possible relabel");
    expect(titleCase("rare_or_wrong")).toBe("Rare Or Wrong");
  });
});
