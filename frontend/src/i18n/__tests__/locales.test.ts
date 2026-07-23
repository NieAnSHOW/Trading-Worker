import { describe, it, expect } from "vitest";
import en from "../locales/en.json";
import zhCN from "../locales/zh-CN.json";
import ja from "../locales/ja.json";
import ko from "../locales/ko.json";
import ar from "../locales/ar.json";

describe("i18n — shadow report PDF export", () => {
  it("en.json runComplete.shadowReportPdf exists and is non-empty", () => {
    const val = (en as Record<string, unknown>).runComplete as Record<string, string> | undefined;
    expect(val?.shadowReportPdf).toBeTypeOf("string");
    expect(val?.shadowReportPdf?.length).toBeGreaterThan(0);
  });

  it("zh-CN.json runComplete.shadowReportPdf exists and is non-empty", () => {
    const val = (zhCN as Record<string, unknown>).runComplete as Record<string, string> | undefined;
    expect(val?.shadowReportPdf).toBeTypeOf("string");
    expect(val?.shadowReportPdf?.length).toBeGreaterThan(0);
  });
});

describe("i18n — desktop external shortcuts", () => {
  const requiredPaths = [
    "layout.externalShortcuts.title",
    "layout.externalShortcuts.sites.tonghuashun.label",
    "layout.externalShortcuts.sites.tonghuashun.description",
    "layout.externalShortcuts.sites.tencentFinance.label",
    "layout.externalShortcuts.sites.tencentFinance.description",
    "layout.externalShortcuts.sites.eastmoney.label",
    "layout.externalShortcuts.sites.eastmoney.description",
    "layout.externalShortcuts.sites.sinaFinance.label",
    "layout.externalShortcuts.sites.sinaFinance.description",
  ];

  const readPath = (source: unknown, path: string) =>
    path.split(".").reduce<unknown>((value, key) => {
      if (!value || typeof value !== "object") return undefined;
      return (value as Record<string, unknown>)[key];
    }, source);

  it.each([
    ["en.json", en],
    ["zh-CN.json", zhCN],
  ])("%s has all external shortcut keys", (_name, locale) => {
    for (const path of requiredPaths) {
      const value = readPath(locale, path);
      expect(value, path).toBeTypeOf("string");
      expect((value as string).length, path).toBeGreaterThan(0);
    }
  });
});

describe("i18n — LLM usage panel", () => {
  const requiredPaths = [
    "llmUsage.title",
    "llmUsage.provider",
    "llmUsage.model",
    "llmUsage.calls",
    "llmUsage.inputTokens",
    "llmUsage.outputTokens",
    "llmUsage.totalTokens",
    "llmUsage.cacheReadTokens",
    "llmUsage.cacheWriteTokens",
    "llmUsage.cacheUnavailable",
    "llmUsage.futureEligible",
    "llmUsage.localOnly",
    "llmUsage.noUsage",
  ];

  const readPath = (source: unknown, path: string) =>
    path.split(".").reduce<unknown>((value, key) => {
      if (!value || typeof value !== "object") return undefined;
      return (value as Record<string, unknown>)[key];
    }, source);

  it.each([
    ["en.json", en],
    ["zh-CN.json", zhCN],
  ])("%s has all LLM usage panel keys", (_name, locale) => {
    for (const path of requiredPaths) {
      const value = readPath(locale, path);
      expect(value, path).toBeTypeOf("string");
      expect((value as string).length, path).toBeGreaterThan(0);
    }
  });
});

describe("i18n — global LLM usage center", () => {
  const usageCenterKeys = [
    "title", "refresh", "refreshing", "lastUpdated", "allTime", "last7Days",
    "last30Days", "thisMonth", "customRange", "startDate", "endDate",
    "totalTokens", "inputTokens", "outputTokens", "calls", "sessions",
    "cacheRead", "cacheWrite", "cacheCoverage", "trend", "breakdown",
    "sessionDetails", "searchSessions", "runs", "expandSession",
    "collapseSession", "empty", "partialData", "missingUsageRuns",
    "invalidUsageRuns", "loadFailed", "retry", "apply", "previousPage",
    "nextPage", "notProvided",
  ].sort();

  it.each([
    ["en.json", en],
    ["zh-CN.json", zhCN],
    ["ja.json", ja],
    ["ko.json", ko],
    ["ar.json", ar],
  ])("%s has the same non-empty usage center leaf keys", (_name, locale) => {
    const root = locale as Record<string, unknown>;
    expect((root.layout as Record<string, unknown>)?.usage).toBeTypeOf("string");
    const usageCenter = root.usageCenter as Record<string, unknown> | undefined;
    expect(Object.keys(usageCenter ?? {}).sort()).toEqual(usageCenterKeys);
    for (const key of usageCenterKeys) {
      expect(usageCenter?.[key], key).toBeTypeOf("string");
      expect((usageCenter?.[key] as string).length, key).toBeGreaterThan(0);
    }
  });
});
