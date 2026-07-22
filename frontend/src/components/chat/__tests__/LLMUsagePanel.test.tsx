import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import i18n from "@/i18n";
import type { LLMUsageSummary } from "@/lib/api";
import { LLMUsagePanel } from "../LLMUsagePanel";

const completeUsage: LLMUsageSummary = {
  provider: "vip_server",
  model: "gpt-5-mini",
  metering_eligible: true,
  totals: {
    calls: 3,
    input_tokens: 39,
    output_tokens: 12,
    total_tokens: 51,
    cache_read_tokens: 8,
    cache_write_tokens: 4,
  },
  per_iteration: [],
};

const usageWithoutCache: LLMUsageSummary = {
  ...completeUsage,
  metering_eligible: false,
  totals: {
    calls: 1,
    input_tokens: 10,
    output_tokens: 20,
    total_tokens: 30,
  },
};

describe("LLMUsagePanel", () => {
  beforeEach(async () => {
    await i18n.changeLanguage("en");
  });

  afterEach(async () => {
    await i18n.changeLanguage("en");
  });

  it("renders only provider-reported usage fields and the eligible label", () => {
    render(<LLMUsagePanel usage={completeUsage} />);

    expect(screen.getByRole("region", { name: "LLM usage" })).toBeInTheDocument();
    expect(screen.getByText("vip_server")).toBeInTheDocument();
    expect(screen.getByText("gpt-5-mini")).toBeInTheDocument();
    expect(screen.getByText("39")).toBeInTheDocument();
    expect(screen.getByText("Eligible for future platform metering")).toBeInTheDocument();
  });

  it("reports unavailable cache usage only when both cache fields are absent", () => {
    render(<LLMUsagePanel usage={usageWithoutCache} />);

    expect(screen.getByText("Cache usage not provided by provider")).toBeInTheDocument();
    expect(screen.getByText("Local-only usage record")).toBeInTheDocument();
  });

  it("renders no-usage state without treating missing usage as zero", () => {
    render(<LLMUsagePanel usage={null} />);

    expect(screen.getByText("No provider-reported usage for this run")).toBeInTheDocument();
    expect(screen.queryByText("0")).not.toBeInTheDocument();
  });

  it("treats old summaries without eligibility as local-only", () => {
    const oldUsage = {
      ...usageWithoutCache,
      metering_eligible: undefined,
    } as unknown as LLMUsageSummary;

    render(<LLMUsagePanel usage={oldUsage} />);

    expect(screen.getByText("Local-only usage record")).toBeInTheDocument();
  });

  it("renders all status copy in Chinese", async () => {
    await i18n.changeLanguage("zh-CN");
    render(<LLMUsagePanel usage={usageWithoutCache} />);

    expect(screen.getByRole("region", { name: "LLM 用量" })).toBeInTheDocument();
    expect(screen.getByText("提供商未提供缓存用量")).toBeInTheDocument();
    expect(screen.getByText("仅本地用量记录")).toBeInTheDocument();
  });
});
