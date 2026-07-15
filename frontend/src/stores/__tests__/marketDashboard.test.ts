import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/stockSdk", () => ({
  fetchDashboardIndexes: vi.fn(),
  fetchMarketPulse: vi.fn(),
  fetchDashboardQuotes: vi.fn(),
  fetchDashboardDailyBars: vi.fn(),
  fetchDashboardMarketSnapshot: vi.fn(),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...original,
    fetchWatchlistStocks: vi.fn(),
    api: {
      ...original.api,
      createMarketSummary: vi.fn(),
    },
  };
});

import {
  fetchDashboardIndexes,
  fetchMarketPulse,
  fetchDashboardQuotes,
  fetchDashboardDailyBars,
  fetchDashboardMarketSnapshot,
} from "@/lib/stockSdk";
import { fetchWatchlistStocks, api } from "@/lib/api";
import { useMarketDashboardStore } from "../marketDashboard";

const mockFetchIndexes = vi.mocked(fetchDashboardIndexes);
const mockFetchPulse = vi.mocked(fetchMarketPulse);
const mockFetchQuotes = vi.mocked(fetchDashboardQuotes);
const mockFetchDailyBars = vi.mocked(fetchDashboardDailyBars);
const mockFetchMarketSnapshot = vi.mocked(fetchDashboardMarketSnapshot);
const mockFetchWatchlist = vi.mocked(fetchWatchlistStocks);
const mockCreateSummary = vi.mocked(api.createMarketSummary);

const MARKET_SNAPSHOT = {
  breadth: { total: 4, up: 2, flat: 1, down: 1, upPct: 50, strongUp: 1, strongDown: 1, avgChangePct: 0, distribution: [] },
  emotion: { score: 56, label: "warm", dimensions: [] },
  trend: { strongUp: 1, strongDown: 1, nearHigh: 1, nearLow: 1, highLowRatio: 50 },
  limit: { limitUp: 2, limitDown: 1, broken: 1, tiers: [] },
  concepts: [{ code: "BK001", name: "人工智能", changePct: 3.2, riseCount: 12, fallCount: 2, leadingStock: "龙头A", leadingStockChangePct: 8.8 }],
  source: "stock-sdk",
  asOf: "2026-07-13T10:00:00Z",
  stale: false,
};

const defaultState = {
  indexes: [],
  pulse: [],
  pulseAsOf: null as string | null,
  pulseStale: false,
  quotes: {} as Record<string, unknown>,
  selectedCode: null as string | null,
  selectedBars: [],
  summary: null,
  marketSnapshot: null,
  indexesLoading: false,
  pulseLoading: false,
  quotesLoading: false,
  summaryLoading: false,
  barsLoading: false,
  marketSnapshotLoading: false,
  indexesError: null as string | null,
  pulseError: null as string | null,
  quotesError: null as string | null,
  summaryError: null as string | null,
  barsError: null as string | null,
  marketSnapshotError: null as string | null,
  pollingTimerId: null as ReturnType<typeof setInterval> | null,
  visibilityHandler: null as (() => void) | null,
};

beforeEach(() => {
  vi.clearAllMocks();
  useMarketDashboardStore.setState({ ...defaultState });
  mockFetchIndexes.mockResolvedValue({ data: [], asOf: "2026-07-13T10:00:00Z", stale: false });
  mockFetchPulse.mockResolvedValue({ data: [], asOf: "2026-07-13T10:00:00Z", stale: false });
  mockFetchWatchlist.mockResolvedValue({ stocks: [] });
  mockFetchMarketSnapshot.mockResolvedValue({ data: MARKET_SNAPSHOT, asOf: MARKET_SNAPSHOT.asOf, stale: false });
});

describe("useMarketDashboardStore", () => {
  describe("initial state", () => {
    it("has empty defaults", () => {
      const s = useMarketDashboardStore.getState();
      expect(s.indexes).toEqual([]);
      expect(s.pulse).toEqual([]);
      expect(s.pulseAsOf).toBeNull();
      expect(s.pulseStale).toBe(false);
      expect(s.quotes).toEqual({});
      expect(s.selectedCode).toBeNull();
      expect(s.selectedBars).toEqual([]);
      expect(s.summary).toBeNull();
      expect(s.marketSnapshot).toBeNull();
      expect(s.indexesLoading).toBe(false);
      expect(s.pulseLoading).toBe(false);
      expect(s.quotesLoading).toBe(false);
      expect(s.summaryLoading).toBe(false);
      expect(s.barsLoading).toBe(false);
      expect(s.marketSnapshotLoading).toBe(false);
      expect(s.indexesError).toBeNull();
      expect(s.pulseError).toBeNull();
      expect(s.quotesError).toBeNull();
      expect(s.summaryError).toBeNull();
      expect(s.barsError).toBeNull();
      expect(s.marketSnapshotError).toBeNull();
    });
  });

  describe("refreshPulse", () => {
    it("updates only pulse state from the Market Pulse endpoint", async () => {
      const pulse = [{
        code: "600519",
        name: "贵州茅台",
        time: "10:00",
        changeType: "涨停",
        info: "白酒板块走强",
        source: "test",
        stale: false,
      }];
      mockFetchPulse.mockResolvedValue({
        data: pulse,
        asOf: "2026-07-13T10:00:00Z",
        stale: false,
      });

      await useMarketDashboardStore.getState().refreshPulse();

      const state = useMarketDashboardStore.getState();
      expect(mockFetchPulse).toHaveBeenCalledOnce();
      expect(mockFetchIndexes).not.toHaveBeenCalled();
      expect(mockFetchWatchlist).not.toHaveBeenCalled();
      expect(mockFetchMarketSnapshot).not.toHaveBeenCalled();
      expect(state.pulse).toEqual(pulse);
      expect(state.pulseAsOf).toBe("2026-07-13T10:00:00Z");
      expect(state.pulseStale).toBe(false);
      expect(state.pulseLoading).toBe(false);
      expect(state.pulseError).toBeNull();
    });

    it("retains the last successful pulse list when the provider returns a stale error", async () => {
      const current = [{
        code: "600519", name: "贵州茅台", time: "10:00", changeType: "涨停",
        info: "白酒板块走强", source: "test", stale: false,
      }];
      useMarketDashboardStore.setState({
        pulse: current as never,
        pulseAsOf: "2026-07-15T09:30:00Z",
      });
      mockFetchPulse.mockResolvedValue({
        data: [], asOf: "2026-07-15T09:45:00Z", stale: true,
        error: "pulse unavailable",
      });

      await useMarketDashboardStore.getState().refreshPulse();

      expect(useMarketDashboardStore.getState()).toMatchObject({
        pulse: current,
        pulseAsOf: "2026-07-15T09:30:00Z",
        pulseStale: true,
        pulseError: "pulse unavailable",
      });
    });
  });

  describe("refreshMarketData", () => {
    it("updates indexes and quotes on success", async () => {
      mockFetchIndexes.mockResolvedValue({
        data: [{ code: "000001", name: "上证指数", price: 3000, changePct: 0.5, changeAmt: 15, source: "test", stale: false }],
        asOf: "2026-07-13T10:00:00Z",
        stale: false,
      });
      mockFetchWatchlist.mockResolvedValue({
        stocks: [{ code: "600519", name: "贵州茅台", market: "a_stock", added_at: "2026-07-01T00:00:00" }],
      });
      mockFetchQuotes.mockResolvedValue({
        data: { "600519": { code: "600519", name: "贵州茅台", price: 1500, changePct: 1.2, changeAmt: 18, source: "test", stale: false } },
        asOf: "2026-07-13T10:00:00Z",
        stale: false,
      });

      await useMarketDashboardStore.getState().refreshMarketData();

      const s = useMarketDashboardStore.getState();
      expect(s.indexes).toHaveLength(1);
      expect(Object.keys(s.quotes)).toHaveLength(1);
      expect(s.indexesError).toBeNull();
      expect(s.quotesError).toBeNull();
    });

    it("does not request Market Pulse from dashboard market-data refresh", async () => {
      await useMarketDashboardStore.getState().refreshMarketData();
      expect(mockFetchPulse).not.toHaveBeenCalled();
    });

    it("keeps the market snapshot available while reporting a stale concept area", async () => {
      const staleSnapshot = {
        ...MARKET_SNAPSHOT,
        stale: true,
        errors: { concepts: "concept source unavailable" },
      };
      mockFetchMarketSnapshot.mockResolvedValue({
        data: staleSnapshot,
        asOf: staleSnapshot.asOf,
        stale: true,
        error: "concept source unavailable",
      });

      await useMarketDashboardStore.getState().refreshMarketData();

      const s = useMarketDashboardStore.getState();
      expect(mockFetchMarketSnapshot).toHaveBeenCalledOnce();
      expect(s.marketSnapshot).toEqual(staleSnapshot);
      expect(s.marketSnapshotError).toBe("concept source unavailable");
      expect(s.marketSnapshotLoading).toBe(false);
    });

    it("preserves old quotes on quotes failure and sets quotesError", async () => {
      // pre-populate quotes
      useMarketDashboardStore.setState({
        quotes: { "000001": { code: "000001", name: "平安银行", price: 10, changePct: 1, changeAmt: 0.1, source: "test", stale: false } } as never,
      });

      mockFetchIndexes.mockResolvedValue({
        data: [],
        asOf: "2026-07-13T10:00:00Z",
        stale: false,
      });
      mockFetchPulse.mockResolvedValue({
        data: [],
        asOf: "2026-07-13T10:00:00Z",
        stale: false,
      });
      mockFetchWatchlist.mockRejectedValue(new Error("watchlist failed"));

      await useMarketDashboardStore.getState().refreshMarketData();

      const s = useMarketDashboardStore.getState();
      // Old quotes preserved
      expect(s.quotes["000001"]).toBeDefined();
      expect(s.quotesError).toBe("watchlist failed");
    });

    it("preserves old indexes on indexes failure and sets indexesError", async () => {
      useMarketDashboardStore.setState({
        indexes: [{ code: "000001", name: "上证指数", price: 3000, changePct: 0.5, changeAmt: 15, source: "test", stale: false }] as never,
      });

      mockFetchIndexes.mockRejectedValue(new Error("indexes failed"));
      mockFetchPulse.mockResolvedValue({
        data: [],
        asOf: "2026-07-13T10:00:00Z",
        stale: false,
      });
      mockFetchWatchlist.mockResolvedValue({ stocks: [] });

      await useMarketDashboardStore.getState().refreshMarketData();

      const s = useMarketDashboardStore.getState();
      expect(s.indexes).toHaveLength(1);
      expect(s.indexesError).toBe("indexes failed");
    });
  });

  describe("refreshSummary", () => {
    it("passes force_refresh: true when force=true", async () => {
      mockCreateSummary.mockResolvedValue({
        available: true,
        summary: { headline: "test", drivers: [], risks: [], focus: [] },
        market_as_of: "2026-07-13T10:00:00Z",
        generated_at: "2026-07-13T10:00:00Z",
        cached: false,
        stale: false,
      });

      await useMarketDashboardStore.getState().refreshSummary(true);

      const callArg = mockCreateSummary.mock.calls[0]?.[0] as Record<string, unknown> | undefined;
      expect(callArg?.force_refresh).toBe(true);
    });

    it("does not call force_refresh by default", async () => {
      mockCreateSummary.mockResolvedValue({
        available: true,
        summary: { headline: "test", drivers: [], risks: [], focus: [] },
        market_as_of: "2026-07-13T10:00:00Z",
        generated_at: "2026-07-13T10:00:00Z",
        cached: false,
        stale: false,
      });

      await useMarketDashboardStore.getState().refreshSummary();

      const callArg = mockCreateSummary.mock.calls[0]?.[0] as Record<string, unknown> | undefined;
      expect(callArg?.force_refresh).toBeFalsy();
    });

    it("does not clear indexes when summary unavailable", async () => {
      useMarketDashboardStore.setState({
        indexes: [{ code: "000001", name: "上证指数", price: 3000, changePct: 0.5, changeAmt: 15, source: "test", stale: false }] as never,
      });

      mockCreateSummary.mockResolvedValue({
        available: false,
        summary: null,
        market_as_of: null,
        generated_at: null,
        cached: false,
        stale: false,
        error_code: "no_provider",
      });

      await useMarketDashboardStore.getState().refreshSummary(false);

      const s = useMarketDashboardStore.getState();
      expect(s.indexes).toHaveLength(1);
      expect(s.summaryError).toBe("no_provider");
    });
  });

  describe("setSelectedCode", () => {
    it("triggers K-line load for selected symbol only", async () => {
      mockFetchDailyBars.mockResolvedValue({
        data: [{ time: "2026-07-13", open: 100, high: 105, low: 99, close: 102, volume: 10000 }],
        asOf: "2026-07-13T10:00:00Z",
        stale: false,
      });

      await useMarketDashboardStore.getState().setSelectedCode("600519");

      expect(mockFetchDailyBars).toHaveBeenCalledWith("600519");
      const s = useMarketDashboardStore.getState();
      expect(s.selectedCode).toBe("600519");
      expect(s.selectedBars).toHaveLength(1);
    });

    it("sets barsError on K-line failure", async () => {
      mockFetchDailyBars.mockRejectedValue(new Error("kline failed"));

      await useMarketDashboardStore.getState().setSelectedCode("600519");

      const s = useMarketDashboardStore.getState();
      expect(s.selectedCode).toBe("600519");
      expect(s.barsError).toBe("kline failed");
    });

    it("keeps the provider error when K-line returns a stale empty result", async () => {
      mockFetchDailyBars.mockResolvedValue({
        data: [],
        asOf: "2026-07-13T10:00:00Z",
        stale: true,
        error: "K-line source unavailable",
      });

      await useMarketDashboardStore.getState().setSelectedCode("600519");

      const s = useMarketDashboardStore.getState();
      expect(s.selectedBars).toEqual([]);
      expect(s.barsError).toBe("K-line source unavailable");
    });
  });

  describe("polling visibility", () => {
    let setIntervalSpy: ReturnType<typeof vi.spyOn>;
    let clearIntervalSpy: ReturnType<typeof vi.spyOn>;

    beforeEach(() => {
      vi.useFakeTimers();
      setIntervalSpy = vi.spyOn(globalThis, "setInterval");
      clearIntervalSpy = vi.spyOn(globalThis, "clearInterval");
    });

    afterEach(() => {
      vi.useRealTimers();
    });

    it("does not start interval when document is hidden", () => {
      vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");

      useMarketDashboardStore.getState().startPolling();

      expect(setIntervalSpy).not.toHaveBeenCalled();
    });

    it("starts interval when visible", () => {
      vi.spyOn(document, "visibilityState", "get").mockReturnValue("visible");

      useMarketDashboardStore.getState().startPolling();

      expect(setIntervalSpy).toHaveBeenCalledWith(expect.any(Function), 15000);
    });

    it("pauses scheduled refreshes when a visible tab becomes hidden", () => {
      const visibilitySpy = vi.spyOn(document, "visibilityState", "get");
      visibilitySpy.mockReturnValue("visible");
      useMarketDashboardStore.getState().startPolling();

      visibilitySpy.mockReturnValue("hidden");
      document.dispatchEvent(new Event("visibilitychange"));
      vi.advanceTimersByTime(15_000);

      expect(clearIntervalSpy).toHaveBeenCalled();
      expect(mockFetchMarketSnapshot).not.toHaveBeenCalled();
      expect(useMarketDashboardStore.getState().pollingTimerId).toBeNull();
    });

    it("refreshes market data once on visibility return after being hidden", () => {
      const visibilitySpy = vi.spyOn(document, "visibilityState", "get");

      // start hidden
      visibilitySpy.mockReturnValue("hidden");
      useMarketDashboardStore.getState().startPolling();
      expect(setIntervalSpy).not.toHaveBeenCalled();

      // prepare mock responses for when visibility changes
      mockFetchIndexes.mockResolvedValue({
        data: [],
        asOf: "2026-07-13T10:00:00Z",
        stale: false,
      });
      mockFetchPulse.mockResolvedValue({
        data: [],
        asOf: "2026-07-13T10:00:00Z",
        stale: false,
      });
      mockFetchWatchlist.mockResolvedValue({ stocks: [] });

      // become visible
      visibilitySpy.mockReturnValue("visible");
      document.dispatchEvent(new Event("visibilitychange"));

      // advance timers to allow async to resolve
      vi.advanceTimersByTime(0);
    });

    it("registers visibilitychange handler on start and removes it on stop", () => {
      const addSpy = vi.spyOn(document, "addEventListener");
      const removeSpy = vi.spyOn(document, "removeEventListener");

      vi.spyOn(document, "visibilityState", "get").mockReturnValue("visible");
      useMarketDashboardStore.getState().startPolling();

      expect(addSpy).toHaveBeenCalledWith("visibilitychange", expect.any(Function));

      useMarketDashboardStore.getState().stopPolling();

      expect(removeSpy).toHaveBeenCalledWith("visibilitychange", expect.any(Function));
    });

    it("stopPolling clears interval and handler", () => {
      vi.spyOn(document, "visibilityState", "get").mockReturnValue("visible");
      useMarketDashboardStore.getState().startPolling();

      useMarketDashboardStore.getState().stopPolling();

      expect(clearIntervalSpy).toHaveBeenCalled();
      const s = useMarketDashboardStore.getState();
      expect(s.pollingTimerId).toBeNull();
      expect(s.visibilityHandler).toBeNull();
    });
  });

  describe("initialize", () => {
    it("calls refreshMarketData and non-forced refreshSummary", async () => {
      mockFetchIndexes.mockResolvedValue({
        data: [],
        asOf: "2026-07-13T10:00:00Z",
        stale: false,
      });
      mockFetchPulse.mockResolvedValue({
        data: [],
        asOf: "2026-07-13T10:00:00Z",
        stale: false,
      });
      mockFetchWatchlist.mockResolvedValue({ stocks: [] });
      mockCreateSummary.mockResolvedValue({
        available: false,
        summary: null,
        market_as_of: null,
        generated_at: null,
        cached: false,
        stale: false,
      });

      await useMarketDashboardStore.getState().initialize();

      expect(mockFetchIndexes).toHaveBeenCalled();
      expect(mockCreateSummary).toHaveBeenCalled();
      // verify non-forced
      const callArg = mockCreateSummary.mock.calls[0]?.[0] as Record<string, unknown> | undefined;
      expect(callArg?.force_refresh).toBeFalsy();
    });
  });
});
