import type { NextConfig } from "next";

// The browser always talks to /api/* on the app origin, so session cookies stay first-party.
// On Vercel the root vercel.json routes /api/* to the API service of the same deployment. Elsewhere
// (local development, self-hosting, or an API deployed separately) Next.js proxies /api/* to
// API_ORIGIN, a server-side setting.
const apiOrigin = (process.env.API_ORIGIN || (process.env.VERCEL ? "" : "http://127.0.0.1:8000")).replace(/\/$/, "");

const securityHeaders = [
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  { key: "X-Frame-Options", value: "DENY" },
  { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=()" },
];

const nextConfig: NextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,
  async rewrites() {
    return apiOrigin ? [{ source: "/api/:path*", destination: `${apiOrigin}/api/:path*` }] : [];
  },
  async headers() {
    return [{ source: "/:path*", headers: securityHeaders }];
  },
};

export default nextConfig;
