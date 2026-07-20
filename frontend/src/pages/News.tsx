import { AlertCircle, ExternalLink, RefreshCw } from "lucide-react";
import type { KeyboardEvent } from "react";
import { useTranslation } from "react-i18next";
import { useNews } from "@/hooks/useNews";
import type { NewsArticle, NewsPublicError, NewsTrackId } from "@/lib/api";
import { cn } from "@/lib/utils";

const TRACK_IDS: NewsTrackId[] = [
  "ai",
  "semi",
  "robot",
  "auto",
  "energy",
  "bio",
  "space",
  "security",
  "tech",
  "consumer",
  "macro",
  "science",
];

function safeArticleUrl(value: string): string | null {
  try {
    const parsed = new URL(value);
    return parsed.protocol === "http:" || parsed.protocol === "https:"
      ? parsed.href
      : null;
  } catch {
    return null;
  }
}

function errorMessage(error: unknown): string | null {
  if (error instanceof Error && error.message) return error.message;
  if (
    error
    && typeof error === "object"
    && "message" in error
    && typeof error.message === "string"
  ) {
    return error.message;
  }
  return null;
}

function publicErrorMessage(error: NewsPublicError | null | undefined): string | null {
  return error?.message ?? null;
}

function ArticleRow({ article }: { article: NewsArticle }) {
  const { t, i18n } = useTranslation();
  const url = safeArticleUrl(article.url);
  const title = article.title_zh || article.title;

  return (
    <article
      data-testid={article.id}
      className="min-w-0 border-t border-border/70 py-5 first:border-t-0 first:pt-0"
    >
      <div className="flex min-w-0 flex-col gap-2 sm:flex-row sm:items-start sm:justify-between sm:gap-5">
        <div className="min-w-0">
          <h3 className="break-words text-base font-semibold leading-6 [overflow-wrap:anywhere]">
            {title}
          </h3>
          <p className="mt-1.5 break-words text-sm leading-6 text-muted-foreground">
            {article.summary ?? t("news.noSummary")}
          </p>
        </div>
        {url && (
          <a
            href={url}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex shrink-0 items-center gap-1 text-xs font-medium text-primary hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40"
          >
            {t("news.viewOriginal")}
            <ExternalLink className="h-3.5 w-3.5" aria-hidden="true" />
          </a>
        )}
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
        <span>{article.source.name}</span>
        {article.published_at ? (
          <time dateTime={article.published_at}>
            {new Date(article.published_at).toLocaleString(i18n.resolvedLanguage)}
          </time>
        ) : (
          <span>{t("news.unknownTime")}</span>
        )}
      </div>
    </article>
  );
}

export function News() {
  const { t, i18n } = useTranslation();
  const {
    snapshot,
    stale,
    snapshotError,
    refreshStatus,
    selectedTrackId,
    isLoading,
    isRefreshing,
    error,
    selectTrack,
    refreshNews,
  } = useNews();

  const selectedTrack = snapshot?.tracks.find(
    (track) => track.track_id === selectedTrackId,
  ) ?? null;
  const requestError = errorMessage(error);
  const statusError = publicErrorMessage(refreshStatus?.error);
  const envelopeError = publicErrorMessage(snapshotError);
  const visibleError = requestError ?? statusError ?? envelopeError;
  const refreshLabel = t("news.refresh");
  const activeTrackId = selectedTrackId ?? TRACK_IDS[0];

  const handleTrackKeyDown = (
    event: KeyboardEvent<HTMLButtonElement>,
    trackId: NewsTrackId,
  ) => {
    const currentIndex = TRACK_IDS.indexOf(trackId);
    let nextIndex: number | null = null;

    if (event.key === "ArrowRight") {
      nextIndex = (currentIndex + 1) % TRACK_IDS.length;
    } else if (event.key === "ArrowLeft") {
      nextIndex = (currentIndex - 1 + TRACK_IDS.length) % TRACK_IDS.length;
    } else if (event.key === "Home") {
      nextIndex = 0;
    } else if (event.key === "End") {
      nextIndex = TRACK_IDS.length - 1;
    }

    if (nextIndex === null) return;
    event.preventDefault();
    const nextTrackId = TRACK_IDS[nextIndex];
    selectTrack(nextTrackId);
    document.getElementById(`news-tab-${nextTrackId}`)?.focus();
  };

  return (
    <div
      data-testid="news-workspace"
      className="mx-auto flex min-h-full w-full min-w-0 max-w-7xl flex-col px-4 py-5 sm:px-6 lg:px-8"
    >
      <header className="flex min-w-0 items-start justify-between gap-4 border-b border-border pb-4">
        <div className="min-w-0">
          <h1 className="break-words text-xl font-semibold sm:text-2xl">
            {t("news.title")}
          </h1>
          {snapshot && (
            <time
              dateTime={snapshot.generated_at}
              className="mt-1 block text-xs text-muted-foreground"
            >
              {new Date(snapshot.generated_at).toLocaleString(i18n.resolvedLanguage)}
            </time>
          )}
        </div>
        <button
          type="button"
          aria-label={refreshLabel}
          title={refreshLabel}
          disabled={isRefreshing}
          onClick={() => void refreshNews()}
          className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md border bg-background text-muted-foreground transition-colors hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40 disabled:cursor-not-allowed disabled:opacity-50"
        >
          <RefreshCw
            className={cn("h-4 w-4", isRefreshing && "animate-spin")}
            aria-hidden="true"
          />
        </button>
      </header>

      <div data-testid="news-desktop-tracks" className="hidden border-b border-border md:block">
        <div
          role="tablist"
          aria-label={t("news.trackList")}
          className="h-11 overflow-x-auto overscroll-x-contain"
        >
          <div className="flex h-full min-w-max items-center gap-1">
            {TRACK_IDS.map((trackId) => (
              <button
                key={trackId}
                id={`news-tab-${trackId}`}
                type="button"
                role="tab"
                aria-selected={activeTrackId === trackId}
                aria-controls={`news-panel-${trackId}`}
                tabIndex={activeTrackId === trackId ? 0 : -1}
                onClick={() => selectTrack(trackId)}
                onKeyDown={(event) => handleTrackKeyDown(event, trackId)}
                className={cn(
                  "h-8 shrink-0 rounded-md px-3 text-sm transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40",
                  activeTrackId === trackId
                    ? "bg-primary/10 font-medium text-primary"
                    : "text-muted-foreground hover:bg-muted hover:text-foreground",
                )}
              >
                {t(`news.tracks.${trackId}`)}
              </button>
            ))}
          </div>
        </div>
      </div>

      <div data-testid="news-mobile-tracks" className="border-b border-border py-3 md:hidden">
        <label htmlFor="news-track-select" className="sr-only">
          {t("news.selectTrack")}
        </label>
        <select
          id="news-track-select"
          aria-label={t("news.selectTrack")}
          value={selectedTrackId ?? ""}
          onChange={(event) => selectTrack(event.target.value as NewsTrackId)}
          className="h-10 w-full min-w-0 rounded-md border bg-background px-3 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40"
        >
          {TRACK_IDS.map((trackId) => (
            <option key={trackId} value={trackId}>
              {t(`news.tracks.${trackId}`)}
            </option>
          ))}
        </select>
      </div>

      {isRefreshing && refreshStatus && (
        <div
          role="status"
          className="border-b border-border py-3 text-sm text-muted-foreground"
        >
          {t("news.refreshProgress", {
            processedEndpoints: refreshStatus.processed_endpoints,
            totalEndpoints: refreshStatus.total_endpoints,
            processedTracks: refreshStatus.processed_tracks,
            totalTracks: refreshStatus.total_tracks,
          })}
        </div>
      )}

      {visibleError && (
        <div
          role="alert"
          className="flex min-w-0 items-start gap-2 border-b border-danger/30 py-3 text-sm text-danger"
        >
          <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
          <div className="min-w-0">
            <p className="font-medium">{t("news.error")}</p>
            <p className="break-words text-xs opacity-80">{visibleError}</p>
          </div>
        </div>
      )}

      {TRACK_IDS.filter((trackId) => trackId !== activeTrackId).map((trackId) => (
        <div
          key={trackId}
          id={`news-panel-${trackId}`}
          role="tabpanel"
          aria-labelledby={`news-tab-${trackId}`}
          hidden
        />
      ))}
      <div
        id={`news-panel-${activeTrackId}`}
        role="tabpanel"
        aria-labelledby={`news-tab-${activeTrackId}`}
        className="flex min-w-0 flex-1 flex-col"
      >
        {isLoading && !snapshot ? (
          <div className="flex flex-1 items-center justify-center py-16 text-sm text-muted-foreground">
            {t("news.loading")}
          </div>
        ) : !snapshot ? (
          <div className="flex flex-1 items-center justify-center py-16 text-sm text-muted-foreground">
            {t("news.emptySnapshot")}
          </div>
        ) : selectedTrack ? (
          <div className="min-w-0 py-5">
            <div className="flex flex-wrap items-center gap-2">
              {selectedTrack.state === "unavailable" ? (
                <span className="text-sm font-medium text-muted-foreground">
                  {t("news.unavailable")}
                </span>
              ) : stale || selectedTrack.stale ? (
                <span className="text-sm font-medium text-amber-600 dark:text-amber-400">
                  {t("news.stale")}
                </span>
              ) : (
                <span className="text-sm font-medium text-emerald-600 dark:text-emerald-400">
                  {t("news.fresh")}
                </span>
              )}
              {selectedTrack.partial && (
                <span className="text-sm text-amber-600 dark:text-amber-400">
                  {t("news.partial")}
                </span>
              )}
            </div>

            <section
              aria-labelledby="news-ai-heading"
              className="mt-5 border-y border-border bg-muted/30 px-4 py-4 sm:px-5"
            >
              <h2 id="news-ai-heading" className="text-sm font-semibold">
                {t("news.ai")}
              </h2>
              {selectedTrack.ai.available && selectedTrack.ai.highlights.length > 0 ? (
                <ul
                  aria-label={t("news.ai")}
                  className="mt-3 grid min-w-0 gap-2 text-sm leading-6 lg:grid-cols-2"
                >
                  {selectedTrack.ai.highlights.slice(0, 5).map((highlight, index) => (
                    <li key={`${index}-${highlight}`} className="flex min-w-0 items-start gap-2">
                      <span className="mt-2 h-1.5 w-1.5 shrink-0 rounded-full bg-primary" aria-hidden="true" />
                      <span className="min-w-0 break-words [overflow-wrap:anywhere]">{highlight}</span>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="mt-2 text-sm text-muted-foreground">
                  {t("news.aiUnavailable")}
                </p>
              )}
            </section>

            {selectedTrack.items.length > 0 ? (
              <section aria-label={t("news.articleList")} className="min-w-0 pt-5">
                {selectedTrack.items.map((item) => (
                  <ArticleRow key={item.id} article={item} />
                ))}
              </section>
            ) : (
              <p className="py-12 text-center text-sm text-muted-foreground">
                {t("news.emptyTrack")}
              </p>
            )}
          </div>
        ) : (
          <div className="flex flex-1 items-center justify-center py-16 text-sm text-muted-foreground">
            {t("news.emptySnapshot")}
          </div>
        )}
      </div>
    </div>
  );
}

export default News;
