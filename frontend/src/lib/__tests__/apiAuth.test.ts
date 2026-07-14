import { getApiAuthKey, setApiAuthKey, authHeaders, withAuthTicket } from "../apiAuth";

describe("apiAuth", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  describe("getApiAuthKey", () => {
    it("returns empty string when nothing stored", () => {
      expect(getApiAuthKey()).toBe("");
    });
    it("returns stored key", () => {
      localStorage.setItem("vibe_trading_api_auth_key", "my-secret");
      expect(getApiAuthKey()).toBe("my-secret");
    });
  });

  describe("setApiAuthKey", () => {
    it("stores trimmed value", () => {
      setApiAuthKey("  abc-123  ");
      expect(localStorage.getItem("vibe_trading_api_auth_key")).toBe("abc-123");
    });
    it("removes key when value is empty/whitespace", () => {
      setApiAuthKey("abc");
      setApiAuthKey("   ");
      expect(localStorage.getItem("vibe_trading_api_auth_key")).toBeNull();
    });
    it("removes key when value is empty string", () => {
      setApiAuthKey("abc");
      setApiAuthKey("");
      expect(localStorage.getItem("vibe_trading_api_auth_key")).toBeNull();
    });
  });

  describe("authHeaders", () => {
    it("returns empty object when no key set", () => {
      expect(authHeaders()).toEqual({});
    });
    it("returns Bearer header when key exists", () => {
      setApiAuthKey("token-xyz");
      expect(authHeaders()).toEqual({ Authorization: "Bearer token-xyz" });
    });
  });

  describe("withAuthTicket", () => {
    it("mints a ticket with the bearer header and never exposes the API key in the stream URL", async () => {
      setApiAuthKey("key with spaces");
      const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ ticket: "one-time-ticket" }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }));
      vi.stubGlobal("fetch", fetchMock);

      await expect(withAuthTicket("https://api.com/data?foo=bar")).resolves.toBe(
        "https://api.com/data?foo=bar&ticket=one-time-ticket",
      );
      expect(fetchMock).toHaveBeenCalledWith("/auth/sse-ticket", {
        method: "POST",
        headers: { Authorization: "Bearer key with spaces" },
      });
      expect(fetchMock.mock.calls[0][0]).not.toContain("api_key");
    });

    it("returns the original URL without minting a ticket when no API key is configured", async () => {
      const fetchMock = vi.fn();
      vi.stubGlobal("fetch", fetchMock);

      await expect(withAuthTicket("https://api.com/data")).resolves.toBe("https://api.com/data");
      expect(fetchMock).not.toHaveBeenCalled();
    });

    it("rejects rather than opening an unauthenticated stream when ticket minting fails", async () => {
      setApiAuthKey("remote-key");
      vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("denied", { status: 401 })));

      await expect(withAuthTicket("https://api.com/events")).rejects.toThrow("SSE ticket");
    });
  });
});
