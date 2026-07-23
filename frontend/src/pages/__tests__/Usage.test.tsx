import { StrictMode } from "react";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { api } from "@/lib/api";
import { echarts } from "@/lib/echarts";
import i18n from "@/i18n";
import { Usage } from "../Usage";

const setOption = vi.fn();
const dispose = vi.fn();
const resize = vi.fn();

vi.mock("@/lib/echarts", () => ({
  echarts: {
    init: vi.fn(() => ({ setOption, dispose, resize })),
  },
}));

vi.mock("@/hooks/useDarkMode", () => ({
  useDarkMode: () => ({ dark: false }),
}));

vi.mock("@/lib/api", () => ({
  api: { getLLMUsage: vi.fn() },
}));

const usageResponse = {
  generated_at: "2026-07-23T04:00:00Z",
  timezone: "Asia/Shanghai",
  period: { start_at: null, end_at: null },
  totals: {
    input_tokens: 700,
    output_tokens: 300,
    total_tokens: 1000,
    calls: 4,
    runs: 2,
    sessions: 2,
    missing_usage_runs: 0,
    invalid_usage_runs: 0,
    cache_read_tokens: 80,
    cache_write_tokens: 20,
    cache_read_reported_runs: 1,
    cache_write_reported_runs: 1,
  },
  trend: [
    { date: "2026-07-22", totals: { input_tokens: 300, output_tokens: 100, total_tokens: 400, calls: 1 } },
    { date: "2026-07-23", totals: { input_tokens: 400, output_tokens: 200, total_tokens: 600, calls: 3 } },
  ],
  breakdown: [
    { provider: "openai", model: "gpt-5", totals: { input_tokens: 400, output_tokens: 200, total_tokens: 600, calls: 3 } },
    { provider: "anthropic", model: "claude", totals: { input_tokens: 300, output_tokens: 100, total_tokens: 400, calls: 1 } },
  ],
  sessions: {
    items: [
      {
        session_id: "session-a",
        title: "会话 A",
        last_run_at: "2026-07-23T03:00:00Z",
        totals: { input_tokens: 400, output_tokens: 200, total_tokens: 600, calls: 3 },
        runs: [{
          run_id: "run-a",
          occurred_at: "2026-07-23T02:00:00Z",
          provider: "openai",
          model: "gpt-5",
          metering_eligible: true,
          totals: { input_tokens: 400, output_tokens: 200, total_tokens: 600, calls: 3 },
        }],
      },
      {
        session_id: "session-b",
        title: "会话 B",
        last_run_at: "2026-07-22T03:00:00Z",
        totals: { input_tokens: 300, output_tokens: 100, total_tokens: 400, calls: 1 },
        runs: [],
      },
    ],
    page: 1,
    page_size: 25,
    total_items: 26,
    total_pages: 2,
  },
};

const getLLMUsage = vi.mocked(api.getLLMUsage);
const init = vi.mocked(echarts.init);

function renderUsage() {
  return render(<MemoryRouter><Usage /></MemoryRouter>);
}

describe("Usage", () => {
  beforeEach(() => {
    i18n.changeLanguage("zh-CN");
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.setSystemTime(new Date("2026-07-23T04:00:00Z"));
    vi.spyOn(Intl.DateTimeFormat.prototype, "resolvedOptions").mockReturnValue({
      timeZone: "Asia/Shanghai",
    } as Intl.ResolvedDateTimeFormatOptions);
    getLLMUsage.mockReset();
    getLLMUsage.mockResolvedValue(structuredClone(usageResponse));
    setOption.mockClear();
    dispose.mockClear();
    resize.mockClear();
    init.mockClear();
    document.documentElement.classList.remove("dark");
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("loads all-time usage once in the browser timezone and expands safe run links", async () => {
    renderUsage();

    await waitFor(() => expect(getLLMUsage).toHaveBeenCalledWith({
      timezone: "Asia/Shanghai", page: 1, page_size: 25,
    }));
    expect(getLLMUsage).toHaveBeenCalledTimes(1);
    expect(screen.getByText("1,000")).toBeInTheDocument();

    await userEvent.setup({ advanceTimers: vi.advanceTimersByTime }).click(
      screen.getByRole("button", { name: /会话 A/i }),
    );
    expect(screen.getByRole("link", { name: /run-a/i })).toHaveAttribute("href", "/runs/run-a");
  });

  it("deduplicates the initial aggregate request under StrictMode", async () => {
    render(<StrictMode><MemoryRouter><Usage /></MemoryRouter></StrictMode>);

    await waitFor(() => expect(getLLMUsage).toHaveBeenCalled());
    expect(getLLMUsage).toHaveBeenCalledTimes(1);
  });

  it.each([
    ["近 7 天", "2026-07-16T04:00:00.000Z", "2026-07-23T04:00:00.000Z"],
    ["近 30 天", "2026-06-23T04:00:00.000Z", "2026-07-23T04:00:00.000Z"],
    ["本月", "2026-06-30T16:00:00.000Z", "2026-07-23T04:00:00.000Z"],
  ])("submits the %s UTC boundaries", async (label, start_at, end_at) => {
    renderUsage();
    await screen.findByText("1,000");
    getLLMUsage.mockClear();

    await userEvent.setup({ advanceTimers: vi.advanceTimersByTime }).click(screen.getByRole("button", { name: label }));

    await waitFor(() => expect(getLLMUsage).toHaveBeenCalledWith({
      timezone: "Asia/Shanghai",
      page: 1,
      page_size: 25,
      start_at: expect.stringMatching(`^${start_at.slice(0, 19)}`),
      end_at: expect.stringMatching(`^${end_at.slice(0, 19)}`),
    }));
  });

  it("uses local calendar midnights and an exclusive next-day custom end", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderUsage();
    await screen.findByText("1,000");
    getLLMUsage.mockClear();

    await user.click(screen.getByRole("button", { name: "自定义" }));
    await user.type(screen.getByLabelText("开始日期"), "2026-07-01");
    await user.type(screen.getByLabelText("结束日期"), "2026-07-02");
    await user.click(screen.getByRole("button", { name: "应用" }));

    await waitFor(() => expect(getLLMUsage).toHaveBeenCalledWith({
      timezone: "Asia/Shanghai",
      page: 1,
      page_size: 25,
      start_at: "2026-06-30T16:00:00.000Z",
      end_at: "2026-07-02T16:00:00.000Z",
    }));
  });

  it("searches and paginates session details without replacing aggregate totals", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderUsage();
    await screen.findByText("1,000");
    getLLMUsage.mockClear();

    await user.type(screen.getByRole("searchbox", { name: "搜索会话" }), "会话 A");
    await user.keyboard("{Enter}");
    await waitFor(() => expect(getLLMUsage).toHaveBeenCalledWith(expect.objectContaining({ query: "会话 A", page: 1 })));
    expect(screen.getByText("1,000")).toBeInTheDocument();

    getLLMUsage.mockClear();
    await user.click(screen.getByRole("button", { name: "下一页" }));
    await waitFor(() => expect(getLLMUsage).toHaveBeenCalledWith(expect.objectContaining({ query: "会话 A", page: 2 })));
    expect(screen.getByText("1,000")).toBeInTheDocument();
  });

  it("keeps the snapshot visible during refresh and records successful refresh time", async () => {
    let resolveRefresh!: (value: typeof usageResponse) => void;
    const pending = new Promise<typeof usageResponse>((resolve) => { resolveRefresh = resolve; });
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderUsage();
    await screen.findByText("1,000");
    getLLMUsage.mockImplementationOnce(() => pending);

    await user.click(screen.getByRole("button", { name: "刷新" }));
    expect(screen.getByText("1,000")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "刷新中" })).toBeDisabled();

    await act(async () => resolveRefresh({ ...usageResponse, totals: { ...usageResponse.totals, total_tokens: 1200 } }));
    expect(await screen.findByText("1,200")).toBeInTheDocument();
    expect(screen.getByText(/更新于/)).toBeInTheDocument();
  });

  it("ignores an older filter response that resolves after the latest request", async () => {
    let resolveOlder!: (value: typeof usageResponse) => void;
    let resolveLatest!: (value: typeof usageResponse) => void;
    const older = new Promise<typeof usageResponse>((resolve) => { resolveOlder = resolve; });
    const latest = new Promise<typeof usageResponse>((resolve) => { resolveLatest = resolve; });
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderUsage();
    await screen.findByText("1,000");
    getLLMUsage.mockImplementationOnce(() => older).mockImplementationOnce(() => latest);

    await user.click(screen.getByRole("button", { name: "近 7 天" }));
    await user.click(screen.getByRole("button", { name: "近 30 天" }));
    await act(async () => resolveLatest({ ...usageResponse, totals: { ...usageResponse.totals, total_tokens: 3000 } }));
    expect(await screen.findByText("3,000")).toBeInTheDocument();

    await act(async () => resolveOlder({ ...usageResponse, totals: { ...usageResponse.totals, total_tokens: 7000 } }));
    expect(screen.getByText("3,000")).toBeInTheDocument();
    expect(screen.queryByText("7,000")).not.toBeInTheDocument();
  });

  it("retries an initial failure and preserves an existing snapshot after refresh failure", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    getLLMUsage.mockRejectedValueOnce(new Error("offline")).mockResolvedValueOnce(structuredClone(usageResponse));
    renderUsage();

    expect(await screen.findByText("offline")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "重试" }));
    expect(await screen.findByText("1,000")).toBeInTheDocument();

    getLLMUsage.mockRejectedValueOnce(new Error("refresh failed"));
    await user.click(screen.getByRole("button", { name: "刷新" }));
    expect(await screen.findByText("refresh failed")).toBeInTheDocument();
    expect(screen.getByText("1,000")).toBeInTheDocument();
  });

  it("shows empty and partial-data states without calling missing cache values zero", async () => {
    getLLMUsage.mockResolvedValueOnce({
      ...structuredClone(usageResponse),
      totals: {
        ...usageResponse.totals,
        runs: 0,
        missing_usage_runs: 2,
        invalid_usage_runs: 1,
        cache_read_tokens: undefined,
        cache_write_tokens: undefined,
        cache_read_reported_runs: 0,
        cache_write_reported_runs: 0,
      },
      sessions: { ...usageResponse.sessions, items: [], total_items: 0, total_pages: 0 },
    });
    renderUsage();

    expect(await screen.findByText(/无供应商报告的用量/)).toBeInTheDocument();
    expect(screen.getByText(/部分运行数据不可用/)).toBeInTheDocument();
    expect(screen.getByText(/缺少用量数据的运行: 2/)).toBeInTheDocument();
    expect(screen.getByText(/用量数据无效的运行: 1/)).toBeInTheDocument();
    expect(screen.getAllByText("未提供").length).toBeGreaterThanOrEqual(2);
  });

  it("renders accessible charts with API-provided trend and sorted breakdown data and disposes them", async () => {
    const { unmount } = renderUsage();
    await screen.findByText("1,000");

    expect(screen.getByRole("img", { name: "Token 趋势" })).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "供应商和模型分布" })).toBeInTheDocument();
    expect(setOption).toHaveBeenCalledWith(expect.objectContaining({
      xAxis: expect.objectContaining({ data: ["2026-07-22", "2026-07-23"] }),
      series: [expect.objectContaining({ data: [400, 600] })],
    }));
    expect(setOption).toHaveBeenCalledWith(expect.objectContaining({
      yAxis: expect.objectContaining({ data: ["openai / gpt-5", "anthropic / claude"] }),
      series: [expect.objectContaining({ data: [600, 400] })],
    }));

    unmount();
    expect(dispose).toHaveBeenCalledTimes(2);
  });

  it("rebuilds and cleans up both charts when the root dark class changes", async () => {
    const disconnect = vi.spyOn(ResizeObserver.prototype, "disconnect");
    renderUsage();
    await screen.findByText("1,000");
    expect(init).toHaveBeenCalledTimes(2);

    await act(async () => {
      document.documentElement.classList.add("dark");
      await Promise.resolve();
    });

    await waitFor(() => expect(init).toHaveBeenCalledTimes(4));
    expect(dispose).toHaveBeenCalledTimes(2);
    expect(disconnect).toHaveBeenCalledTimes(2);
  });

  it("renders sessions in vertically safe rows on narrow screens", async () => {
    const { container } = renderUsage();
    await screen.findByText("1,000");

    const sessionList = screen.getByRole("region", { name: "会话明细" });
    expect(within(sessionList).getByRole("button", { name: /会话 A/i })).toHaveClass("flex-col", "sm:flex-row");
    expect(container.querySelector(".overflow-x-auto")).not.toBeInTheDocument();
  });
});
