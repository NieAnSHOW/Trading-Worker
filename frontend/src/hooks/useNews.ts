import { useCallback, useEffect, useReducer, useRef } from "react";
import {
  api,
  type NewsPublicError,
  type NewsRefreshStatus,
  type NewsSnapshot,
  type NewsSnapshotResponse,
  type NewsTrackId,
} from "@/lib/api";

const POLL_INTERVAL_MS = 1000;

export interface NewsPageState {
  snapshot: NewsSnapshot | null;
  available: boolean;
  stale: boolean;
  snapshotError: NewsPublicError | null;
  refreshStatus: NewsRefreshStatus | null;
  selectedTrackId: NewsTrackId | null;
  isLoading: boolean;
  isRefreshing: boolean;
  error: unknown | null;
}

export interface NewsPageActions {
  selectTrack: (trackId: NewsTrackId) => void;
  refreshNews: () => Promise<void>;
}

type StateAction =
  | { type: "snapshotLoaded"; response: NewsSnapshotResponse }
  | { type: "snapshotFailed"; error: unknown }
  | { type: "refreshRequested" }
  | { type: "refreshStatus"; status: NewsRefreshStatus }
  | { type: "refreshFailed"; error: unknown }
  | { type: "trackSelected"; trackId: NewsTrackId };

const initialState: NewsPageState = {
  snapshot: null,
  available: false,
  stale: false,
  snapshotError: null,
  refreshStatus: null,
  selectedTrackId: null,
  isLoading: true,
  isRefreshing: false,
  error: null,
};

function refreshIsRunning(status: NewsRefreshStatus): boolean {
  return !["idle", "succeeded", "failed", "cancelled"].includes(status.state);
}

function defaultTrackId(snapshot: NewsSnapshot | null): NewsTrackId | null {
  if (!snapshot) return null;
  return snapshot.tracks.find((track) => track.state !== "unavailable")?.track_id ?? snapshot.tracks[0]?.track_id ?? null;
}

function reduceNewsState(state: NewsPageState, action: StateAction): NewsPageState {
  switch (action.type) {
    case "snapshotLoaded": {
      const { response } = action;
      const selectedTrackId = response.snapshot?.tracks.some((track) => track.track_id === state.selectedTrackId)
        ? state.selectedTrackId
        : defaultTrackId(response.snapshot);
      return {
        ...state,
        snapshot: response.snapshot,
        available: response.available,
        stale: response.stale,
        snapshotError: response.error,
        refreshStatus: response.refresh,
        selectedTrackId,
        isLoading: false,
        isRefreshing: refreshIsRunning(response.refresh),
        error: null,
      };
    }
    case "snapshotFailed":
      return { ...state, isLoading: false, error: action.error };
    case "refreshRequested":
      return { ...state, isLoading: false, isRefreshing: true, error: null };
    case "refreshStatus":
      return { ...state, refreshStatus: action.status, isRefreshing: refreshIsRunning(action.status) };
    case "refreshFailed":
      return { ...state, isRefreshing: false, error: action.error };
    case "trackSelected":
      return { ...state, selectedTrackId: action.trackId };
  }
}

function wasAborted(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

export function useNews(): NewsPageState & NewsPageActions {
  const [state, dispatch] = useReducer(reduceNewsState, initialState);
  const activeRef = useRef(true);
  const pollGenerationRef = useRef(0);
  const pollTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const snapshotControllerRef = useRef<AbortController | null>(null);
  const snapshotGenerationRef = useRef(0);
  const refreshControllerRef = useRef<AbortController | null>(null);
  const statusControllerRef = useRef<AbortController | null>(null);
  const pollRefreshRef = useRef<(generation: number) => Promise<void>>(async () => {});
  const effectGenerationRef = useRef(0);

  const stopPolling = useCallback(() => {
    pollGenerationRef.current += 1;
    if (pollTimerRef.current !== null) {
      clearTimeout(pollTimerRef.current);
      pollTimerRef.current = null;
    }
    statusControllerRef.current?.abort();
    statusControllerRef.current = null;
  }, []);

  const schedulePoll = useCallback((generation: number) => {
    if (!activeRef.current || generation !== pollGenerationRef.current) return;
    pollTimerRef.current = setTimeout(() => {
      pollTimerRef.current = null;
      void pollRefreshRef.current(generation);
    }, POLL_INTERVAL_MS);
  }, []);

  const startPolling = useCallback(() => {
    stopPolling();
    const generation = pollGenerationRef.current;
    schedulePoll(generation);
  }, [schedulePoll, stopPolling]);

  const invalidateSnapshot = useCallback(() => {
    snapshotGenerationRef.current += 1;
    snapshotControllerRef.current?.abort();
    snapshotControllerRef.current = null;
  }, []);

  const loadSnapshot = useCallback(async () => {
    snapshotControllerRef.current?.abort();
    const controller = new AbortController();
    snapshotControllerRef.current = controller;
    const generation = snapshotGenerationRef.current;

    try {
      const response = await api.getNewsSnapshot(controller.signal);
      if (!activeRef.current || controller.signal.aborted || generation !== snapshotGenerationRef.current) return;
      dispatch({ type: "snapshotLoaded", response });
      if (refreshIsRunning(response.refresh)) startPolling();
    } catch (error) {
      if (!activeRef.current || controller.signal.aborted || wasAborted(error) || generation !== snapshotGenerationRef.current) return;
      dispatch({ type: "snapshotFailed", error });
    } finally {
      if (snapshotControllerRef.current === controller) snapshotControllerRef.current = null;
    }
  }, [startPolling]);

  pollRefreshRef.current = async (generation: number) => {
    if (!activeRef.current || generation !== pollGenerationRef.current) return;
    const controller = new AbortController();
    statusControllerRef.current = controller;

    try {
      const status = await api.getNewsRefreshStatus(controller.signal);
      if (!activeRef.current || controller.signal.aborted || generation !== pollGenerationRef.current) return;
      dispatch({ type: "refreshStatus", status });
      if (refreshIsRunning(status)) {
        schedulePoll(generation);
      } else if (status.state === "succeeded") {
        await loadSnapshot();
      }
    } catch (error) {
      if (!activeRef.current || controller.signal.aborted || wasAborted(error) || generation !== pollGenerationRef.current) return;
      dispatch({ type: "refreshFailed", error });
    } finally {
      if (statusControllerRef.current === controller) statusControllerRef.current = null;
    }
  };

  const refreshNews = useCallback(async () => {
    stopPolling();
    invalidateSnapshot();
    refreshControllerRef.current?.abort();
    const controller = new AbortController();
    refreshControllerRef.current = controller;
    dispatch({ type: "refreshRequested" });

    try {
      const accepted = await api.startNewsRefresh(controller.signal);
      if (!activeRef.current || controller.signal.aborted) return;
      dispatch({ type: "refreshStatus", status: accepted.status });
      if (refreshIsRunning(accepted.status)) {
        startPolling();
      } else if (accepted.status.state === "succeeded") {
        await loadSnapshot();
      }
    } catch (error) {
      if (!activeRef.current || controller.signal.aborted || wasAborted(error)) return;
      dispatch({ type: "refreshFailed", error });
    } finally {
      if (refreshControllerRef.current === controller) refreshControllerRef.current = null;
    }
  }, [invalidateSnapshot, loadSnapshot, startPolling, stopPolling]);

  const selectTrack = useCallback((trackId: NewsTrackId) => {
    dispatch({ type: "trackSelected", trackId });
  }, []);

  useEffect(() => {
    activeRef.current = true;
    const effectGeneration = ++effectGenerationRef.current;
    const snapshotGeneration = snapshotGenerationRef.current;
    queueMicrotask(() => {
      if (
        activeRef.current
        && effectGeneration === effectGenerationRef.current
        && snapshotGeneration === snapshotGenerationRef.current
      ) {
        void loadSnapshot();
      }
    });

    return () => {
      activeRef.current = false;
      effectGenerationRef.current += 1;
      stopPolling();
      invalidateSnapshot();
      refreshControllerRef.current?.abort();
      statusControllerRef.current?.abort();
    };
  }, [invalidateSnapshot, loadSnapshot, stopPolling]);

  return { ...state, selectTrack, refreshNews };
}
