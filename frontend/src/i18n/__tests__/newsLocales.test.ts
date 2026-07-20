import { describe, expect, it } from "vitest";
import ar from "../locales/ar.json";
import en from "../locales/en.json";
import ja from "../locales/ja.json";
import ko from "../locales/ko.json";
import zhCN from "../locales/zh-CN.json";

const trackIds = ["ai", "semi", "robot", "auto", "energy", "bio", "space", "security", "tech", "consumer", "macro", "science"];
const requiredPaths = [
  "layout.news",
  "news.title",
  "news.fresh",
  "news.stale",
  "news.unavailable",
  "news.partial",
  "news.ai",
  "news.aiUnavailable",
  "news.error",
  "news.refresh",
  "news.trackList",
  "news.selectTrack",
  "news.refreshProgress",
  "news.loading",
  "news.emptySnapshot",
  "news.noSummary",
  "news.unknownTime",
  "news.viewOriginal",
  "news.articleList",
  "news.emptyTrack",
  ...trackIds.map((trackId) => `news.tracks.${trackId}`),
];

function readPath(source: unknown, path: string): unknown {
  return path.split(".").reduce<unknown>((value, key) => {
    if (!value || typeof value !== "object") return undefined;
    return (value as Record<string, unknown>)[key];
  }, source);
}

describe("i18n - investment news", () => {
  it.each([
    ["zh-CN", zhCN],
    ["en", en],
    ["ja", ja],
    ["ko", ko],
    ["ar", ar],
  ])("%s has the complete investment-news key structure", (_language, locale) => {
    for (const path of requiredPaths) {
      const value = readPath(locale, path);
      expect(value, path).toBeTypeOf("string");
      expect((value as string).trim(), path).not.toHaveLength(0);
    }
  });
});
