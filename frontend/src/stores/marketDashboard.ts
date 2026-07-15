import { create } from "zustand";
import type { PriceBar, MarketSummaryResponse, MarketSnapshotRequest } from "@/lib/api";
import { fetchWatchlistStocks, api } from "@/lib/api";
import {
  fetchDashboardIndexes,
  fetchMarketPulse,
  fetchDashboardQuotes,
  fetchDashboardDailyBars,
  fetchDashboardMarketSnapshot,
} from "@/lib/stockSdk";
import type { DashboardIndex, MarketPulseItem, DashboardQuote, DashboardMarketSnapshot } from "@/lib/stockSdk";

export interface MarketDashboardState {
  indexes: DashboardIndex[];
  pulse: MarketPulseItem[];
  pulseAsOf: string | null;
  pulseStale: boolean;
  quotes: Record<string, DashboardQuote>;
  selectedCode: string | null;
  selectedBars: PriceBar[];
  summary: MarketSummaryResponse | null;
  marketSnapshot: DashboardMarketSnapshot | null;

  indexesLoading: boolean;
  pulseLoading: boolean;
  quotesLoading: boolean;
  summaryLoading: boolean;
  barsLoading: boolean;
  marketSnapshotLoading: boolean;

  indexesError: string | null;
  pulseError: string | null;
  quotesError: string | null;
  summaryError: string | null;
  barsError: string | null;
  marketSnapshotError: string | null;

  pollingTimerId: ReturnType<typeof setInterval> | null;
  visibilityHandler: (() => void) | null;

  initialize: () => Promise<void>;
  refreshMarketData: () => Promise<void>;
  refreshPulse: () => Promise<void>;
  refreshSummary: (force?: boolean) => Promise<void>;
  setSelectedCode: (code: string) => Promise<void>;
  startPolling: () => void;
  stopPolling: () => void;
}

const POLL_INTERVAL = 15000;

export const useMarketDashboardStore = create<MarketDashboardState>((set, get) => ({
  indexes: [],
  pulse: [],
  pulseAsOf: null,
  pulseStale: false,
  quotes: {},
  selectedCode: null,
  selectedBars: [],
  summary: null,
  marketSnapshot: null,

  indexesLoading: false,
  pulseLoading: false,
  quotesLoading: false,
  summaryLoading: false,
  barsLoading: false,
  marketSnapshotLoading: false,

  indexesError: null,
  pulseError: null,
  quotesError: null,
  summaryError: null,
  barsError: null,
  marketSnapshotError: null,

  pollingTimerId: null,
  visibilityHandler: null,

  initialize: async () => {
    await get().refreshMarketData();
    await get().refreshSummary(false);
  },

  refreshPulse: async () => {
    set({ pulseLoading: true });
    try {
      const result = await fetchMarketPulse();
      if (result.stale || result.error) {
        set({ pulseError: result.error ?? "pulse failed", pulseStale: true });
        return;
      }
      set({
        pulse: result.data,
        pulseAsOf: result.asOf,
        pulseStale: false,
        pulseError: null,
      });
    } catch (error) {
      set({
        pulseError: (error as Error)?.message ?? "pulse failed",
        pulseStale: true,
      });
    } finally {
      set({ pulseLoading: false });
    }
  },

  refreshMarketData: async () => {
    set({
      indexesLoading: true,
      quotesLoading: true,
      marketSnapshotLoading: true,
    });

    const [indexesResult, stocksResult, marketSnapshotResult] = await Promise.allSettled([
      fetchDashboardIndexes(),
      fetchWatchlistStocks(),
      fetchDashboardMarketSnapshot(),
    ]);

    // Indexes
    if (indexesResult.status === "fulfilled") {
      set({ indexes: indexesResult.value.data, indexesError: null });
    } else {
      set({ indexesError: (indexesResult.reason as Error)?.message ?? "indexes failed" });
    }

    // Full-market snapshot
    if (marketSnapshotResult.status === "fulfilled") {
      set({
        marketSnapshot: marketSnapshotResult.value.data,
        marketSnapshotError: marketSnapshotResult.value.error ?? null,
      });
    } else {
      set({ marketSnapshotError: (marketSnapshotResult.reason as Error)?.message ?? "market snapshot failed" });
    }

    // Watchlist → Quotes
    if (stocksResult.status === "fulfilled") {
      const codes = stocksResult.value.stocks.map((s) => s.code);
      if (codes.length > 0) {
        try {
          const quotesResult = await fetchDashboardQuotes(codes);
          set({ quotes: quotesResult.data, quotesError: null });
        } catch (e) {
          set({ quotesError: (e as Error)?.message ?? "quotes failed" });
        }
      } else {
        set({ quotes: {}, quotesError: null });
      }
    } else {
      set({ quotesError: (stocksResult.reason as Error)?.message ?? "watchlist failed" });
    }

    set({
      indexesLoading: false,
      quotesLoading: false,
      marketSnapshotLoading: false,
    });
  },

  refreshSummary: async (force = false) => {
    set({ summaryLoading: true });

    const state = get();
    const snapshot: MarketSnapshotRequest = {
      market: "a_share",
      market_as_of: new Date().toISOString(),
      force_refresh: force,
      indices: state.indexes.map((i) => ({
        code: i.code,
        name: i.name,
        price: i.price ?? 0,
        change_pct: i.changePct ?? 0,
      })),
      sectors: [],
      watchlist: Object.values(state.quotes).map((q) => ({
        code: q.code,
        name: q.name,
        change_pct: q.changePct,
      })),
    };

    try {
      const response = await api.createMarketSummary(snapshot);
      set({
        summary: response,
        summaryError: response.available ? null : (response.error_code ?? "unavailable"),
      });
    } catch (e) {
      set({ summaryError: (e as Error)?.message ?? "summary failed" });
    }

    set({ summaryLoading: false });
  },

  setSelectedCode: async (code: string) => {
    set({ selectedCode: code, barsLoading: true, barsError: null });

    try {
      const result = await fetchDashboardDailyBars(code);
      set({ selectedBars: result.data, barsError: result.error ?? null });
    } catch (e) {
      set({ barsError: (e as Error)?.message ?? "bars failed" });
    }

    set({ barsLoading: false });
  },

  startPolling: () => {
    if (get().visibilityHandler) return;

    const startInterval = () => {
      if (get().pollingTimerId) return;
      const timerId = setInterval(() => {
        if (document.visibilityState === "visible") {
          get().refreshMarketData();
        }
      }, POLL_INTERVAL);
      set({ pollingTimerId: timerId });
    };

    const handler = () => {
      if (document.visibilityState !== "visible") {
        const { pollingTimerId } = get();
        if (pollingTimerId) {
          clearInterval(pollingTimerId);
          set({ pollingTimerId: null });
        }
        return;
      }

      get().refreshMarketData();
      startInterval();
    };

    document.addEventListener("visibilitychange", handler);
    set({ visibilityHandler: handler });
    if (document.visibilityState === "visible") {
      startInterval();
    }
  },

  stopPolling: () => {
    const { pollingTimerId, visibilityHandler } = get();

    if (pollingTimerId) {
      clearInterval(pollingTimerId);
    }
    if (visibilityHandler) {
      document.removeEventListener("visibilitychange", visibilityHandler);
    }

    set({ pollingTimerId: null, visibilityHandler: null });
  },
}));
