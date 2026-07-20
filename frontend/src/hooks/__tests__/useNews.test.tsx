import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { NewsRefreshAccepted, NewsRefreshStatus, NewsSnapshotResponse } from "@/lib/api";
import { useNews } from "../useNews";

const { apiMock } = vi.hoisted(() => ({
  apiMock: {
    getNewsSnapshot: vi.fn(),
    startNewsRefresh: vi.fn(),
    getNewsRefreshStatus: vi.fn(),
  },
}));

vi.mock("@/lib/api", () => ({ api: apiMock }));

const runningStatus = (state: NewsRefreshStatus["state"] = "fetching"): NewsRefreshStatus => ({
  state,
  task_id: "00000000-0000-4000-8000-000000000001",
  started_at: "2026-07-20T10:00:00Z",
  completed_at: state === "succeeded" || state === "failed" || state === "cancelled" ? "2026-07-20T10:01:00Z" : null,
  processed_endpoints: 1,
  successful_endpoints: 1,
  failed_endpoints: 0,
  processed_tracks: 1,
  total_endpoints: 106,
  total_tracks: 12,
  error: state === "failed" ? { code: "upstream_failed", message: "news refresh failed" } : null,
});

function snapshotResponse(articleId: string, refresh = runningStatus("succeeded")): NewsSnapshotResponse {
  return {
    available: true,
    stale: false,
    snapshot: {
      schema_version: 1,
      generated_at: "2026-07-20T10:00:00Z",
      upstream_commit: "commit",
      source_stats: {
        endpoint_success_count: 1,
        endpoint_failure_count: 0,
        assignment_success_count: 1,
        assignment_failure_count: 0,
      },
      errors: [],
      tracks: ["ai", "semi", "robot", "auto", "energy", "bio", "space", "security", "tech", "consumer", "macro", "science"].map((track_id) => ({
        track_id: track_id as NewsSnapshotResponse["snapshot"] extends { tracks: (infer T)[] } ? T extends { track_id: infer I } ? I : never : never,
        state: "fresh",
        generated_at: "2026-07-20T10:00:00Z",
        stale: false,
        partial: false,
        items: [{
          id: `${articleId}-${track_id}`,
          track_id: track_id as NewsSnapshotResponse["snapshot"] extends { tracks: (infer T)[] } ? T extends { track_id: infer I } ? I : never : never,
          title: "Headline",
          title_zh: null,
          summary: null,
          source: { id: "source", name: "Source", url: "https://example.com/feed" },
          published_at: null,
          url: "https://example.com/article",
        }],
        ai: { available: false, generated_at: null, highlights: [], error: null },
        source_stats: {
          endpoint_success_count: 1,
          endpoint_failure_count: 0,
          assignment_success_count: 1,
          assignment_failure_count: 0,
        },
      })),
    },
    refresh,
    error: null,
  };
}

function refreshAccepted(reused: boolean, status = runningStatus()): NewsRefreshAccepted {
  return { task_id: status.task_id!, reused, status };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((next) => { resolve = next; });
  return { promise, resolve };
}

describe("useNews", () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("loads a snapshot once and polls only while the refresh is running", async () => {
    apiMock.getNewsSnapshot
      .mockResolvedValueOnce(snapshotResponse("before", runningStatus()))
      .mockResolvedValueOnce(snapshotResponse("after"));
    apiMock.getNewsRefreshStatus.mockResolvedValue(runningStatus("succeeded"));

    const { result } = renderHook(() => useNews());

    await waitFor(() => expect(apiMock.getNewsSnapshot).toHaveBeenCalledTimes(1));
    expect(apiMock.getNewsRefreshStatus).not.toHaveBeenCalled();
    expect(result.current.selectedTrackId).toBe("ai");

    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });

    expect(apiMock.getNewsRefreshStatus).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(apiMock.getNewsSnapshot).toHaveBeenCalledTimes(2));
    expect(result.current.snapshot?.tracks[0].items[0].id).toBe("after-ai");
  });

  it("loads the initial snapshot only once under StrictMode", async () => {
    apiMock.getNewsSnapshot.mockResolvedValue(snapshotResponse("initial"));

    const { result } = renderHook(() => useNews(), { reactStrictMode: true });

    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(apiMock.getNewsSnapshot).toHaveBeenCalledTimes(1);
    expect(result.current.snapshot?.tracks[0].items[0].id).toBe("initial-ai");
  });

  it.each([false, true])("starts a %s reused refresh and polls its returned running status", async (reused) => {
    apiMock.getNewsSnapshot.mockResolvedValue(snapshotResponse("before"));
    apiMock.startNewsRefresh.mockResolvedValue(refreshAccepted(reused));
    apiMock.getNewsRefreshStatus.mockResolvedValue(runningStatus("cancelled"));

    const { result } = renderHook(() => useNews());
    await waitFor(() => expect(apiMock.getNewsSnapshot).toHaveBeenCalledTimes(1));

    await act(async () => { await result.current.refreshNews(); });

    expect(apiMock.startNewsRefresh).toHaveBeenCalledWith(expect.any(AbortSignal));
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(apiMock.getNewsRefreshStatus).toHaveBeenCalledTimes(1);
  });

  it("keeps the existing snapshot when refresh polling fails", async () => {
    apiMock.getNewsSnapshot.mockResolvedValue(snapshotResponse("before", runningStatus()));
    apiMock.getNewsRefreshStatus.mockResolvedValue(runningStatus("failed"));

    const { result } = renderHook(() => useNews());
    await waitFor(() => expect(apiMock.getNewsSnapshot).toHaveBeenCalledTimes(1));

    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });

    expect(result.current.refreshStatus?.state).toBe("failed");
    expect(result.current.snapshot?.tracks[0].items[0].id).toBe("before-ai");
    expect(apiMock.getNewsSnapshot).toHaveBeenCalledTimes(1);
  });

  it("does not apply a stale snapshot that resolves while a refresh is starting", async () => {
    const pendingSnapshot = deferred<NewsSnapshotResponse>();
    let snapshotSignal: AbortSignal | undefined;
    apiMock.getNewsSnapshot.mockImplementation((signal?: AbortSignal) => {
      snapshotSignal = signal;
      return pendingSnapshot.promise;
    });
    apiMock.startNewsRefresh.mockResolvedValue(refreshAccepted(false));

    const { result } = renderHook(() => useNews());
    await waitFor(() => expect(apiMock.getNewsSnapshot).toHaveBeenCalledTimes(1));

    await act(async () => { await result.current.refreshNews(); });
    expect(snapshotSignal?.aborted).toBe(true);

    await act(async () => { pendingSnapshot.resolve(snapshotResponse("stale")); });

    expect(result.current.snapshot).toBeNull();
    expect(result.current.refreshStatus?.state).toBe("fetching");
    expect(result.current.isRefreshing).toBe(true);
  });

  it.each([
    ["failed", () => Promise.resolve(refreshAccepted(false, runningStatus("failed")))],
    ["cancelled", () => Promise.resolve(refreshAccepted(false, runningStatus("cancelled")))],
    ["rejected", () => Promise.reject(new Error("refresh unavailable"))],
  ])("settles initial loading when refresh is %s before its snapshot resolves", async (_outcome, refreshResult) => {
    const pendingSnapshot = deferred<NewsSnapshotResponse>();
    apiMock.getNewsSnapshot.mockReturnValue(pendingSnapshot.promise);
    apiMock.startNewsRefresh.mockImplementation(refreshResult);

    const { result } = renderHook(() => useNews());
    await waitFor(() => expect(apiMock.getNewsSnapshot).toHaveBeenCalledTimes(1));

    await act(async () => { await result.current.refreshNews(); });
    await act(async () => { pendingSnapshot.resolve(snapshotResponse("stale")); });

    expect(result.current.snapshot).toBeNull();
    expect(result.current.isLoading).toBe(false);
  });

  it("aborts distinct snapshot, refresh, and status signals during lifecycle cleanup", async () => {
    const pendingSnapshot = deferred<NewsSnapshotResponse>();
    let refreshSignal: AbortSignal | undefined;
    let snapshotSignal: AbortSignal | undefined;
    let statusSignal: AbortSignal | undefined;
    const pendingRefresh = deferred<NewsRefreshAccepted>();
    apiMock.getNewsSnapshot
      .mockResolvedValueOnce(snapshotResponse("before", runningStatus()))
      .mockImplementationOnce((signal?: AbortSignal) => {
        snapshotSignal = signal;
        return pendingSnapshot.promise;
      });
    apiMock.getNewsRefreshStatus.mockImplementation((signal?: AbortSignal) => {
      statusSignal = signal;
      return Promise.resolve(runningStatus("succeeded"));
    });
    apiMock.startNewsRefresh.mockImplementation((signal?: AbortSignal) => {
      refreshSignal = signal;
      return pendingRefresh.promise;
    });

    const { result, unmount } = renderHook(() => useNews());
    await waitFor(() => expect(apiMock.getNewsSnapshot).toHaveBeenCalledTimes(1));

    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    await waitFor(() => expect(apiMock.getNewsSnapshot).toHaveBeenCalledTimes(2));

    act(() => { void result.current.refreshNews(); });
    await waitFor(() => expect(apiMock.startNewsRefresh).toHaveBeenCalledTimes(1));
    unmount();

    expect(snapshotSignal).toBeDefined();
    expect(statusSignal).toBeDefined();
    expect(refreshSignal).toBeDefined();
    expect(snapshotSignal).not.toBe(statusSignal);
    expect(snapshotSignal).not.toBe(refreshSignal);
    expect(statusSignal).not.toBe(refreshSignal);
    expect(snapshotSignal?.aborted).toBe(true);
    expect(statusSignal?.aborted).toBe(true);
    expect(refreshSignal?.aborted).toBe(true);
    await act(async () => { await vi.advanceTimersByTimeAsync(5_000); });
    expect(apiMock.getNewsRefreshStatus).toHaveBeenCalledTimes(1);
  });

  it("ignores a late snapshot response after unmount", async () => {
    const pendingSnapshot = deferred<NewsSnapshotResponse>();
    apiMock.getNewsSnapshot.mockReturnValue(pendingSnapshot.promise);

    const { result, unmount } = renderHook(() => useNews());
    unmount();

    await act(async () => { pendingSnapshot.resolve(snapshotResponse("late", runningStatus())); });

    expect(result.current.snapshot).toBeNull();
    expect(apiMock.getNewsRefreshStatus).not.toHaveBeenCalled();
  });

  it("schedules the next status request exactly one second after the prior request resolves", async () => {
    const firstStatus = deferred<NewsRefreshStatus>();
    apiMock.getNewsSnapshot.mockResolvedValue(snapshotResponse("before", runningStatus()));
    apiMock.getNewsRefreshStatus
      .mockReturnValueOnce(firstStatus.promise)
      .mockResolvedValueOnce(runningStatus("cancelled"));

    const { unmount } = renderHook(() => useNews());
    await waitFor(() => expect(apiMock.getNewsSnapshot).toHaveBeenCalledTimes(1));

    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(apiMock.getNewsRefreshStatus).toHaveBeenCalledTimes(1);

    await act(async () => { firstStatus.resolve(runningStatus()); });
    await act(async () => { await vi.advanceTimersByTimeAsync(999); });
    expect(apiMock.getNewsRefreshStatus).toHaveBeenCalledTimes(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    expect(apiMock.getNewsRefreshStatus).toHaveBeenCalledTimes(2);

    unmount();
  });
});
