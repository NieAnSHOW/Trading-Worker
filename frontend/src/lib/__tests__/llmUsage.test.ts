import { describe, expect, it } from "vitest";
import { accumulateLLMUsage, parseLLMUsageDelta, parseLLMUsageSummary } from "@/lib/llmUsage";

const validDelta = {
  iter: 1,
  input_tokens: 10,
  output_tokens: 5,
  total_tokens: 15,
  provider: "vip_server",
  model: "vip-v1",
  metering_eligible: true,
};

describe("LLM usage reducer", () => {
  it("accumulates allowed counters and preserves cache fields only when present", () => {
    const first = parseLLMUsageDelta({
      ...validDelta,
      cache_read_tokens: 4,
    });
    const second = parseLLMUsageDelta({
      ...validDelta,
      iter: 2,
      input_tokens: 20,
      output_tokens: 4,
      total_tokens: 24,
    });

    expect(first).not.toBeNull();
    expect(second).not.toBeNull();
    expect(accumulateLLMUsage(accumulateLLMUsage(null, first!), second!)).toMatchObject({
      totals: {
        input_tokens: 30,
        output_tokens: 9,
        total_tokens: 39,
        calls: 2,
        cache_read_tokens: 4,
      },
      per_iteration: [
        expect.objectContaining({ iter: 1 }),
        expect.objectContaining({ iter: 2 }),
      ],
    });

    expect(accumulateLLMUsage(null, second!).totals).not.toHaveProperty("cache_read_tokens");
    expect(accumulateLLMUsage(null, second!).totals).not.toHaveProperty("cache_write_tokens");
  });

  it("preserves explicit zero cache counters", () => {
    const delta = parseLLMUsageDelta({
      ...validDelta,
      cache_read_tokens: 0,
      cache_write_tokens: 0,
    });

    expect(delta).not.toBeNull();
    expect(accumulateLLMUsage(null, delta!).totals).toMatchObject({
      cache_read_tokens: 0,
      cache_write_tokens: 0,
    });
  });

  it.each([
    [{ ...validDelta, input_tokens: -1 }],
    [{ ...validDelta, output_tokens: Number.NaN }],
    [{ ...validDelta, total_tokens: "15" }],
    [{ ...validDelta, provider: "" }],
    [{ ...validDelta, iter: 0 }],
    [{ ...validDelta, iter: 1.5 }],
    [{ ...validDelta, input_tokens: undefined }],
    [{ ...validDelta, model: 1 }],
    [{ ...validDelta, metering_eligible: "true" }],
  ])("rejects an invalid delta: %o", (data) => {
    expect(parseLLMUsageDelta(data)).toBeNull();
  });

  it("drops raw SSE fields outside the whitelist", () => {
    const delta = parseLLMUsageDelta({
      ...validDelta,
      api_key: "secret",
      prompt: "private prompt",
      response: "private response",
    });

    expect(delta).not.toBeNull();
    const summary = accumulateLLMUsage(null, delta!);
    expect(summary).not.toHaveProperty("api_key");
    expect(summary).not.toHaveProperty("prompt");
    expect(summary).not.toHaveProperty("response");
    expect(summary.per_iteration[0]).not.toHaveProperty("api_key");
    expect(summary.per_iteration[0]).not.toHaveProperty("prompt");
    expect(summary.per_iteration[0]).not.toHaveProperty("response");
  });

  it("parses persisted summaries through the same recursive whitelist", () => {
    expect(parseLLMUsageSummary({
      provider: "vip_server",
      model: "vip-v1",
      metering_eligible: true,
      prompt: "private prompt",
      totals: {
        input_tokens: 10,
        output_tokens: 5,
        total_tokens: 15,
        calls: 1,
        api_key: "secret",
      },
      per_iteration: [{
        iter: 1,
        input_tokens: 10,
        output_tokens: 5,
        total_tokens: 15,
        response: "private response",
      }],
    })).toEqual({
      provider: "vip_server",
      model: "vip-v1",
      metering_eligible: true,
      totals: {
        input_tokens: 10,
        output_tokens: 5,
        total_tokens: 15,
        calls: 1,
      },
      per_iteration: [{
        iter: 1,
        input_tokens: 10,
        output_tokens: 5,
        total_tokens: 15,
      }],
    });
  });

  it.each([
    [null],
    [{ ...validDelta }],
    [{ provider: "openai", model: null, metering_eligible: false, totals: {}, per_iteration: [] }],
    [{
      provider: "openai", model: null, metering_eligible: false,
      totals: { input_tokens: 1, output_tokens: 1, total_tokens: 2, calls: 1 },
      per_iteration: [{ iter: 0, input_tokens: 1, output_tokens: 1, total_tokens: 2 }],
    }],
  ])("rejects an invalid persisted summary: %o", (data) => {
    expect(parseLLMUsageSummary(data)).toBeNull();
  });
});
