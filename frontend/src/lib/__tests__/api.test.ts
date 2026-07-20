import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api";

const TRACK_IDS = ["ai", "semi", "robot", "auto", "energy", "bio", "space", "security", "tech", "consumer", "macro", "science"];

const runningStatus = {
  state: "fetching",
  task_id: "00000000-0000-4000-8000-000000000001",
  started_at: "2026-07-20T10:00:00Z",
  completed_at: null,
  processed_endpoints: 1,
  successful_endpoints: 1,
  failed_endpoints: 0,
  processed_tracks: 0,
  total_endpoints: 106,
  total_tracks: 12,
  error: null,
};

function validNewsSnapshot() {
  return {
    available: true,
    stale: false,
    snapshot: {
      schema_version: 1,
      generated_at: "2026-07-20T10:00:00Z",
      upstream_commit: "d98aa603228f4839fb48859812c63a58ca10cead",
      source_stats: {
        endpoint_success_count: 1,
        endpoint_failure_count: 0,
        assignment_success_count: 1,
        assignment_failure_count: 0,
      },
      errors: [],
      tracks: TRACK_IDS.map((track_id) => ({
        track_id,
        state: "fresh",
        generated_at: "2026-07-20T10:00:00Z",
        stale: false,
        partial: false,
        items: [{
          id: `${track_id}-article`,
          track_id,
          title: `${track_id} headline`,
          title_zh: null,
          summary: null,
          source: { id: "source", name: "News source", url: "https://example.com/feed" },
          published_at: "2026-07-20T09:00:00Z",
          url: "https://example.com/article",
        }],
        ai: {
          available: true,
          generated_at: "2026-07-20T10:00:00Z",
          highlights: ["One", "Two", "Three"],
          error: null,
        },
        source_stats: {
          endpoint_success_count: 1,
          endpoint_failure_count: 0,
          assignment_success_count: 1,
          assignment_failure_count: 0,
        },
      })),
    },
    refresh: { ...runningStatus },
    error: null,
  };
}

function mockFetchJson(body: unknown) {
  const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(body), {
    status: 200,
    headers: { "content-type": "application/json" },
  }));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

async function loadApiModule() {
  vi.resetModules();
  return import("../api");
}

describe("api request helper", () => {
  beforeEach(() => {
    vi.stubGlobal("localStorage", {
      getItem: vi.fn(() => ""),
      setItem: vi.fn(),
      removeItem: vi.fn(),
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.resetModules();
  });

  it("rejects non-JSON responses with a descriptive error", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response("<!doctype html><html><body>SPA</body></html>", {
          status: 200,
          headers: { "content-type": "text/html" },
        }),
      ),
    );

    const { api } = await loadApiModule();

    await expect(api.getChannelStatus()).rejects.toMatchObject({
      name: "ApiError",
      status: 200,
      message: expect.stringContaining("Expected JSON from /channels/status, got text/html"),
    } satisfies Partial<ApiError>);
  });

  it("wraps malformed JSON responses in ApiError", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response("{\"status\": true", {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      ),
    );

    const { api } = await loadApiModule();

    await expect(api.getChannelStatus()).rejects.toMatchObject({
      name: "ApiError",
      status: 200,
      message: "Invalid JSON response from /channels/status",
    } satisfies Partial<ApiError>);
  });

  it("preserves a successful refresh response status for malformed JSON", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response("{\"task_id\":", {
          status: 202,
          headers: { "content-type": "application/json" },
        }),
      ),
    );

    const { api } = await loadApiModule();

    await expect(api.startNewsRefresh()).rejects.toMatchObject({ name: "ApiError", status: 202 });
  });

  it("starts news refresh through the authenticated json client", async () => {
    const fetchMock = mockFetchJson({
      task_id: "00000000-0000-4000-8000-000000000001",
      reused: false,
      status: runningStatus,
    });
    window.localStorage.getItem = vi.fn(() => "news-api-key");
    const signal = new AbortController().signal;
    const { api } = await loadApiModule();

    await api.startNewsRefresh(signal);

    expect(fetchMock).toHaveBeenCalledWith("/news-api/refresh", expect.objectContaining({
      method: "POST",
      signal,
      headers: expect.objectContaining({ Authorization: "Bearer news-api-key" }),
    }));
  });

  it("parses a complete news snapshot response", async () => {
    const { parseNewsSnapshotResponse } = await loadApiModule();

    expect(parseNewsSnapshotResponse(validNewsSnapshot())).toMatchObject({
      available: true,
      snapshot: { tracks: expect.arrayContaining([expect.objectContaining({ track_id: "ai" })]) },
    });
  });

  it("accepts a UUID permitted by the backend status model", async () => {
    const payload = validNewsSnapshot();
    payload.refresh.task_id = "00000000-0000-0000-0000-000000000000";
    const { parseNewsSnapshotResponse } = await loadApiModule();

    expect(parseNewsSnapshotResponse(payload).refresh.task_id).toBe(payload.refresh.task_id);
  });

  it("rejects a snapshot with missing canonical tracks", async () => {
    const payload = validNewsSnapshot();
    payload.snapshot.tracks.pop();
    const { parseNewsSnapshotResponse } = await loadApiModule();

    expect(() => parseNewsSnapshotResponse(payload)).toThrow(expect.objectContaining({ name: "ApiError", status: 200 }));
  });

  it("rejects a refresh response with an unknown phase", async () => {
    const payload = validNewsSnapshot();
    payload.refresh.state = "mystery";
    mockFetchJson(payload);
    const { api } = await loadApiModule();

    await expect(api.getNewsSnapshot()).rejects.toMatchObject({ name: "ApiError", status: 200 });
  });

  it("rejects article URLs outside HTTP(S)", async () => {
    const payload = validNewsSnapshot();
    payload.snapshot.tracks[0].items[0].url = "javascript:alert(1)";
    const { parseNewsSnapshotResponse } = await loadApiModule();

    expect(() => parseNewsSnapshotResponse(payload)).toThrow(expect.objectContaining({ name: "ApiError", status: 200 }));
  });

  it("rejects article URLs without an explicit HTTP(S) authority", async () => {
    const payload = validNewsSnapshot();
    payload.snapshot.tracks[0].items[0].url = "https:example.com/article";
    const { parseNewsSnapshotResponse } = await loadApiModule();

    expect(() => parseNewsSnapshotResponse(payload)).toThrow(expect.objectContaining({ name: "ApiError", status: 200 }));
  });

  it.each([
    "https://example.com/article?access_token=secret-value",
    "https://example.com/article#authorization=Bearer%20secret-value",
    "https://example.com/article?continue=sk-secret-value",
    "https://example.com/article?session=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signature",
  ])("rejects article URLs with credential material: %s", async (url) => {
    const payload = validNewsSnapshot();
    payload.snapshot.tracks[0].items[0].url = url;
    const { parseNewsSnapshotResponse } = await loadApiModule();

    expect(() => parseNewsSnapshotResponse(payload)).toThrow(expect.objectContaining({ name: "ApiError", status: 200 }));
  });

  it.each([
    "https://example.com/article?next=%ZZ",
    "https://example.com:99999/article",
  ])("accepts article URLs permitted by the backend: %s", async (url) => {
    const payload = validNewsSnapshot();
    payload.snapshot.tracks[0].items[0].url = url;
    const { parseNewsSnapshotResponse } = await loadApiModule();

    expect(parseNewsSnapshotResponse(payload).snapshot?.tracks[0].items[0].url).toBe(url);
  });

  it("rejects article titles longer than the backend limit", async () => {
    const payload = validNewsSnapshot();
    payload.snapshot.tracks[0].items[0].title = "x".repeat(301);
    const { parseNewsSnapshotResponse } = await loadApiModule();

    expect(() => parseNewsSnapshotResponse(payload)).toThrow(expect.objectContaining({ name: "ApiError", status: 200 }));
  });

  it.each([
    "2026-02-30T10:00:00Z",
    "0000-01-01T10:00:00Z",
  ])("rejects invalid UTC calendar dates: %s", async (generatedAt) => {
    const payload = validNewsSnapshot();
    payload.snapshot.generated_at = generatedAt;
    const { parseNewsSnapshotResponse } = await loadApiModule();

    expect(() => parseNewsSnapshotResponse(payload)).toThrow(expect.objectContaining({ name: "ApiError", status: 200 }));
  });

  it("accepts article titles within the backend code-point limit", async () => {
    const payload = validNewsSnapshot();
    payload.snapshot.tracks[0].items[0].title = "😀".repeat(151);
    const { parseNewsSnapshotResponse } = await loadApiModule();

    expect(parseNewsSnapshotResponse(payload).snapshot?.tracks[0].items[0].title).toBe(payload.snapshot.tracks[0].items[0].title);
  });

  it.each([
    "https://@example.com/article",
    "https://:@example.com/article",
  ])("rejects article URLs with empty userinfo: %s", async (url) => {
    const payload = validNewsSnapshot();
    payload.snapshot.tracks[0].items[0].url = url;
    const { parseNewsSnapshotResponse } = await loadApiModule();

    expect(() => parseNewsSnapshotResponse(payload)).toThrow(expect.objectContaining({ name: "ApiError", status: 200 }));
  });
});
