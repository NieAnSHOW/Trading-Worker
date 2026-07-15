import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { MarketPulseItem } from "@/lib/stockSdk";
import { MarketPulsePanel } from "../MarketPulsePanel";

const storeState = vi.hoisted(() => ({
  pulse: [] as MarketPulseItem[],
  pulseLoading: false,
  pulseError: null as string | null,
  pulseAsOf: null as string | null,
  pulseStale: false,
  refreshPulse: vi.fn<() => Promise<void>>(),
}));

vi.mock("@/stores/marketDashboard", () => ({
  useMarketDashboardStore: <T,>(selector: (state: typeof storeState) => T) => selector(storeState),
}));

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string, fallback?: string, values?: Record<string, string | number>) => {
      if (!fallback) return key;
      return Object.entries(values ?? {}).reduce(
        (result, [name, value]) => result.replace(`{{${name}}}`, String(value)),
        fallback,
      );
    },
  }),
}));

const limitUpItem: MarketPulseItem = {
  code: "600519",
  name: "贵州茅台",
  time: "10:00",
  changeType: "封涨停板",
  info: "白酒板块走强",
  source: "test",
  stale: false,
};

const upwardItem: MarketPulseItem = {
  code: "000001",
  name: "平安银行",
  time: "10:01",
  changeType: "火箭发射",
  info: "买盘活跃",
  source: "test",
  stale: false,
};

const downwardItem: MarketPulseItem = {
  ...upwardItem,
  code: "600000",
  name: "浦发银行",
  time: "10:02",
  changeType: "高台跳水",
  info: "卖盘活跃",
};

const limitDownBrokenItem: MarketPulseItem = {
  ...upwardItem,
  code: "600001",
  name: "邯郸钢铁",
  time: "10:03",
  changeType: "打开涨停板",
  info: "封板打开",
};

function renderPanel(overrides: Partial<typeof storeState> = {}) {
  Object.assign(storeState, overrides);
  return render(<MarketPulsePanel />);
}

describe("MarketPulsePanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    Object.assign(storeState, {
      pulse: [],
      pulseLoading: false,
      pulseError: null,
      pulseAsOf: null,
      pulseStale: false,
      refreshPulse: vi.fn().mockResolvedValue(undefined),
    });
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      value: "visible",
    });
  });

  it("selects the first matching event detail when a category filter changes", async () => {
    const user = userEvent.setup();
    renderPanel({ pulse: [limitUpItem, upwardItem] });

    expect(await screen.findByRole("heading", { name: /贵州茅台/i })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /上涨异动/i }));

    expect(await screen.findByRole("heading", { name: /平安银行/i })).toBeInTheDocument();
  });

  it("updates selected-event detail when a different list row is selected", async () => {
    const user = userEvent.setup();
    renderPanel({ pulse: [limitUpItem, upwardItem] });

    await user.click(screen.getByRole("button", { name: /平安银行 000001/i }));

    expect(await screen.findByRole("heading", { name: /平安银行/i })).toBeInTheDocument();
  });

  it("uses the approved page size default and paginates the final event", async () => {
    const user = userEvent.setup();
    const paginatedItems = Array.from({ length: 201 }, (_, index) => ({
      ...upwardItem,
      code: String(index).padStart(6, "0"),
      name: `测试股票${index + 1}`,
      time: `10:${String(index % 60).padStart(2, "0")}`,
    }));
    renderPanel({ pulse: paginatedItems });

    const pageSize = screen.getByLabelText("每页数量");
    expect(pageSize).toHaveValue("200");
    expect(Array.from(pageSize.querySelectorAll("option"), ({ value }) => value)).toEqual(["200", "500", "1000"]);

    await user.click(screen.getByRole("button", { name: /下一页/i }));

    expect(screen.getByText("2 / 2")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /测试股票201 000200/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /下一页/i })).toBeDisabled();
  });

  it("combines downward and limit-down-broken events in the decline summary", () => {
    renderPanel({ pulse: [downwardItem, limitDownBrokenItem] });

    expect(screen.getByText("跌停/炸板").parentElement).toHaveTextContent("2");
  });

  it("keeps active filters after manual refresh", async () => {
    const user = userEvent.setup();
    const refreshPulse = vi.fn().mockResolvedValue(undefined);
    renderPanel({ pulse: [upwardItem], refreshPulse });
    refreshPulse.mockClear();

    await user.type(screen.getByRole("searchbox", { name: /搜索市场异动/i }), "平安");
    await user.click(screen.getByRole("button", { name: /刷新市场异动/i }));

    expect(refreshPulse).toHaveBeenCalledOnce();
    expect(screen.getByRole("searchbox", { name: /搜索市场异动/i })).toHaveValue("平安");
  });

  it("refreshes on mount and while visible, then cleans up polling", () => {
    vi.useFakeTimers();
    const refreshPulse = vi.fn().mockResolvedValue(undefined);
    const { unmount } = renderPanel({ refreshPulse });

    expect(refreshPulse).toHaveBeenCalledTimes(1);

    act(() => {
      vi.advanceTimersByTime(15_000);
    });
    expect(refreshPulse).toHaveBeenCalledTimes(2);

    Object.defineProperty(document, "visibilityState", { configurable: true, value: "hidden" });
    act(() => {
      document.dispatchEvent(new Event("visibilitychange"));
      vi.advanceTimersByTime(15_000);
    });
    expect(refreshPulse).toHaveBeenCalledTimes(2);

    Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
    act(() => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    expect(refreshPulse).toHaveBeenCalledTimes(3);

    unmount();
    act(() => {
      vi.advanceTimersByTime(15_000);
    });
    expect(refreshPulse).toHaveBeenCalledTimes(3);
    vi.useRealTimers();
  });

  it("marks retained rows as stale when refresh reports an error", () => {
    renderPanel({ pulse: [upwardItem], pulseError: "provider unavailable", pulseStale: true });

    expect(screen.getByRole("alert")).toHaveTextContent("provider unavailable");
    expect(screen.getByRole("button", { name: /平安银行 000001/i })).toBeInTheDocument();
  });

  it("offers retry for an empty first-load error", async () => {
    const user = userEvent.setup();
    const refreshPulse = vi.fn().mockResolvedValue(undefined);
    renderPanel({ pulseError: "provider unavailable", refreshPulse });
    refreshPulse.mockClear();

    expect(screen.getByRole("alert")).toHaveTextContent("provider unavailable");
    await user.click(screen.getByRole("button", { name: /重试/i }));
    expect(refreshPulse).toHaveBeenCalledOnce();
  });

  it("shows an empty provider state when no events are available", () => {
    renderPanel();

    expect(screen.getByText("暂无市场异动")).toBeInTheDocument();
  });

  it("clears filters after an empty filtered result", async () => {
    const user = userEvent.setup();
    renderPanel({ pulse: [upwardItem] });

    await user.type(screen.getByRole("searchbox", { name: /搜索市场异动/i }), "不存在");

    expect(screen.getByText("没有匹配的市场异动")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /清除筛选/i }));
    expect(screen.getByRole("button", { name: /平安银行 000001/i })).toBeInTheDocument();
  });

  it("keeps retained rows visible while a refresh is loading", async () => {
    renderPanel({ pulse: [upwardItem], pulseLoading: true });

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /平安银行 000001/i })).toBeInTheDocument();
    });
    expect(screen.getByRole("status")).toHaveTextContent("正在更新市场异动");
  });
});
