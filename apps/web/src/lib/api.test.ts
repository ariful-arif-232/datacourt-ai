import { api, ApiError, qs } from "@/lib/api";

function mockFetch(status: number, body: unknown) {
  const fn = vi.fn(async () => new Response(body === undefined ? "" : JSON.stringify(body), { status }));
  vi.stubGlobal("fetch", fn);
  return fn;
}

afterEach(() => vi.unstubAllGlobals());

describe("api client", () => {
  it("sends the CSRF header and JSON body on state-changing requests", async () => {
    const fetch = mockFetch(200, { ok: true });
    await api("/cases/1/decision", { json: { action: "keep" } });
    const [url, init] = fetch.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/v1/cases/1/decision");
    expect(init.method).toBe("POST");
    expect(init.credentials).toBe("include");
    expect((init.headers as Record<string, string>)["x-datacourt-csrf"]).toBe("1");
    expect(init.body).toBe(JSON.stringify({ action: "keep" }));
  });

  it("does not send the CSRF header on GET", async () => {
    const fetch = mockFetch(200, []);
    await api("/versions/1/cases");
    const [, init] = fetch.mock.calls[0] as unknown as [string, RequestInit];
    expect(init.method).toBe("GET");
    expect((init.headers as Record<string, string>)["x-datacourt-csrf"]).toBeUndefined();
  });

  it("surfaces the API's error detail with the status code", async () => {
    mockFetch(409, { detail: "export not ready" });
    const err = await api("/exports/1/download").catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(409);
    expect(err.message).toBe("export not ready");
  });

  it("uses a generic message for validation errors", async () => {
    mockFetch(422, { detail: [{ loc: ["body", "email"], msg: "bad" }] });
    await expect(api("/auth/register", { json: {} })).rejects.toThrow("Some fields are invalid.");
  });
});

describe("qs", () => {
  it("drops empty values and repeats arrays", () => {
    expect(qs({ a: "x", b: "", c: null, d: undefined, e: 0, f: ["1", "2"] })).toBe("?a=x&e=0&f=1&f=2");
    expect(qs({ a: undefined })).toBe("");
  });
});
