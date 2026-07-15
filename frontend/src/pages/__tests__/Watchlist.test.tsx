import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, act } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import WatchlistPage from "../Watchlist";

// Mock i18n
vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (k: string, d?: string | object) => typeof d === "string" ? d : k }),
}));

// Mock sonner
vi.mock("sonner", () => ({ toast: { info: vi.fn(), success: vi.fn(), error: vi.fn() } }));

// Mock navigate
const mockNavigate = vi.fn();
vi.mock("react-router-dom", async (orig) => {
  const actual = await orig<typeof import("react-router-dom")>();
  return { ...actual, useNavigate: () => mockNavigate };
});

// Mock stores
vi.mock("@/stores/watchlist");
import { useWatchlistStore } from "@/stores/watchlist";
const mockStore = vi.mocked(useWatchlistStore);

vi.mock("@/stores/marketDashboard", () => ({
  useMarketDashboardStore: vi.fn(),
}));
import { useMarketDashboardStore } from "@/stores/marketDashboard";
const mockMarketStore = vi.mocked(useMarketDashboardStore);

function baseStore(overrides: Record<string, unknown> = {}) {
  return {
    stocks: [],
    quotes: {},
    selected: new Set<string>(),
    loading: false,
    error: null,
    refresh: vi.fn().mockResolvedValue(undefined),
    add: vi.fn().mockResolvedValue({ added: true, exists: false }),
    remove: vi.fn().mockResolvedValue(undefined),
    refreshQuotes: vi.fn().mockResolvedValue(undefined),
    toggleSelection: vi.fn(),
    clearSelection: vi.fn(),
    ...overrides,
  };
}

function baseMarketStore(overrides: Record<string, unknown> = {}) {
  return {
    selectedCode: null,
    selectedBars: [],
    barsLoading: false,
    barsError: null,
    setSelectedCode: vi.fn(),
    ...overrides,
  };
}

function renderPage(
  storeOverrides: Record<string, unknown> = {},
  marketOverrides: Record<string, unknown> = {},
) {
  mockStore.mockReturnValue(baseStore(storeOverrides) as ReturnType<typeof useWatchlistStore>);
  mockMarketStore.mockImplementation((selector) => selector(baseMarketStore(marketOverrides) as never));
  return render(
    <MemoryRouter initialEntries={["/watchlist"]}>
      <WatchlistPage />
    </MemoryRouter>
  );
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("WatchlistPage — empty state", () => {
  it("shows empty state message when no stocks", () => {
    renderPage();
    expect(screen.getByText(/暂无自选股|No stocks yet/i)).toBeTruthy();
  });

  it("does not render or refresh Market Pulse", () => {
    const refreshPulse = vi.fn().mockResolvedValue(undefined);
    renderPage({}, { refreshPulse });

    expect(screen.queryByTestId("watchlist-market-pulse")).toBeNull();
    expect(screen.queryByTestId("market-pulse-card")).toBeNull();
    expect(refreshPulse).not.toHaveBeenCalled();
  });
});

describe("WatchlistPage — add form validation", () => {
  it("shows inline error for non-6-digit code", async () => {
    renderPage();
    const input = screen.getByRole("textbox");
    fireEvent.change(input, { target: { value: "abc" } });
    const form = input.closest("form")!;
    await act(async () => { fireEvent.submit(form); });
    expect(screen.getByText(/6 位|6-digit/i)).toBeTruthy();
  });

  it("calls add for valid 6-digit code", async () => {
    const add = vi.fn().mockResolvedValue({ added: true, exists: false });
    renderPage({ add });
    const input = screen.getByRole("textbox");
    fireEvent.change(input, { target: { value: "000001" } });
    const form = input.closest("form")!;
    await act(async () => { fireEvent.submit(form); });
    expect(add).toHaveBeenCalledWith("000001");
  });
});

describe("WatchlistPage — quotes table", () => {
  const stocks = [{ code: "000001", name: "平安银行", market: "a_stock", added_at: "" }];
  const quotes = { "000001": { code: "000001", name: "平安银行", price: 10.5, change_pct: 1.2, change_amt: 0.12 } };

  it("renders stock code in table", () => {
    renderPage({ stocks, quotes });
    expect(screen.getByText("000001")).toBeTruthy();
  });

  it("applies red class for positive change_pct (A股上涨红)", () => {
    renderPage({ stocks, quotes });
    const cells = screen.getAllByText("+1.20%");
    expect(cells[0].className).toContain("text-red-500");
  });

  it("applies green class for negative change_pct (A股下跌绿)", () => {
    const negQuotes = { "000001": { ...quotes["000001"], change_pct: -1.2, change_amt: -0.12 } };
    renderPage({ stocks, quotes: negQuotes });
    const cells = screen.getAllByText("-1.20%");
    expect(cells[0].className).toContain("text-green-500");
  });
});

describe("WatchlistPage — delete confirmation", () => {
  const stocks = [{ code: "000001", name: "平安银行", market: "a_stock", added_at: "" }];

  it("first click shows confirm button (two-step delete)", async () => {
    const remove = vi.fn();
    renderPage({ stocks, remove });
    const deleteBtn = screen.getByTestId("delete-000001");
    await act(async () => { fireEvent.click(deleteBtn); });
    expect(remove).not.toHaveBeenCalled();
    expect(screen.getByTestId("confirm-delete-000001")).toBeTruthy();
  });

  it("second click (confirm) calls remove", async () => {
    const remove = vi.fn().mockResolvedValue(undefined);
    renderPage({ stocks, remove });
    await act(async () => { fireEvent.click(screen.getByTestId("delete-000001")); });
    await act(async () => { fireEvent.click(screen.getByTestId("confirm-delete-000001")); });
    expect(remove).toHaveBeenCalledWith("000001");
  });
});

describe("WatchlistPage — multi-select + send to agent", () => {
  const stocks = [
    { code: "000001", name: "平安银行", market: "a_stock", added_at: "" },
    { code: "600519", name: "贵州茅台", market: "a_stock", added_at: "" },
  ];

  it("shows send-to-agent button when a stock is selected", () => {
    renderPage({ stocks, selected: new Set(["000001"]) as Set<string> });
    expect(screen.getByTestId("send-to-agent")).toBeTruthy();
  });

  it("send-to-agent button navigates to /agent with prefill", async () => {
    renderPage({ stocks, selected: new Set(["000001"]) as Set<string> });
    await act(async () => { fireEvent.click(screen.getByTestId("send-to-agent")); });
    expect(mockNavigate).toHaveBeenCalledWith(expect.stringContaining("/agent?prefill="));
  });
});
