import { act, renderHook } from "@testing-library/react";
import { useFilteredOffset } from "@/lib/hooks";

describe("useFilteredOffset", () => {
  it("keeps the page while filters are unchanged and resets when they change", () => {
    const { result, rerender } = renderHook(({ a, b }) => useFilteredOffset(a, b), { initialProps: { a: "x", b: 1 } });
    expect(result.current[0]).toBe(0);
    act(() => result.current[1](40));
    expect(result.current[0]).toBe(40);
    rerender({ a: "x", b: 1 });
    expect(result.current[0]).toBe(40);
    rerender({ a: "y", b: 1 });
    expect(result.current[0]).toBe(0);
  });
});
