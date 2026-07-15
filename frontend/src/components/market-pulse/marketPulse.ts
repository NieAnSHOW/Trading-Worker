import type { MarketPulseItem } from "@/lib/stockSdk";

export type MarketPulseCategory =
  | "all"
  | "limitDownBroken"
  | "limitUp"
  | "upward"
  | "downward"
  | "turnoverCapital"
  | "other";

export interface MarketPulseFilters {
  category: MarketPulseCategory;
  query: string;
  hideStale: boolean;
}

const CATEGORY_RULES: Array<[Exclude<MarketPulseCategory, "all" | "other">, string[]]> = [
  ["limitDownBroken", ["打开涨停", "炸板", "封跌停", "跌停"]],
  ["limitUp", ["封涨停", "涨停", "封板"]],
  [
    "upward",
    ["火箭发射", "快速反弹", "大笔买入", "有大买盘", "竞价上涨", "高开", "向上缺口", "新高", "拉升", "上涨", "大涨"],
  ],
  [
    "downward",
    ["加速下跌", "高台跳水", "大笔卖出", "有大卖盘", "竞价下跌", "低开", "向下缺口", "新低", "跳水", "下跌", "大跌"],
  ],
  ["turnoverCapital", ["放量", "缩量", "成交", "换手", "资金", "净流入", "净流出"]],
];

export function categorizeMarketPulse(item: MarketPulseItem): MarketPulseCategory {
  const label = item.changeType.trim();
  return CATEGORY_RULES.find(([, words]) => words.some((word) => label.includes(word)))?.[0] ?? "other";
}

export function filterMarketPulse(items: readonly MarketPulseItem[], filters: MarketPulseFilters): MarketPulseItem[] {
  const query = filters.query.trim().toLowerCase();

  return items.filter((item) => {
    if (filters.category !== "all" && categorizeMarketPulse(item) !== filters.category) {
      return false;
    }

    if (filters.hideStale && item.stale) {
      return false;
    }

    return !query || item.name.trim().toLowerCase().includes(query) || item.code.trim().toLowerCase().includes(query);
  });
}

export function countMarketPulseCategories(items: readonly MarketPulseItem[]): Record<MarketPulseCategory, number> {
  const counts: Record<MarketPulseCategory, number> = {
    all: items.length,
    limitDownBroken: 0,
    limitUp: 0,
    upward: 0,
    downward: 0,
    turnoverCapital: 0,
    other: 0,
  };

  items.forEach((item) => {
    counts[categorizeMarketPulse(item)] += 1;
  });

  return counts;
}

export interface PulsePage {
  items: MarketPulseItem[];
  page: number;
  totalPages: number;
}

export function getPulsePage(items: readonly MarketPulseItem[], page: number, pageSize: number): PulsePage {
  const normalizedPageSize = Math.floor(pageSize);
  const safePageSize = Number.isFinite(normalizedPageSize) && normalizedPageSize > 0 ? normalizedPageSize : 1;
  const totalPages = Math.max(1, Math.ceil(items.length / safePageSize));
  const safePage = Math.min(Math.max(0, Number.isFinite(page) ? Math.floor(page) : 0), totalPages - 1);
  const start = safePage * safePageSize;

  return { items: items.slice(start, start + safePageSize), page: safePage, totalPages };
}

export function pulseItemKey(item: MarketPulseItem, index: number): string {
  return `${item.code}-${item.time}-${item.changeType}-${index}`;
}
