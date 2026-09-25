import { finalizeUpload, uploadArchive, type UploadPlan } from "@/lib/upload";

type Sent = { url: string; size: number; headers: Record<string, string> };

/** Minimal XMLHttpRequest stand-in: answers PUTs from a per-URL script, reports upload progress. */
function installFakeXhr(respond: (url: string, attempt: number) => number) {
  const sent: Sent[] = [];
  const attempts = new Map<string, number>();
  let inFlight = 0;
  let maxInFlight = 0;
  class FakeXhr {
    status = 0;
    url = "";
    headers: Record<string, string> = {};
    upload: { onprogress: ((e: { lengthComputable: boolean; loaded: number }) => void) | null } = { onprogress: null };
    onload: (() => void) | null = null;
    onerror: (() => void) | null = null;
    onabort: (() => void) | null = null;
    private timer: ReturnType<typeof setTimeout> | null = null;
    open(_method: string, url: string) {
      this.url = url;
    }
    setRequestHeader(k: string, v: string) {
      this.headers[k] = v;
    }
    send(body: Blob) {
      const n = (attempts.get(this.url) ?? 0) + 1;
      attempts.set(this.url, n);
      inFlight++;
      maxInFlight = Math.max(maxInFlight, inFlight);
      this.timer = setTimeout(() => {
        inFlight--;
        this.status = respond(this.url, n);
        if (this.status === 0) return this.onerror?.();
        sent.push({ url: this.url, size: body.size, headers: this.headers });
        this.upload.onprogress?.({ lengthComputable: true, loaded: body.size });
        this.onload?.();
      }, 5);
    }
    abort() {
      if (this.timer) clearTimeout(this.timer);
      inFlight--;
      this.onabort?.();
    }
  }
  vi.stubGlobal("XMLHttpRequest", FakeXhr);
  return { sent, maxInFlight: () => maxInFlight };
}

const file = (size: number) => new File([new Uint8Array(size)], "data.zip", { type: "application/zip" });

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("uploadArchive", () => {
  it("sends a small archive with one presigned PUT", async () => {
    const xhr = installFakeXhr(() => 200);
    const progress: number[] = [];
    const plan: UploadPlan = { mode: "single", url: "https://bucket/put", method: "PUT", headers: { "Content-Type": "application/zip" }, expires_in: 60 };
    await uploadArchive("v1", file(1000), plan, (p) => progress.push(p));
    expect(xhr.sent).toEqual([{ url: "https://bucket/put", size: 1000, headers: { "Content-Type": "application/zip" } }]);
    expect(progress.at(-1)).toBe(1);
  });

  it("uploads parts in parallel, retries a part whose URL expired with a fresh URL", async () => {
    const xhr = installFakeXhr((url, attempt) => (url === "https://bucket/p2" && attempt === 1 ? 403 : 200));
    const fetch = vi.fn(async () => new Response(JSON.stringify({ parts: [{ part_number: 2, url: "https://bucket/p2" }] }), { status: 200 }));
    vi.stubGlobal("fetch", fetch);
    const parts = [1, 2, 3, 4, 5].map((n) => ({ part_number: n, url: `https://bucket/p${n}` }));
    const plan: UploadPlan = { mode: "multipart", part_bytes: 100, parts, expires_in: 60 };
    const progress: number[] = [];
    vi.useFakeTimers({ shouldAdvanceTime: true });
    await uploadArchive("v1", file(450), plan, (p) => progress.push(p));
    const sizes = Object.fromEntries(xhr.sent.map((s) => [s.url, s.size]));
    expect(sizes).toEqual({ "https://bucket/p1": 100, "https://bucket/p2": 100, "https://bucket/p3": 100, "https://bucket/p4": 100, "https://bucket/p5": 50 });
    expect(xhr.maxInFlight()).toBeLessThanOrEqual(3);
    const [url, init] = fetch.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/v1/versions/v1/upload/parts");
    expect(JSON.parse(String(init.body))).toEqual({ part_numbers: [2] });
    expect(progress.at(-1)).toBe(1);
  });

  it("stops when cancelled", async () => {
    installFakeXhr(() => 200);
    const ctl = new AbortController();
    const plan: UploadPlan = { mode: "multipart", part_bytes: 100, parts: [{ part_number: 1, url: "https://bucket/p1" }], expires_in: 60 };
    const pending = uploadArchive("v1", file(100), plan, () => {}, ctl.signal);
    ctl.abort();
    await expect(pending).rejects.toMatchObject({ name: "AbortError" });
  });
});

describe("finalizeUpload", () => {
  it("re-sends parts the server did not receive, then finalizes", async () => {
    const xhr = installFakeXhr(() => 200);
    const responses = [
      new Response(JSON.stringify({ detail: "1 of 2 parts have not been uploaded yet", missing_parts: [2] }), { status: 409 }),
      new Response(JSON.stringify({ parts: [{ part_number: 2, url: "https://bucket/p2-fresh" }] }), { status: 200 }),
      new Response(JSON.stringify({ version: { id: "v1", status: "uploaded" } }), { status: 200 }),
    ];
    vi.stubGlobal("fetch", vi.fn(async () => responses.shift()!));
    const plan: UploadPlan = {
      mode: "multipart",
      part_bytes: 100,
      parts: [
        { part_number: 1, url: "https://bucket/p1" },
        { part_number: 2, url: "https://bucket/p2" },
      ],
      expires_in: 60,
    };
    const done = await finalizeUpload("v1", file(150), plan);
    expect(done.version.status).toBe("uploaded");
    expect(xhr.sent).toEqual([{ url: "https://bucket/p2-fresh", size: 50, headers: {} }]);
  });
});
