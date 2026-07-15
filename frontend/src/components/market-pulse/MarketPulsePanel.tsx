import { useEffect, useMemo, useState } from "react";
import { AlertCircle, ChevronLeft, ChevronRight, RefreshCw, Search, X } from "lucide-react";
import { useTranslation } from "react-i18next";
import { cn } from "@/lib/utils";
import { useMarketDashboardStore } from "@/stores/marketDashboard";
import {
  categorizeMarketPulse,
  countMarketPulseCategories,
  filterMarketPulse,
  getPulsePage,
  pulseItemKey,
  type MarketPulseCategory,
} from "./marketPulse";

const POLL_INTERVAL = 15_000;
const PAGE_SIZES = [200, 500, 1000] as const;

const categoryLabels: Record<MarketPulseCategory, string> = {
  all: "全部",
  limitDownBroken: "跌停/炸板",
  limitUp: "涨停异动",
  upward: "上涨异动",
  downward: "下跌异动",
  turnoverCapital: "成交/资金",
  other: "其他",
};

function eventTone(changeType: string) {
  if (/(涨|上涨|买入|拉升|反弹|新高)/.test(changeType)) return "text-red-500";
  if (/(跌|下跌|卖出|跳水|新低)/.test(changeType)) return "text-green-500";
  return "text-foreground";
}

function eventBadgeTone(changeType: string) {
  if (/(涨|上涨|买入|拉升|反弹|新高)/.test(changeType)) return "bg-red-500/10 text-red-500";
  if (/(跌|下跌|卖出|跳水|新低)/.test(changeType)) return "bg-green-500/10 text-green-500";
  return "bg-muted text-muted-foreground";
}

export function MarketPulsePanel() {
  const { t } = useTranslation();
  const pulse = useMarketDashboardStore((state) => state.pulse);
  const pulseLoading = useMarketDashboardStore((state) => state.pulseLoading);
  const pulseError = useMarketDashboardStore((state) => state.pulseError);
  const pulseAsOf = useMarketDashboardStore((state) => state.pulseAsOf);
  const pulseStale = useMarketDashboardStore((state) => state.pulseStale);
  const refreshPulse = useMarketDashboardStore((state) => state.refreshPulse);
  const [category, setCategory] = useState<MarketPulseCategory>("all");
  const [query, setQuery] = useState("");
  const [hideStale, setHideStale] = useState(false);
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = useState<(typeof PAGE_SIZES)[number]>(200);

  useEffect(() => {
    const refreshWhenVisible = () => {
      if (document.visibilityState === "visible") {
        void refreshPulse();
      }
    };

    void refreshPulse();
    const timer = window.setInterval(refreshWhenVisible, POLL_INTERVAL);
    document.addEventListener("visibilitychange", refreshWhenVisible);

    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", refreshWhenVisible);
    };
  }, [refreshPulse]);

  const counts = useMemo(() => countMarketPulseCategories(pulse), [pulse]);
  const keyedEvents = useMemo(
    () => pulse.map((item, index) => ({ item, key: pulseItemKey(item, index) })),
    [pulse],
  );
  const filteredEvents = useMemo(
    () =>
      keyedEvents.filter(
        ({ item }) => filterMarketPulse([item], { category, query, hideStale }).length === 1,
      ),
    [category, hideStale, keyedEvents, query],
  );
  const filteredItems = useMemo(() => filteredEvents.map(({ item }) => item), [filteredEvents]);
  const pageResult = useMemo(() => getPulsePage(filteredItems, page, pageSize), [filteredItems, page, pageSize]);
  const pageEvents = useMemo(() => {
    const start = pageResult.page * pageSize;
    return filteredEvents.slice(start, start + pageSize);
  }, [filteredEvents, pageResult.page, pageSize]);
  const selectedEvent = useMemo(
    () => filteredEvents.find((event) => event.key === selectedKey) ?? null,
    [filteredEvents, selectedKey],
  );

  useEffect(() => {
    if (pageResult.page !== page) {
      setPage(pageResult.page);
    }
  }, [page, pageResult.page]);

  useEffect(() => {
    if (!selectedKey || !filteredEvents.some((event) => event.key === selectedKey)) {
      setSelectedKey(filteredEvents[0]?.key ?? null);
    }
  }, [filteredEvents, selectedKey]);

  const resetPage = () => setPage(0);
  const clearFilters = () => {
    setCategory("all");
    setQuery("");
    setHideStale(false);
    resetPage();
  };

  return (
    <div className="flex h-full w-full flex-col gap-3 p-3 lg:gap-3 lg:p-5">
      <header className="flex items-start justify-between gap-3">
        <div className="min-w-0 space-y-1">
          <h1 className="text-xl font-bold">
            {t("marketPulse.title", "市场异动")}
          </h1>
          <p className="text-sm text-muted-foreground">
            {t("marketPulse.description", "跟踪盘中股票异动和资金信号")}
          </p>
          {pulseAsOf && (
            <p className="text-xs text-muted-foreground">
              {t("marketPulse.asOf", "更新于 {{asOf}}", { asOf: pulseAsOf })}
            </p>
          )}
        </div>
        <button
          type="button"
          onClick={() => void refreshPulse()}
          className="rounded-md border p-1.5 text-muted-foreground hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary disabled:cursor-not-allowed disabled:opacity-50"
          aria-label={t("marketPulse.refresh", "刷新市场异动")}
          title={t("marketPulse.refresh", "刷新市场异动")}
          disabled={pulseLoading}
        >
          <RefreshCw
            className={cn("h-4 w-4", pulseLoading && "animate-spin")}
            aria-hidden="true"
          />
        </button>
      </header>

      <section
        aria-labelledby="market-pulse-overview-title"
        className="grid grid-cols-2 gap-3 sm:grid-cols-4"
      >
        <h2 id="market-pulse-overview-title" className="sr-only">
          {t("marketPulse.overview", "市场异动概览")}
        </h2>
        <SummaryCard label={t("marketPulse.total", "异动总数")} value={counts.all} />
        <SummaryCard label={t("marketPulse.limitUp", "涨停异动")} value={counts.limitUp} tone="text-red-500" />
        <SummaryCard label={t("marketPulse.upward", "上涨异动")} value={counts.upward} tone="text-red-500" />
        <SummaryCard
          label={t("marketPulse.limitDownBroken", "跌停/炸板")}
          value={counts.downward + counts.limitDownBroken}
          tone="text-green-500"
        />
      </section>

      <div className="grid min-h-0 flex-1 gap-3 lg:grid-cols-[minmax(15rem,0.36fr)_minmax(0,1fr)] lg:gap-3">
        <aside
          aria-labelledby="market-pulse-list-title"
          className="min-h-0 rounded-lg border bg-card p-3 lg:overflow-auto"
        >
          <div className="mb-3">
            <h2 id="market-pulse-list-title" className="text-sm font-semibold">
              {t("marketPulse.events", "异动事件")}
            </h2>
            <p className="mt-0.5 text-xs text-muted-foreground">
              {t("marketPulse.showing", "显示 {{count}} / {{total}}", {
                count: filteredEvents.length,
                total: pulse.length,
              })}
            </p>
          </div>

          <label className="relative mb-3 block">
            <Search
              className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
              aria-hidden="true"
            />
            <span className="sr-only">{t("marketPulse.search", "搜索市场异动")}</span>
            <input
              type="search"
              value={query}
              onChange={(event) => {
                setQuery(event.target.value);
                resetPage();
              }}
              placeholder={t("marketPulse.searchPlaceholder", "搜索名称或代码")}
              aria-label={t("marketPulse.search", "搜索市场异动")}
              className="h-8 w-full rounded-md border bg-background py-1 pl-8 pr-2 text-sm outline-none placeholder:text-muted-foreground focus-visible:ring-2 focus-visible:ring-primary"
            />
          </label>

          <div className="mb-3 flex flex-wrap gap-1" aria-label={t("marketPulse.categories", "异动分类")}>
            {(Object.keys(categoryLabels) as MarketPulseCategory[]).map((value) => (
              <button
                key={value}
                type="button"
                aria-pressed={category === value}
                onClick={() => {
                  setCategory(value);
                  resetPage();
                }}
                className={cn(
                  "rounded-md px-2 py-1 text-xs font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary",
                  category === value ? "bg-primary text-primary-foreground" : "bg-muted text-muted-foreground hover:bg-muted/70",
                )}
              >
                {t(`marketPulse.category.${value}`, categoryLabels[value])} {counts[value]}
              </button>
            ))}
          </div>

          <label className="mb-3 flex cursor-pointer items-center gap-2 text-xs text-muted-foreground">
            <input
              type="checkbox"
              checked={hideStale}
              onChange={(event) => {
                setHideStale(event.target.checked);
                resetPage();
              }}
              className="h-3.5 w-3.5 rounded border-input accent-primary"
            />
            {t("marketPulse.hideStale", "隐藏过期数据")}
          </label>

          {pulseLoading && pulse.length === 0 && (
            <div role="status" className="space-y-2" aria-label={t("marketPulse.loading", "正在加载市场异动")}>
              <div className="h-12 animate-pulse rounded-md bg-muted/60" />
              <div className="h-12 animate-pulse rounded-md bg-muted/60" />
              <div className="h-12 animate-pulse rounded-md bg-muted/60" />
            </div>
          )}

          {pulseError && pulse.length === 0 && !pulseLoading && (
            <div role="alert" className="space-y-2 text-sm text-destructive">
              <div className="flex items-start gap-2">
                <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
                <p>{pulseError}</p>
              </div>
              <button
                type="button"
                onClick={() => void refreshPulse()}
                className="rounded-md border px-2 py-1 text-xs font-medium text-foreground hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"
              >
                {t("marketPulse.retry", "重试")}
              </button>
            </div>
          )}

          {pulseError && pulse.length > 0 && (
            <div role="alert" className="mb-3 flex items-start gap-2 rounded-md border border-warning/40 bg-warning/10 p-2 text-xs text-foreground">
              <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-warning" aria-hidden="true" />
              <p>{t("marketPulse.staleError", "数据可能已过期：{{error}}", { error: pulseError })}</p>
            </div>
          )}

          {!pulseError && pulseStale && pulse.length > 0 && (
            <p role="status" className="mb-3 rounded-md bg-warning/10 p-2 text-xs text-foreground">
              {t("marketPulse.stale", "当前显示的是可能已过期的数据")}
            </p>
          )}

          {pulseLoading && pulse.length > 0 && (
            <p role="status" className="mb-2 text-xs text-muted-foreground">
              {t("marketPulse.refreshing", "正在更新市场异动")}
            </p>
          )}

          {!pulseLoading && !pulseError && pulse.length === 0 && (
            <p className="py-6 text-center text-sm text-muted-foreground">
              {t("marketPulse.empty", "暂无市场异动")}
            </p>
          )}

          {filteredEvents.length === 0 && pulse.length > 0 && (
            <div role="status" className="py-6 text-center">
              <p className="text-sm text-muted-foreground">
                {t("marketPulse.noMatch", "没有匹配的市场异动")}
              </p>
              <button
                type="button"
                onClick={clearFilters}
                className="mt-2 inline-flex items-center gap-1 rounded-md border px-2 py-1 text-xs font-medium hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"
              >
                <X className="h-3.5 w-3.5" aria-hidden="true" />
                {t("marketPulse.clearFilters", "清除筛选")}
              </button>
            </div>
          )}

          {pageEvents.length > 0 && (
            <ul className="divide-y">
              {pageEvents.map(({ item, key }) => (
                <li key={key}>
                  <button
                    type="button"
                    aria-pressed={selectedKey === key}
                    onClick={() => setSelectedKey(key)}
                    className={cn(
                      "w-full px-1 py-2 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary",
                      selectedKey === key && "bg-muted",
                    )}
                  >
                    <span className="flex items-center justify-between gap-2">
                      <span className="min-w-0 truncate text-sm font-medium">
                        {item.name} <span className="font-mono text-xs text-muted-foreground">{item.code}</span>
                      </span>
                      <time className="shrink-0 font-mono text-xs text-muted-foreground">{item.time}</time>
                    </span>
                    <span className="mt-1 flex items-center gap-2">
                      <span className={cn("rounded px-1.5 py-0.5 text-[10px] font-medium", eventBadgeTone(item.changeType))}>
                        {item.changeType}
                      </span>
                      {item.stale && <span className="text-[10px] text-warning">{t("marketPulse.staleTag", "过期")}</span>}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}

          {filteredEvents.length > 0 && (
            <div className="mt-3 flex items-center justify-between gap-2 border-t pt-3">
              <label className="flex items-center gap-1 text-xs text-muted-foreground">
                <span className="sr-only">{t("marketPulse.pageSize", "每页数量")}</span>
                <select
                  value={pageSize}
                  onChange={(event) => {
                    setPageSize(Number(event.target.value) as (typeof PAGE_SIZES)[number]);
                    resetPage();
                  }}
                  className="h-7 rounded-md border bg-background px-1 text-xs focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"
                >
                  {PAGE_SIZES.map((size) => (
                    <option key={size} value={size}>{size}</option>
                  ))}
                </select>
              </label>
              <div className="flex items-center gap-1">
                <button
                  type="button"
                  onClick={() => setPage((value) => Math.max(0, value - 1))}
                  disabled={pageResult.page === 0}
                  aria-label={t("marketPulse.previousPage", "上一页")}
                  title={t("marketPulse.previousPage", "上一页")}
                  className="rounded-md p-1 hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary disabled:cursor-not-allowed disabled:opacity-40"
                >
                  <ChevronLeft className="h-4 w-4" aria-hidden="true" />
                </button>
                <span className="min-w-10 text-center font-mono text-xs text-muted-foreground">
                  {pageResult.page + 1} / {pageResult.totalPages}
                </span>
                <button
                  type="button"
                  onClick={() => setPage((value) => Math.min(pageResult.totalPages - 1, value + 1))}
                  disabled={pageResult.page === pageResult.totalPages - 1}
                  aria-label={t("marketPulse.nextPage", "下一页")}
                  title={t("marketPulse.nextPage", "下一页")}
                  className="rounded-md p-1 hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary disabled:cursor-not-allowed disabled:opacity-40"
                >
                  <ChevronRight className="h-4 w-4" aria-hidden="true" />
                </button>
              </div>
            </div>
          )}
        </aside>

        <section
          aria-labelledby="market-pulse-detail-title"
          className="min-h-0 rounded-lg border bg-card p-4 lg:overflow-auto"
        >
          {selectedEvent ? (
            <>
              <div className="flex items-start justify-between gap-3 border-b pb-4">
                <div className="min-w-0">
                  <p className="text-xs text-muted-foreground">{t("marketPulse.detail", "事件详情")}</p>
                  <h2 id="market-pulse-detail-title" className="mt-1 text-lg font-semibold">
                    {selectedEvent.item.name}
                  </h2>
                  <p className="mt-0.5 font-mono text-sm text-muted-foreground">{selectedEvent.item.code}</p>
                </div>
                <span className={cn("shrink-0 rounded px-2 py-1 text-xs font-medium", eventBadgeTone(selectedEvent.item.changeType))}>
                  {selectedEvent.item.changeType}
                </span>
              </div>

              <dl className="divide-y text-sm">
                <DetailRow label={t("marketPulse.time", "发生时间")} value={selectedEvent.item.time} />
                <DetailRow
                  label={t("marketPulse.categoryLabel", "异动分类")}
                  value={t(
                    `marketPulse.category.${categorizeMarketPulse(selectedEvent.item)}`,
                    categoryLabels[categorizeMarketPulse(selectedEvent.item)],
                  )}
                />
                <DetailRow label={t("marketPulse.source", "数据来源")} value={selectedEvent.item.source} />
                <DetailRow
                  label={t("marketPulse.status", "数据状态")}
                  value={selectedEvent.item.stale ? t("marketPulse.staleTag", "过期") : t("marketPulse.current", "当前")}
                />
              </dl>

              <div className="pt-4">
                <h3 className="text-sm font-semibold">{t("marketPulse.descriptionLabel", "异动说明")}</h3>
                <p className={cn("mt-2 text-sm leading-6", eventTone(selectedEvent.item.changeType))}>
                  {selectedEvent.item.info || t("marketPulse.noInfo", "暂无补充说明")}
                </p>
              </div>
            </>
          ) : (
            <div className="flex h-full min-h-40 items-center justify-center text-center">
              <div>
                <h2 id="market-pulse-detail-title" className="text-sm font-semibold">
                  {t("marketPulse.noSelectionTitle", "暂无可查看的异动")}
                </h2>
                <p className="mt-1 text-sm text-muted-foreground">
                  {t("marketPulse.noSelectionDescription", "调整筛选条件或等待市场异动数据更新")}
                </p>
              </div>
            </div>
          )}
        </section>
      </div>
    </div>
  );
}

function SummaryCard({ label, value, tone }: { label: string; value: number; tone?: string }) {
  return (
    <div className="rounded-lg border bg-card px-3 py-2.5">
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className={cn("mt-1 font-mono text-lg font-semibold tabular-nums", tone)}>{value}</p>
    </div>
  );
}

function DetailRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center justify-between gap-3 py-3">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="text-right font-medium">{value}</dd>
    </div>
  );
}
