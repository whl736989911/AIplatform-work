import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("./config", () => ({
  getApiUrl: (path: string) => `/api${path}`,
}));

vi.mock("../i18n", () => ({
  default: { language: "zh" },
}));

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

/**
 * A 401 is no longer the end of the session when the browser still holds a
 * refresh token. These tests pin the two properties that make that safe: one
 * renewal per burst (the server rotates, and a second use of the same token looks
 * like a replay — two racing renewals would sign the user out), and never more
 * than one attempt per request (a token that is refused again is a real 401).
 */
describe("session renewal on 401", () => {
  const replace = vi.fn();
  let mod: typeof import("./request");
  let calls: string[];

  beforeEach(async () => {
    vi.resetModules();
    localStorage.clear();
    replace.mockClear();
    calls = [];
    Object.defineProperty(window, "location", {
      configurable: true,
      value: { pathname: "/chat", replace },
    });
    mod = await import("./request");
    mod.setAuthToken("expired-token");
    mod.setRefreshToken("refresh-1");
  });

  function stubFetch(handler: (url: string) => Response | Promise<Response>) {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: unknown) => {
        const url = String(input);
        calls.push(url);
        return handler(url);
      }),
    );
  }

  it("renews once and retries the original request with the new token", async () => {
    let renewed = false;
    stubFetch((url) => {
      if (url.endsWith("/auth/refresh")) {
        renewed = true;
        return json(200, { access_token: "fresh-access", refresh_token: "refresh-2" });
      }
      return renewed ? json(200, { ok: true }) : json(401, {});
    });

    await expect(mod.request<{ ok: boolean }>("/agents")).resolves.toEqual({ ok: true });

    expect(calls.filter((url) => url.endsWith("/auth/refresh"))).toHaveLength(1);
    expect(mod.getAuthToken()).toBe("fresh-access");
    expect(mod.getRefreshToken()).toBe("refresh-2");
  });

  it("shares one renewal between requests that fail together", async () => {
    let renewed = false;
    let renewals = 0;
    let started = 0;
    // Hold the renewal until every request has reached its 401, so the test proves
    // they *share* the exchange rather than merely finishing fast.
    const allStarted = Promise.withResolvers<void>();
    stubFetch(async (url) => {
      if (url.endsWith("/auth/refresh")) {
        renewals += 1;
        await allStarted.promise;
        renewed = true;
        return json(200, { access_token: "fresh-access", refresh_token: "refresh-2" });
      }
      started += 1;
      if (started === 3) {
        allStarted.resolve();
      }
      return renewed ? json(200, {}) : json(401, {});
    });

    await Promise.allSettled([mod.request("/agents"), mod.request("/skills"), mod.request("/cron")]);

    expect(renewals).toBe(1);
  });

  it("gives up when the renewal itself is refused", async () => {
    let renewals = 0;
    stubFetch((url) => {
      if (url.endsWith("/auth/refresh")) {
        renewals += 1;
        return json(401, {});
      }
      return json(401, {});
    });

    await expect(mod.request("/agents")).rejects.toThrow();

    // Exactly one attempt, and the dead session is cleared.
    expect(renewals).toBe(1);
    expect(mod.getRefreshToken()).toBe("");
    expect(replace).toHaveBeenCalledWith("/login");
  });

  it("does not renew again when the fresh token is refused too", async () => {
    let renewals = 0;
    let renewed = false;
    stubFetch((url) => {
      if (url.endsWith("/auth/refresh")) {
        renewals += 1;
        renewed = true;
        return json(200, { access_token: "fresh-access", refresh_token: "refresh-2" });
      }
      return renewed ? json(401, {}) : json(401, {});
    });

    await expect(mod.request("/agents")).rejects.toThrow();

    expect(renewals).toBe(1);
  });
});
