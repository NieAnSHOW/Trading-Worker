import { useTranslation } from "react-i18next";
import type { LLMUsageSummary } from "@/lib/api";
import { cn } from "@/lib/utils";

interface Props {
  usage: LLMUsageSummary | null;
  compact?: boolean;
}

export function LLMUsagePanel({ usage, compact = false }: Props) {
  const { t, i18n } = useTranslation();
  const formatNumber = (value: number) => new Intl.NumberFormat(i18n.language).format(value);

  if (usage === null) {
    return (
      <section
        className={cn("border border-border/70 bg-muted/20 p-3 text-xs", compact ? "max-w-xl" : "w-full")}
        aria-label={t("llmUsage.title")}
      >
        <p className="text-muted-foreground">{t("llmUsage.noUsage")}</p>
      </section>
    );
  }

  const { totals } = usage;
  const reportedCacheUsage = [
    totals.cache_read_tokens === undefined
      ? null
      : [t("llmUsage.cacheReadTokens"), formatNumber(totals.cache_read_tokens)],
    totals.cache_write_tokens === undefined
      ? null
      : [t("llmUsage.cacheWriteTokens"), formatNumber(totals.cache_write_tokens)],
  ].filter((entry): entry is [string, string] => entry !== null);
  const fields: Array<[string, string]> = [
    [t("llmUsage.provider"), usage.provider],
    [t("llmUsage.model"), usage.model ?? "-"],
    [t("llmUsage.calls"), formatNumber(totals.calls)],
    [t("llmUsage.inputTokens"), formatNumber(totals.input_tokens)],
    [t("llmUsage.outputTokens"), formatNumber(totals.output_tokens)],
    [t("llmUsage.totalTokens"), formatNumber(totals.total_tokens)],
    ...reportedCacheUsage,
  ];

  return (
    <section
      className={cn("border border-border/70 bg-muted/20 p-3 text-xs", compact ? "max-w-xl" : "w-full")}
      aria-label={t("llmUsage.title")}
    >
      <dl className="grid grid-cols-2 gap-x-4 gap-y-2 sm:grid-cols-3">
        {fields.map(([label, value]) => (
          <div key={label} className="min-w-0">
            <dt className="text-muted-foreground">{label}</dt>
            <dd className="truncate font-medium tabular-nums" title={value}>{value}</dd>
          </div>
        ))}
      </dl>
      {reportedCacheUsage.length === 0 && (
        <p className="mt-2 text-muted-foreground">{t("llmUsage.cacheUnavailable")}</p>
      )}
      <p className="mt-2 font-medium">
        {usage.metering_eligible === true ? t("llmUsage.futureEligible") : t("llmUsage.localOnly")}
      </p>
    </section>
  );
}
