import type {
  LLMUsageDelta,
  LLMUsageIteration,
  LLMUsageSummary,
  LLMUsageTotals,
} from "./api";

const asNonNegativeInt = (value: unknown): number | null =>
  typeof value === "number" && Number.isInteger(value) && value >= 0 ? value : null;

const optionalCounter = (value: unknown): number | undefined => {
  const normalized = asNonNegativeInt(value);
  return normalized === null ? undefined : normalized;
};

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

const copyIteration = (iteration: LLMUsageIteration): LLMUsageIteration => {
  const copy: LLMUsageIteration = {
    iter: iteration.iter,
    input_tokens: iteration.input_tokens,
    output_tokens: iteration.output_tokens,
    total_tokens: iteration.total_tokens,
  };
  if (iteration.cache_read_tokens !== undefined) {
    copy.cache_read_tokens = iteration.cache_read_tokens;
  }
  if (iteration.cache_write_tokens !== undefined) {
    copy.cache_write_tokens = iteration.cache_write_tokens;
  }
  return copy;
};

export function parseLLMUsageDelta(data: Record<string, unknown>): LLMUsageDelta | null {
  const iter = asNonNegativeInt(data.iter);
  const inputTokens = asNonNegativeInt(data.input_tokens);
  const outputTokens = asNonNegativeInt(data.output_tokens);
  const totalTokens = asNonNegativeInt(data.total_tokens);
  const provider = data.provider;
  const model = data.model;
  const meteringEligible = data.metering_eligible;

  if (
    iter === null || iter <= 0 ||
    inputTokens === null || outputTokens === null || totalTokens === null ||
    typeof provider !== "string" || provider.trim().length === 0 ||
    (typeof model !== "string" && model !== null) ||
    typeof meteringEligible !== "boolean"
  ) {
    return null;
  }

  const delta: LLMUsageDelta = {
    iter,
    input_tokens: inputTokens,
    output_tokens: outputTokens,
    total_tokens: totalTokens,
    provider,
    model,
    metering_eligible: meteringEligible,
  };
  const cacheReadTokens = optionalCounter(data.cache_read_tokens);
  const cacheWriteTokens = optionalCounter(data.cache_write_tokens);
  if (cacheReadTokens !== undefined) {
    delta.cache_read_tokens = cacheReadTokens;
  }
  if (cacheWriteTokens !== undefined) {
    delta.cache_write_tokens = cacheWriteTokens;
  }
  return delta;
}

export function parseLLMUsageSummary(data: unknown): LLMUsageSummary | null {
  if (!isRecord(data) || !isRecord(data.totals) || !Array.isArray(data.per_iteration)) {
    return null;
  }

  const provider = data.provider;
  const model = data.model;
  const meteringEligible = data.metering_eligible;
  const inputTokens = asNonNegativeInt(data.totals.input_tokens);
  const outputTokens = asNonNegativeInt(data.totals.output_tokens);
  const totalTokens = asNonNegativeInt(data.totals.total_tokens);
  const calls = asNonNegativeInt(data.totals.calls);
  if (
    typeof provider !== "string" || provider.trim().length === 0 ||
    (typeof model !== "string" && model !== null) ||
    (typeof meteringEligible !== "boolean" && meteringEligible !== undefined) ||
    inputTokens === null || outputTokens === null || totalTokens === null || calls === null
  ) {
    return null;
  }

  const perIteration: LLMUsageIteration[] = [];
  for (const value of data.per_iteration) {
    if (!isRecord(value)) return null;
    const iter = asNonNegativeInt(value.iter);
    const iterationInput = asNonNegativeInt(value.input_tokens);
    const iterationOutput = asNonNegativeInt(value.output_tokens);
    const iterationTotal = asNonNegativeInt(value.total_tokens);
    if (iter === null || iter <= 0 || iterationInput === null || iterationOutput === null || iterationTotal === null) {
      return null;
    }
    const iteration: LLMUsageIteration = {
      iter,
      input_tokens: iterationInput,
      output_tokens: iterationOutput,
      total_tokens: iterationTotal,
    };
    const cacheReadTokens = optionalCounter(value.cache_read_tokens);
    const cacheWriteTokens = optionalCounter(value.cache_write_tokens);
    if (cacheReadTokens !== undefined) iteration.cache_read_tokens = cacheReadTokens;
    if (cacheWriteTokens !== undefined) iteration.cache_write_tokens = cacheWriteTokens;
    perIteration.push(iteration);
  }

  const totals: LLMUsageTotals = {
    input_tokens: inputTokens,
    output_tokens: outputTokens,
    total_tokens: totalTokens,
    calls,
  };
  const cacheReadTokens = optionalCounter(data.totals.cache_read_tokens);
  const cacheWriteTokens = optionalCounter(data.totals.cache_write_tokens);
  if (cacheReadTokens !== undefined) totals.cache_read_tokens = cacheReadTokens;
  if (cacheWriteTokens !== undefined) totals.cache_write_tokens = cacheWriteTokens;

  return {
    provider,
    model,
    metering_eligible: meteringEligible === true,
    totals,
    per_iteration: perIteration,
  };
}

export function accumulateLLMUsage(
  current: LLMUsageSummary | null,
  delta: LLMUsageDelta,
): LLMUsageSummary {
  const currentTotals = current?.totals;
  const totals: LLMUsageTotals = {
    input_tokens: (currentTotals?.input_tokens ?? 0) + delta.input_tokens,
    output_tokens: (currentTotals?.output_tokens ?? 0) + delta.output_tokens,
    total_tokens: (currentTotals?.total_tokens ?? 0) + delta.total_tokens,
    calls: (currentTotals?.calls ?? 0) + 1,
  };

  for (const field of ["cache_read_tokens", "cache_write_tokens"] as const) {
    const existing = currentTotals?.[field];
    const incoming = delta[field];
    if (existing !== undefined || incoming !== undefined) {
      totals[field] = (existing ?? 0) + (incoming ?? 0);
    }
  }

  return {
    provider: delta.provider,
    model: delta.model,
    metering_eligible: delta.metering_eligible,
    totals,
    per_iteration: [
      ...(current?.per_iteration.map(copyIteration) ?? []),
      copyIteration(delta),
    ],
  };
}
