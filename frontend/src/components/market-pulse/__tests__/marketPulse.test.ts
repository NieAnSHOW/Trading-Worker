import { describe, expect, it } from "vitest";
import {
  categorizeMarketPulse,
  countMarketPulseCategories,
  filterMarketPulse,
  getPulsePage,
  pulseItemKey,
} from "../marketPulse";

const item = (changeType: string, overrides = {}) => ({
  code: "600519",
  name: "Guizhou Moutai",
  time: "10:00",
  changeType,
  info: "test",
  source: "test",
  stale: false,
  ...overrides,
});

describe("market pulse helpers", () => {
  it.each([
    ["打开涨停板", "limitDownBroken"],
    ["封涨停板", "limitUp"],
    ["火箭发射", "upward"],
    ["高台跳水", "downward"],
    ["成交量放大", "turnoverCapital"],
    ["未来标签", "other"],
  ] as const)("maps %s to %s", (changeType, category) => {
    expect(categorizeMarketPulse(item(changeType))).toBe(category);
  });

  it("combines category, query, and stale filters before paginating", () => {
    const result = filterMarketPulse(
      [
        item("火箭发射", { code: "000001", name: "Ping An Bank" }),
        item("高台跳水", { code: "600519", stale: true }),
      ],
      { category: "upward", query: "000001", hideStale: true },
    );

    expect(getPulsePage(result, 0, 200).items).toHaveLength(1);
  });

  it("matches a trimmed case-insensitive query while preserving source order", () => {
    const first = item("火箭发射", { code: "000001", name: "Ping An Bank" });
    const second = item("快速反弹", { code: "000002", name: "PING AN Insurance" });

    expect(
      filterMarketPulse([first, second], { category: "all", query: " ping an ", hideStale: false }),
    ).toEqual([first, second]);
  });

  it("returns all category counts including all items", () => {
    expect(countMarketPulseCategories([item("封涨停板"), item("火箭发射"), item("未来标签")])).toEqual({
      all: 3,
      limitDownBroken: 0,
      limitUp: 1,
      upward: 1,
      downward: 0,
      turnoverCapital: 0,
      other: 1,
    });
  });

  it("clamps pages and always reports at least one page", () => {
    const entries = [item("火箭发射", { code: "000001" }), item("火箭发射", { code: "000002" })];

    expect(getPulsePage(entries, 5, 1)).toEqual({ items: [entries[1]], page: 1, totalPages: 2 });
    expect(getPulsePage([], -1, 20)).toEqual({ items: [], page: 0, totalPages: 1 });
  });

  it("falls back to a page size of one when a fractional size rounds down to zero", () => {
    const entries = [item("火箭发射", { code: "000001" })];

    expect(getPulsePage(entries, 0, 0.5)).toEqual({ items: entries, page: 0, totalPages: 1 });
  });

  it("builds a key from item identity fields and its list index", () => {
    expect(pulseItemKey(item("火箭发射"), 3)).toBe("600519-10:00-火箭发射-3");
  });
});
