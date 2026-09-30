// Jarvis failover front door.
//
// Public address: app.dj-ai.org (bind this Worker to that custom domain).
// It tries the home server first (jarvis.dj-ai.org, the direct Cloudflare
// Tunnel address) and falls back to Render if the server is down.
//
// Deliberately does NOT just time out a slow request and assume the
// server's dead -- a real Jarvis reply can take up to ~90s (the AI
// thinking), and bouncing that to Render mid-answer would be wrong. So it
// does a small, separate health check first (a cheap static file, not the
// real request) to decide which backend to use, then sends the actual
// request there with no artificial time limit.

const PRIMARY_ORIGIN = "https://jarvis.dj-ai.org";
const FALLBACK_ORIGIN = "https://jarvis-phone-sxs6.onrender.com";
const HEALTH_CHECK_PATH = "/manifest.json"; // static, cheap, no AI involved
const HEALTH_CHECK_TIMEOUT_MS = 2500;

async function isHealthy(origin) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), HEALTH_CHECK_TIMEOUT_MS);
  try {
    const res = await fetch(origin + HEALTH_CHECK_PATH, {
      method: "GET",
      signal: controller.signal,
    });
    return res.ok;
  } catch (err) {
    return false;
  } finally {
    clearTimeout(timeout);
  }
}

async function forward(origin, request, url) {
  const headers = new Headers(request.headers);
  headers.delete("host"); // let fetch() set the right one for the target origin

  const init = { method: request.method, headers, redirect: "manual" };
  if (!["GET", "HEAD"].includes(request.method)) {
    init.body = await request.clone().arrayBuffer();
  }
  return fetch(origin + url.pathname + url.search, init);
}

export default {
  async fetch(request) {
    const url = new URL(request.url);
    const primaryUp = await isHealthy(PRIMARY_ORIGIN);
    const chosen = primaryUp ? PRIMARY_ORIGIN : FALLBACK_ORIGIN;

    try {
      return await forward(chosen, request, url);
    } catch (err) {
      // Health check passed but the real request still failed (rare) --
      // one more try against the other side rather than failing outright.
      const other = chosen === PRIMARY_ORIGIN ? FALLBACK_ORIGIN : PRIMARY_ORIGIN;
      try {
        return await forward(other, request, url);
      } catch (err2) {
        return new Response(
          JSON.stringify({ error: "Both the home server and the backup are unreachable." }),
          { status: 502, headers: { "Content-Type": "application/json" } }
        );
      }
    }
  },
};
