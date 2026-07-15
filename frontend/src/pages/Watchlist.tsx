import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import {
  AlertCircle,
  Eye,
  Trash2,
  Send,
  RefreshCw,
  CandlestickChart as CandlestickIcon,
  TrendingUp,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { useWatchlistStore } from "@/stores/watchlist";
import { useMarketDashboardStore } from "@/stores/marketDashboard";
import { CandlestickChart as CandlestickChartView } from "@/components/charts/CandlestickChart";
import type { PriceBar, QuoteData } from "@/lib/api";
import { toast } from "sonner";

/** A 股代码：6 位数字 */
const A_STOCK_RE = /^\d{6}$/;

/** 涨跌颜色：A 股惯例，上涨红，下跌绿 */
function changeColor(pct: number | null | undefined): string {
  if (pct == null) return "text-muted-foreground";
  if (pct > 0) return "text-red-500";
  if (pct < 0) return "text-green-500";
  return "text-muted-foreground";
}

/** 涨跌幅 chip：柔和背景色块，一眼可辨涨跌方向 */
function pctChipClass(pct: number | null | undefined): string {
  if (pct == null) return "text-muted-foreground";
  if (pct > 0) return "bg-red-500/10 text-red-500";
  if (pct < 0) return "bg-green-500/10 text-green-500";
  return "bg-muted text-muted-foreground";
}

function fmtPrice(v: number | null | undefined): string {
  if (v == null) return "—";
  return v.toFixed(2);
}

function fmtPct(v: number | null | undefined): string {
  if (v == null) return "—";
  const sign = v > 0 ? "+" : "";
  return `${sign}${v.toFixed(2)}%`;
}

function fmtAmt(v: number | null | undefined): string {
  if (v == null) return "—";
  const sign = v > 0 ? "+" : "";
  return `${sign}${v.toFixed(2)}`;
}

/** 表头行：真实表与骨架表共用，保证列宽一致 */
function HeaderRow({
  selectAllChecked,
  onSelectAll,
  labels,
}: {
  selectAllChecked: boolean;
  onSelectAll: () => void;
  labels: {
    selectAll: string;
    code: string;
    name: string;
    price: string;
    changeAmt: string;
    changePct: string;
    actions: string;
  };
}) {
  return (
    <tr className="text-xs font-medium text-muted-foreground">
      <th className="px-3 py-2.5 text-left w-8">
        <input
          type="checkbox"
          checked={selectAllChecked}
          onChange={onSelectAll}
          className="accent-primary"
          aria-label={labels.selectAll}
        />
      </th>
      <th className="px-3 py-2.5 text-left">{labels.code}</th>
      <th className="px-3 py-2.5 text-left">{labels.name}</th>
      <th className="px-3 py-2.5 text-right">{labels.price}</th>
      <th className="px-3 py-2.5 text-right">{labels.changeAmt}</th>
      <th className="px-3 py-2.5 text-right">{labels.changePct}</th>
      <th className="px-3 py-2.5 text-center">{labels.actions}</th>
    </tr>
  );
}

/** 骨架行：首次加载占位，避免空白闪烁 */
function SkeletonRow() {
  return (
    <tr className="border-t">
      <td className="px-3 py-3">
        <div className="h-3.5 w-3 rounded bg-muted/60 animate-pulse" />
      </td>
      {["w-14", "w-20", "w-16", "w-14", "w-16", "w-16"].map((w, i) => (
        <td key={i} className="px-3 py-3">
          <div className={cn("h-3.5 rounded bg-muted/60 animate-pulse", w)} />
        </td>
      ))}
    </tr>
  );
}

function StockDetailSection({
  code,
  name,
  bars,
  loading,
  error,
  onAnalyze,
}: {
  code: string | null;
  name: string | null;
  bars: PriceBar[];
  loading: boolean;
  error: string | null;
  onAnalyze: (code: string, name: string) => void;
}) {
  const { t } = useTranslation();

  if (!code) {
    return (
      <section className="rounded-lg border p-4 space-y-3 bg-card">
        <h2 className="text-sm font-semibold">{t("dashboard.stockDetail")}</h2>
        <div className="flex flex-col items-center justify-center gap-2 py-12 text-center">
          <TrendingUp className="h-6 w-6 text-muted-foreground/40" />
          <p className="text-xs text-muted-foreground">{t("dashboard.selectStockHint")}</p>
        </div>
      </section>
    );
  }

  return (
    <section className="rounded-lg border p-4 space-y-3 bg-card">
      <div className="flex items-center justify-between gap-3">
        <h2 className="text-sm font-semibold">
          {name || code} <span className="text-muted-foreground font-normal text-xs">({code})</span>
        </h2>
        <button
          type="button"
          onClick={() => onAnalyze(code, name || code)}
          className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md bg-primary text-primary-foreground text-xs font-medium hover:bg-primary/90 transition-colors"
          aria-label={t("dashboard.sendToAgent")}
        >
          <Send className="h-3 w-3" />
          {t("dashboard.sendToAgent")}
        </button>
      </div>

      {loading && <p className="text-xs text-muted-foreground">{t("dashboard.loading")}</p>}
      {error && (
        <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
          <AlertCircle className="h-3 w-3" />
          {error}
        </div>
      )}
      {!loading && !error && bars.length === 0 && (
        <p className="text-xs text-muted-foreground">{t("charts.noPriceData")}</p>
      )}
      {bars.length > 0 && <CandlestickChartView data={bars} height={440} defaultRange="3M" />}
    </section>
  );
}

export default function WatchlistPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();

  const {
    stocks,
    quotes,
    selected,
    loading,
    error,
    refresh,
    add,
    remove,
    refreshQuotes,
    toggleSelection,
    clearSelection,
  } = useWatchlistStore();

  const [inputCode, setInputCode] = useState("");
  const [inputError, setInputError] = useState("");
  const [adding, setAdding] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  const pollingRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const selectedCode = useMarketDashboardStore((s) => s.selectedCode);
  const selectedBars = useMarketDashboardStore((s) => s.selectedBars);
  const barsLoading = useMarketDashboardStore((s) => s.barsLoading);
  const barsError = useMarketDashboardStore((s) => s.barsError);
  const setSelectedCode = useMarketDashboardStore((s) => s.setSelectedCode);
  const activeSelectedCode = selectedCode && stocks.some((stock) => stock.code === selectedCode)
    ? selectedCode
    : null;

  // 初始加载
  useEffect(() => {
    refresh();
  }, [refresh]);

  // 行情轮询：3s，Page Visibility API 暂停/恢复
  useEffect(() => {
    if (stocks.length === 0) return;

    function startPolling() {
      refreshQuotes();
      pollingRef.current = setInterval(() => {
        if (!document.hidden) refreshQuotes();
      }, 3000);
    }

    function handleVisibility() {
      if (!document.hidden && stocks.length > 0) {
        // 切回立即刷一次
        refreshQuotes();
      }
    }

    startPolling();
    document.addEventListener("visibilitychange", handleVisibility);

    return () => {
      if (pollingRef.current) clearInterval(pollingRef.current);
      document.removeEventListener("visibilitychange", handleVisibility);
    };
  }, [stocks.length, refreshQuotes]);

  async function handleAdd(e: React.FormEvent) {
    e.preventDefault();
    const code = inputCode.trim();
    if (!A_STOCK_RE.test(code)) {
      setInputError(t("watchlist.invalidCode", "请输入 6 位数字股票代码"));
      return;
    }
    setInputError("");
    setAdding(true);
    try {
      const result = await add(code);
      if (result.exists) {
        toast.info(
          t("watchlist.alreadyAdded", "{{code}} 已在自选股中", { code }),
        );
      } else {
        setInputCode("");
        toast.success(t("watchlist.added", "已添加 {{code}}", { code }));
      }
    } catch {
      toast.error(t("watchlist.addError", "添加失败，请稍后重试"));
    } finally {
      setAdding(false);
    }
  }

  async function handleDelete(code: string) {
    if (confirmDelete !== code) {
      setConfirmDelete(code);
      return;
    }
    setConfirmDelete(null);
    try {
      await remove(code);
      toast.success(t("watchlist.removed", "已删除 {{code}}", { code }));
    } catch {
      toast.error(t("watchlist.removeError", "删除失败，请稍后重试"));
    }
  }

  function handleSelectAll() {
    if (selected.size === stocks.length) clearSelection();
    else
      stocks.forEach((s) => {
        if (!selected.has(s.code)) toggleSelection(s.code);
      });
  }

  function handleSendToAgent() {
    const selectedStocks = stocks.filter((s) => selected.has(s.code));
    if (selectedStocks.length === 0) return;
    const names = selectedStocks.map((s) => {
      const q = quotes[s.code];
      return q?.name ? `${s.code} ${q.name}` : s.code;
    });
    const prefill =
      selectedStocks.length === 1
        ? `帮我分析 ${names[0]} 的走势和投资机会`
        : `帮我分析以下股票的走势和投资机会：${names.join("、")}`;
    clearSelection();
    navigate(`/agent?prefill=${encodeURIComponent(prefill)}`);
  }

  function handleAnalyze(code: string, name: string) {
    const message = `请分析 ${name} (${code}) 的近期走势和技术指标`;
    navigate(`/agent?prefill=${encodeURIComponent(message)}`);
  }

  const colLabels = {
    selectAll: t("watchlist.selectAll", "全选"),
    code: t("watchlist.col.code", "代码"),
    name: t("watchlist.col.name", "名称"),
    price: t("watchlist.col.price", "最新价"),
    changeAmt: t("watchlist.col.changeAmt", "涨跌额"),
    changePct: t("watchlist.col.changePct", "涨跌幅"),
    actions: t("watchlist.col.actions", "操作"),
  };
  const allSelected = selected.size === stocks.length && stocks.length > 0;
  const showSkeleton = loading && stocks.length === 0;

  return (
    <div className="flex h-full w-full flex-col gap-4 p-4">
      {/* Header */}
      <header className="flex items-center justify-between">
        <div className="flex items-baseline gap-2.5">
          <h1 className="text-xl font-semibold tracking-tight">
            {t("watchlist.title", "A股自选")}
          </h1>
          {stocks.length > 0 && (
            <span className="text-xs font-medium tabular-nums text-muted-foreground bg-muted rounded-full px-2 py-0.5">
              {stocks.length}
            </span>
          )}
        </div>
        <button
          onClick={() => {
            refresh();
            refreshQuotes();
          }}
          className="p-1.5 rounded-lg text-muted-foreground hover:text-foreground hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40 transition-colors"
          title={t("watchlist.refresh", "刷新")}
          aria-label={t("watchlist.refresh", "刷新")}
        >
          <RefreshCw size={16} className={cn(loading && "animate-spin")} />
        </button>
      </header>

      {/* Add Form */}
      <form onSubmit={handleAdd} className="flex gap-2 items-start">
        <div className="flex flex-col gap-1">
          <input
            type="text"
            value={inputCode}
            onChange={(e) => {
              setInputCode(e.target.value);
              setInputError("");
            }}
            placeholder={t("watchlist.placeholder", "输入 6 位股票代码")}
            maxLength={6}
            inputMode="numeric"
            className="border rounded-lg px-3 py-2 text-sm w-44 bg-background placeholder:text-muted-foreground/70 focus:outline-none focus:ring-2 focus:ring-primary/30 focus:border-primary transition-shadow"
            aria-label={t("watchlist.placeholder", "输入 6 位股票代码")}
            aria-invalid={!!inputError}
          />
          {inputError && (
            <span className="flex items-center gap-1 text-xs text-red-500">
              <AlertCircle size={12} />
              {inputError}
            </span>
          )}
        </div>
        <button
          type="submit"
          disabled={adding}
          className="px-3.5 py-2 text-sm rounded-lg bg-primary text-primary-foreground hover:bg-primary/90 disabled:opacity-50 disabled:cursor-not-allowed focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40 focus-visible:ring-offset-1 focus-visible:ring-offset-background transition-colors"
        >
          {adding
            ? t("watchlist.adding", "添加中…")
            : t("watchlist.add", "添加")}
        </button>
      </form>

      {/* Error */}
      {error && (
        <div className="flex items-center gap-2 text-sm text-destructive bg-destructive/5 border border-destructive/20 rounded-lg px-3 py-2">
          <AlertCircle size={14} className="shrink-0" />
          <span>{error}</span>
        </div>
      )}

      {/* Skeleton：首次加载 */}
      {showSkeleton && (
        <div className="overflow-hidden rounded-lg border">
          <table className="w-full text-sm">
            <thead className="bg-muted/40">
              <HeaderRow
                selectAllChecked={false}
                onSelectAll={() => {}}
                labels={colLabels}
              />
            </thead>
            <tbody>
              {Array.from({ length: 4 }).map((_, i) => (
                <SkeletonRow key={i} />
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Empty State */}
      {!loading && stocks.length === 0 && (
        <div className="flex flex-col items-center justify-center flex-1 gap-3 py-16 text-center">
          <div className="flex items-center justify-center w-16 h-16 rounded-full bg-muted">
            <Eye size={28} strokeWidth={1.5} className="text-muted-foreground" />
          </div>
          <div className="space-y-1.5">
            <p className="text-base font-medium">
              {t("watchlist.empty", "暂无自选股，请添加股票代码")}
            </p>
            <p className="text-sm text-muted-foreground max-w-xs">
              {t(
                "watchlist.emptyHint",
                "盯盘你关注的股票，涨跌实时刷新",
              )}
            </p>
          </div>
        </div>
      )}

      {/* Quotes Table */}
      {stocks.length > 0 && (
        <div data-testid="watchlist-workspace" className="grid min-h-0 flex-1 gap-4 lg:grid-cols-[minmax(24rem,0.85fr)_minmax(0,1.35fr)] lg:gap-6">
          <section data-testid="watchlist-chart-panel" className="order-2 min-w-0 lg:order-2">
            <StockDetailSection
              code={activeSelectedCode}
              name={activeSelectedCode ? quotes[activeSelectedCode]?.name ?? null : null}
              bars={selectedBars}
              loading={barsLoading}
              error={barsError}
              onAnalyze={handleAnalyze}
            />
          </section>

          <aside data-testid="watchlist-list-panel" className="order-1 min-w-0 space-y-3 lg:order-1">
            <div className="overflow-x-auto rounded-lg border">
              <table className="w-full text-sm">
                <thead className="bg-muted/40">
                  <HeaderRow
                    selectAllChecked={allSelected}
                    onSelectAll={handleSelectAll}
                    labels={colLabels}
                  />
                </thead>
                <tbody>
                  {stocks.map((stock) => {
                    const q: QuoteData | undefined = quotes[stock.code];
                    const pct = q?.change_pct;
                    const isSelected = selected.has(stock.code);
                    return (
                      <tr
                        key={stock.code}
                        title={q?.stale ? t("watchlist.stale", "行情延迟") : undefined}
                        className={cn(
                          "border-t transition-colors cursor-pointer hover:bg-muted/40",
                          isSelected && "bg-primary/[0.06]",
                          activeSelectedCode === stock.code && "ring-1 ring-inset ring-primary/40",
                          q?.stale && "opacity-50",
                        )}
                        onClick={() => void setSelectedCode(stock.code)}
                      >
                        <td className="px-3 py-2.5" onClick={(e) => e.stopPropagation()}>
                          <input
                            type="checkbox"
                            checked={isSelected}
                            onChange={() => toggleSelection(stock.code)}
                            className="accent-primary"
                            aria-label={`${t("watchlist.select", "选择")} ${stock.code}`}
                          />
                        </td>
                        <td className="px-3 py-2.5 font-mono tabular-nums">{stock.code}</td>
                        <td className="px-3 py-2.5 max-w-[8rem] truncate">{q?.name ?? stock.name ?? "—"}</td>
                        <td className={cn("px-3 py-2.5 text-right font-mono tabular-nums", changeColor(pct))}>{fmtPrice(q?.price)}</td>
                        <td className={cn("px-3 py-2.5 text-right font-mono tabular-nums", changeColor(pct))}>{fmtAmt(q?.change_amt)}</td>
                        <td className="px-3 py-2.5 text-right">
                          <span className={cn("inline-block min-w-[3.75rem] px-1.5 py-0.5 rounded text-xs font-mono tabular-nums text-right", pctChipClass(pct))}>{fmtPct(pct)}</span>
                        </td>
                        <td className="px-3 py-2.5 text-center" onClick={(e) => e.stopPropagation()}>
                          {confirmDelete === stock.code ? (
                            <span className="flex items-center gap-1 justify-center">
                              <button onClick={() => handleDelete(stock.code)} className="text-xs font-medium text-red-600 hover:text-red-700 px-2 py-1 rounded hover:bg-red-500/10 transition-colors" data-testid={`confirm-delete-${stock.code}`}>
                                {t("watchlist.confirmDelete", "确认删除")}
                              </button>
                              <button onClick={() => setConfirmDelete(null)} className="text-xs text-muted-foreground hover:text-foreground px-2 py-1 rounded hover:bg-muted/60 transition-colors">
                                {t("watchlist.cancelDelete", "取消")}
                              </button>
                            </span>
                          ) : (
                            <span className="flex items-center gap-1 justify-center">
                              <a href={`https://stockpage.10jqka.com.cn/${stock.code}`} target="_blank" rel="noopener noreferrer" className="p-1.5 rounded-md text-muted-foreground hover:text-foreground hover:bg-muted/60 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40 transition-colors inline-flex" title={t("watchlist.kline", "同花顺 K 线")} aria-label={t("watchlist.kline", "同花顺 K 线")} data-testid={`kline-${stock.code}`}>
                                <CandlestickIcon size={14} />
                              </a>
                              <button onClick={() => handleDelete(stock.code)} className="p-1.5 rounded-md text-muted-foreground hover:text-red-500 hover:bg-red-500/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40 transition-colors" title={t("watchlist.delete", "删除")} aria-label={t("watchlist.delete", "删除")} data-testid={`delete-${stock.code}`}>
                                <Trash2 size={14} />
                              </button>
                            </span>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>

            {selected.size > 0 && (
              <div className="flex justify-end">
                <button onClick={handleSendToAgent} className="flex items-center gap-2 px-4 py-2.5 text-sm rounded-lg bg-primary text-primary-foreground hover:bg-primary/90 shadow-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40 focus-visible:ring-offset-1 focus-visible:ring-offset-background transition-colors" data-testid="send-to-agent">
                  <Send size={14} />
                  <span>{t("watchlist.sendToAgent", "发给 Agent 分析")}</span>
                  <span className="inline-flex items-center justify-center min-w-[1.25rem] h-5 px-1 rounded-full bg-primary-foreground/25 text-xs font-medium tabular-nums">{selected.size}</span>
                </button>
              </div>
            )}
          </aside>
        </div>
      )}
    </div>
  );
}
