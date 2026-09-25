import { fireEvent, render as rtlRender, screen, waitFor } from "@testing-library/react";
import type { ReactElement } from "react";
import { SWRConfig } from "swr";
import { AuthForm } from "@/components/auth-form";

// A fresh SWR cache per test, so the backend health probe runs against each test's fetch mock.
const render = (ui: ReactElement) => rtlRender(<SWRConfig value={{ provider: () => new Map() }}>{ui}</SWRConfig>);

const push = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push, replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams("next=/app/projects/p1"),
  usePathname: () => "/login",
}));

afterEach(() => {
  vi.unstubAllGlobals();
  push.mockReset();
});

function fill(label: string, value: string) {
  fireEvent.change(screen.getByLabelText(label), { target: { value } });
}

describe("AuthForm", () => {
  it("signs in and returns to the requested page", async () => {
    const fetch = vi.fn(async () => new Response(JSON.stringify({ user: {}, organizations: [] }), { status: 200 }));
    vi.stubGlobal("fetch", fetch);
    render(<AuthForm mode="login" />);
    fill("Email", "a@example.com");
    fill("Password", "correct-horse-battery");
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    await waitFor(() => expect(push).toHaveBeenCalledWith("/app/projects/p1"));
    const [url, init] = fetch.mock.calls.find((c: unknown[]) => c[0] === "/api/v1/auth/login") as unknown as [string, RequestInit];
    expect(url).toBe("/api/v1/auth/login");
    expect(JSON.parse(String(init.body))).toEqual({ email: "a@example.com", password: "correct-horse-battery" });
  });

  it("shows the server's error and stays on the page", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ detail: "invalid email or password" }), { status: 401 })));
    render(<AuthForm mode="login" />);
    fill("Email", "a@example.com");
    fill("Password", "wrong");
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("invalid email or password");
    expect(push).not.toHaveBeenCalled();
  });

  it("asks for a name and a 10-character password when registering", () => {
    render(<AuthForm mode="register" />);
    expect(screen.getByLabelText("Your name")).toBeRequired();
    expect(screen.getByLabelText(/^Password/)).toHaveAttribute("minLength", "10");
  });
});

describe("AuthForm without a working backend", () => {
  it("explains that the API is not configured and disables sign-in", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(JSON.stringify({ ok: false, configured: false, problems: ["AUTH_SECRET"] }), { status: 503 })),
    );
    render(<AuthForm mode="login" />);
    expect(await screen.findByRole("status")).toHaveTextContent("not configured yet");
    expect(screen.getByRole("button", { name: "Sign in" })).toBeDisabled();
  });

  it("explains that the API is unreachable", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new TypeError("network down"); }));
    render(<AuthForm mode="register" />);
    expect(await screen.findByRole("status")).toHaveTextContent("not reachable");
  });
});
