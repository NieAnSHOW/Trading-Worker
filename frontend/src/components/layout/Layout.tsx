import { useTranslation } from "react-i18next";
import { useEffect, useState, useRef } from "react";
import { Link, Outlet, useLocation, useSearchParams } from "react-router-dom";
import { track } from "@/lib/telemetry";
import {
  Activity,
  BarChart3,
  Bot,
  Eye,
  FileText,
  Languages,
  Moon,
  Sun,
  Plus,
  Trash2,
  Pencil,
  ChevronsLeft,
  ChevronsRight,
  Settings,
  Layers,
  Loader2,
  Globe2,
  LineChart,
  Newspaper,
  Search,
  BookOpen,
  LayoutDashboard,
  type LucideIcon,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { useDarkMode } from "@/hooks/useDarkMode";
import { api, type SessionItem } from "@/lib/api";
import { useAgentStore } from "@/stores/agent";
import { ConnectionBanner } from "@/components/layout/ConnectionBanner";

// Bump on each release; one place keeps the footer in sync with package.json.
const APP_VERSION = "v0.1.10";

type ExternalShortcut = {
  id: string;
  labelKey: string;
  url: string;
  descriptionKey: string;
  icon: LucideIcon;
};

const EXTERNAL_SHORTCUTS: ExternalShortcut[] = [
  {
    id: "tonghuashun",
    labelKey: "layout.externalShortcuts.sites.tonghuashun.label",
    url: "https://www.10jqka.com.cn/",
    descriptionKey: "layout.externalShortcuts.sites.tonghuashun.description",
    icon: LineChart,
  },
  {
    id: "tencent-finance",
    labelKey: "layout.externalShortcuts.sites.tencentFinance.label",
    url: "https://stockapp.finance.qq.com/",
    descriptionKey: "layout.externalShortcuts.sites.tencentFinance.description",
    icon: Newspaper,
  },
  {
    id: "eastmoney",
    labelKey: "layout.externalShortcuts.sites.eastmoney.label",
    url: "https://www.eastmoney.com/",
    descriptionKey: "layout.externalShortcuts.sites.eastmoney.description",
    icon: Search,
  },
  {
    id: "sina-finance",
    labelKey: "layout.externalShortcuts.sites.sinaFinance.label",
    url: "https://finance.sina.com.cn/",
    descriptionKey: "layout.externalShortcuts.sites.sinaFinance.description",
    icon: Globe2,
  },
  {
    id: "cninfo",
    labelKey: "layout.externalShortcuts.sites.cninfo.label",
    url: "https://www.cninfo.com.cn/",
    descriptionKey: "layout.externalShortcuts.sites.cninfo.description",
    icon: FileText,
  },
  {
    id: "jisilu",
    labelKey: "layout.externalShortcuts.sites.jisilu.label",
    url: "https://www.jisilu.cn/",
    descriptionKey: "layout.externalShortcuts.sites.jisilu.description",
    icon: BarChart3,
  },
  {
    id: "wallstreetcn",
    labelKey: "layout.externalShortcuts.sites.wallstreetcn.label",
    url: "https://wallstreetcn.com/",
    descriptionKey: "layout.externalShortcuts.sites.wallstreetcn.description",
    icon: Newspaper,
  },
  {
    id: "cls",
    labelKey: "layout.externalShortcuts.sites.cls.label",
    url: "https://www.cls.cn/",
    descriptionKey: "layout.externalShortcuts.sites.cls.description",
    icon: Activity,
  },
  {
    id: "investing",
    labelKey: "layout.externalShortcuts.sites.investing.label",
    url: "https://cn.investing.com/",
    descriptionKey: "layout.externalShortcuts.sites.investing.description",
    icon: LineChart,
  },
  {
    id: "gelonghui",
    labelKey: "layout.externalShortcuts.sites.gelonghui.label",
    url: "https://www.gelonghui.com/",
    descriptionKey: "layout.externalShortcuts.sites.gelonghui.description",
    icon: Globe2,
  },
];

// ── nav link helper ──
function NavLink({
  to,
  icon: Icon,
  label,
  collapsed,
  isActive,
  external,
}: {
  to: string;
  icon: LucideIcon;
  label: string;
  collapsed: boolean;
  isActive: boolean;
  external?: boolean;
}) {
  const base =
    "flex items-center rounded-md text-sm transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-primary";

  if (external) {
    return (
      <a
        href={to}
        target="_blank"
        rel="noopener noreferrer"
        className={cn(
          base,
          collapsed ? "justify-center p-2" : "gap-3 px-3 py-2",
          "text-muted-foreground hover:bg-muted hover:text-foreground"
        )}
        title={collapsed ? label : undefined}
        onClick={() => {
          try { track("feature_use", { nav_target: to }, { name: "nav_sidebar" }); } catch {}
        }}
      >
        <Icon className="h-4 w-4 shrink-0" aria-hidden="true" />
        {!collapsed && label}
      </a>
    );
  }

  return (
    <Link
      to={to}
      className={cn(
        base,
        collapsed ? "justify-center p-2" : "gap-3 px-3 py-2",
        isActive
          ? "bg-primary/10 text-primary font-medium"
          : "text-muted-foreground hover:bg-muted hover:text-foreground"
      )}
      title={collapsed ? label : undefined}
      onClick={() => {
        try { track("feature_use", { nav_target: to }, { name: "nav_sidebar" }); } catch {}
      }}
    >
      <Icon className="h-4 w-4 shrink-0" aria-hidden="true" />
      {!collapsed && label}
    </Link>
  );
}

// ── section label ──
function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <div className="px-3 pt-4 pb-1 text-[11px] font-medium text-muted-foreground/60 uppercase tracking-wider select-none">
      {children}
    </div>
  );
}

export function Layout() {
  const { t, i18n: i18nHook } = useTranslation();
  const { pathname } = useLocation();
  const [searchParams] = useSearchParams();

  // ── telemetry ──
  const startedAtRef = useRef(Date.now());
  useEffect(() => {
    try { track("session_start", {}); } catch {}
    const onHide = () => {
      try {
        track("session_end", { duration_ms: Date.now() - startedAtRef.current });
      } catch {}
    };
    window.addEventListener("pagehide", onHide);
    return () => window.removeEventListener("pagehide", onHide);
  }, []);

  useEffect(() => {
    try { track("page_view", { route: pathname }); } catch {}
  }, [pathname]);

  const { dark, toggle } = useDarkMode();
  const [sessions, setSessions] = useState<SessionItem[]>([]);
  const [sessionsLoading, setSessionsLoading] = useState(true);
  const sseStatus = useAgentStore((s) => s.sseStatus);
  const sseRetryAttempt = useAgentStore((s) => s.sseRetryAttempt);
  const [collapsed, setCollapsed] = useState(
    () => localStorage.getItem("qa-sidebar") === "collapsed"
  );
  const [showExternal, setShowExternal] = useState(false);

  const activeSessionId = searchParams.get("session");
  const streamingSessionId = useAgentStore((s) => s.streamingSessionId);

  useEffect(() => {
    localStorage.setItem("qa-sidebar", collapsed ? "collapsed" : "expanded");
  }, [collapsed]);

  const loadSessions = () => {
    api
      .listSessions()
      .then((list) => setSessions(Array.isArray(list) ? list : []))
      .catch(() => {})
      .finally(() => setSessionsLoading(false));
  };

  const isAgentPage = pathname.startsWith("/agent");
  useEffect(() => {
    loadSessions();
  }, [isAgentPage, activeSessionId]);

  const [deleteTarget, setDeleteTarget] = useState<string | null>(null);
  const [renameTarget, setRenameTarget] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState("");

  const deleteSession = async (sid: string) => {
    try {
      await api.deleteSession(sid);
      setSessions((prev) => prev.filter((s) => s.session_id !== sid));
    } catch { /* ignore */ }
    setDeleteTarget(null);
  };

  const renameSession = async (sid: string) => {
    if (!renameValue.trim()) { setRenameTarget(null); return; }
    try {
      await api.renameSession(sid, renameValue.trim());
      setSessions((prev) =>
        prev.map((s) =>
          s.session_id === sid ? { ...s, title: renameValue.trim() } : s
        )
      );
    } catch { /* ignore */ }
    setRenameTarget(null);
  };

  const openExternalUrl = async (url: string) => {
    try {
      const { invoke } = await import("@tauri-apps/api/core");
      await invoke("open_external_url", { url });
    } catch {
      window.open(url, "_blank", "noopener,noreferrer");
    }
  };

  // ── route helpers ──
  const isActive = (to: string) =>
    to === "/" ? pathname === "/" : pathname.startsWith(to);

  return (
    <div className="flex h-screen overflow-hidden bg-background">
      {/* Sidebar */}
      <aside
        className={cn(
          "border-r bg-card flex flex-col shrink-0 transition-all duration-200",
          collapsed ? "w-12" : "w-60"
        )}
      >
        {/* ── top: user + primary nav ── */}
        <nav className={cn("space-y-0.5", collapsed ? "p-1" : "p-2")}>
          {!collapsed && <SectionLabel>{t("layout.section.workspace")}</SectionLabel>}

          <NavLink to="/" icon={BarChart3} label={t("layout.home")} collapsed={collapsed} isActive={isActive("/")} />
          <NavLink to="/dashboard" icon={LayoutDashboard} label={t("layout.dashboard")} collapsed={collapsed} isActive={isActive("/dashboard")} />
          <NavLink to="/market-pulse" icon={Activity} label={t("layout.marketPulse")} collapsed={collapsed} isActive={isActive("/market-pulse")} />
          <NavLink to="/indices" icon={LineChart} label={t("layout.indices", "指数")} collapsed={collapsed} isActive={isActive("/indices")} />
          <NavLink to="/agent" icon={Bot} label={t("layout.agent")} collapsed={collapsed} isActive={isActive("/agent")} />
          <NavLink to="/watchlist" icon={Eye} label={t("layout.watchlist")} collapsed={collapsed} isActive={isActive("/watchlist")} />
          <NavLink to="/reports" icon={FileText} label={t("layout.reports")} collapsed={collapsed} isActive={isActive("/reports")} />
          <NavLink to="/alpha-zoo" icon={Layers} label={t("layout.alphaZoo")} collapsed={collapsed} isActive={isActive("/alpha-zoo")} />

          {!collapsed && <SectionLabel>{t("layout.section.tools")}</SectionLabel>}

          <NavLink to="/correlation" icon={BarChart3} label={t("layout.correlation")} collapsed={collapsed} isActive={isActive("/correlation")} />
          <NavLink to="/runtime" icon={Activity} label={t("layout.runtime")} collapsed={collapsed} isActive={isActive("/runtime")} />
          <NavLink to="/settings" icon={Settings} label={t("layout.settings")} collapsed={collapsed} isActive={isActive("/settings")} />

          {/* docs */}
          <NavLink
            to="https://agent.nieanshow.cn/column/04-ai-trading/"
            icon={BookOpen}
            label={t("layout.docs")}
            collapsed={collapsed}
            isActive={false}
            external
          />

          {/* external — collapsible group */}
          {!collapsed && (
            <>
              <button
                type="button"
                onClick={() => setShowExternal((v) => !v)}
                className="flex w-full items-center gap-3 rounded-md px-3 py-2 text-sm transition-colors text-muted-foreground hover:bg-muted hover:text-foreground"
              >
                <Globe2 className="h-4 w-4 shrink-0" />
                <span className="flex-1 text-left">{t("layout.externalShortcuts.title")}</span>
                <span className={cn("text-[10px] text-muted-foreground/50 transition-transform", showExternal && "rotate-90")}>
                  ▶
                </span>
              </button>
              {showExternal &&
                EXTERNAL_SHORTCUTS.map((s) => (
                  <button
                    key={s.id}
                    type="button"
                    onClick={() => {
                      try { track("feature_use", { shortcut_id: s.id }, { name: "external_shortcut" }); } catch {}
                      openExternalUrl(s.url);
                    }}
                    className="flex items-center gap-3 rounded-md pl-8 pr-3 py-1.5 text-sm transition-colors text-muted-foreground hover:bg-muted hover:text-foreground w-full text-left"
                    title={t(s.descriptionKey)}
                  >
                    <s.icon className="h-4 w-4 shrink-0" />
                    {t(s.labelKey)}
                  </button>
                ))}
            </>
          )}
          {collapsed &&
            EXTERNAL_SHORTCUTS.map((s) => (
              <button
                key={s.id}
                type="button"
                onClick={() => {
                  try { track("feature_use", { shortcut_id: s.id }, { name: "external_shortcut" }); } catch {}
                  openExternalUrl(s.url);
                }}
                className="flex items-center justify-center p-2 w-full rounded-md text-muted-foreground hover:bg-muted hover:text-foreground transition-colors"
                title={t(s.labelKey)}
              >
                <s.icon className="h-4 w-4 shrink-0" />
              </button>
            ))}
        </nav>

        {/* ── middle: sessions ── */}
        {!collapsed && (
          <div className="flex-1 overflow-hidden border-t mt-2 flex flex-col min-h-0">
            <div className="flex items-center justify-between py-2 px-4">
              <span className="text-[11px] font-medium text-muted-foreground/60 uppercase tracking-wider select-none">
                {t("layout.sessions")}
              </span>
              <Link
                to="/agent"
                className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground transition-colors rounded"
                title={t("layout.newChat")}
                onClick={() => {
                  try { track("feature_use", {}, { name: "session_new" }); } catch {}
                }}
              >
                <Plus className="h-3.5 w-3.5" />
              </Link>
            </div>

            <div className="px-2 pb-2 space-y-0.5 overflow-auto flex-1">
              {sessionsLoading ? (
                <div className="space-y-1.5 px-2 py-1">
                  {[1, 2, 3].map((i) => (
                    <div key={i} className="h-7 rounded-md bg-muted/50 animate-pulse" />
                  ))}
                </div>
              ) : sessions.length === 0 ? (
                <p className="px-3 py-4 text-center text-xs text-muted-foreground/50">
                  {t("layout.noSessions")}
                </p>
              ) : null}
              {sessions.map((s) => {
                const active = s.session_id === activeSessionId;
                const isDeleting = deleteTarget === s.session_id;
                const isRenaming = renameTarget === s.session_id;
                return (
                  <div key={s.session_id} className="group relative flex items-center">
                    {isRenaming ? (
                      <input
                        autoFocus
                        value={renameValue}
                        onChange={(e) => setRenameValue(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") renameSession(s.session_id);
                          if (e.key === "Escape") setRenameTarget(null);
                        }}
                        onBlur={() => renameSession(s.session_id)}
                        className="flex-1 min-w-0 pl-3 pr-2 py-1 rounded-md text-xs border border-primary bg-background outline-none"
                      />
                    ) : (
                      <Link
                        to={`/agent?session=${s.session_id}`}
                        className={cn(
                          "flex-1 min-w-0 pl-3 pr-14 py-1.5 rounded-md text-xs transition-colors truncate block",
                          "focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-primary",
                          active
                            ? "bg-primary/10 text-primary font-medium"
                            : "text-muted-foreground hover:bg-muted hover:text-foreground"
                        )}
                        title={s.title || s.session_id}
                      >
                        <span className="flex items-center gap-1.5">
                          {streamingSessionId === s.session_id ? (
                            <Loader2 className="h-3 w-3 shrink-0 animate-spin text-primary" />
                          ) : (
                            <span
                              className={cn(
                                "h-1.5 w-1.5 rounded-full shrink-0",
                                active ? "bg-primary/70" : "bg-muted-foreground/40"
                              )}
                            />
                          )}
                          {s.title || s.session_id.slice(0, 16)}
                        </span>
                      </Link>
                    )}
                    {!isRenaming && isDeleting ? (
                      <div className="absolute right-0.5 flex items-center gap-0.5">
                        <button
                          onClick={() => {
                            try { track("feature_use", {}, { name: "session_delete" }); } catch {}
                            deleteSession(s.session_id);
                          }}
                          className="p-1 text-danger hover:bg-danger/10 rounded text-[10px] font-medium"
                        >
                          {t("layout.confirm")}
                        </button>
                        <button
                          onClick={() => setDeleteTarget(null)}
                          className="p-1 text-muted-foreground hover:bg-muted rounded text-[10px]"
                        >
                          {t("layout.cancel")}
                        </button>
                      </div>
                    ) : !isRenaming ? (
                      <div className="absolute right-1 opacity-0 group-hover:opacity-100 flex items-center gap-0.5 transition-opacity">
                        <button
                          onClick={(e) => {
                            e.preventDefault();
                            e.stopPropagation();
                            setRenameTarget(s.session_id);
                            setRenameValue(s.title || "");
                          }}
                          className="p-1 text-muted-foreground hover:text-foreground rounded"
                          title={t("layout.rename")}
                        >
                          <Pencil className="h-3 w-3" />
                        </button>
                        <button
                          onClick={(e) => {
                            e.preventDefault();
                            e.stopPropagation();
                            setDeleteTarget(s.session_id);
                          }}
                          className="p-1 text-muted-foreground hover:text-danger rounded"
                          title={t("layout.delete")}
                        >
                          <Trash2 className="h-3 w-3" />
                        </button>
                      </div>
                    ) : null}
                  </div>
                );
              })}
            </div>
          </div>
        )}

        {/* Spacer when collapsed */}
        {collapsed && <div className="flex-1" />}

        {/* ── bottom: theme / lang / collapse ── */}
        <div
          className={cn(
            "border-t",
            collapsed ? "p-1 flex flex-col items-center gap-1" : "px-3 py-2.5 flex items-center justify-between"
          )}
        >
          {collapsed ? (
            <>
              <button
                onClick={() => {
                  try { track("feature_use", {}, { name: "theme_toggle" }); } catch {}
                  toggle();
                }}
                className="p-1.5 text-muted-foreground hover:text-foreground rounded transition-colors"
                title={dark ? t("layout.light") : t("layout.dark")}
              >
                {dark ? <Sun className="h-3.5 w-3.5" /> : <Moon className="h-3.5 w-3.5" />}
              </button>
              <button
                onClick={() => {
                  try { track("feature_use", {}, { name: "sidebar_toggle" }); } catch {}
                  setCollapsed(false);
                }}
                className="p-1.5 text-muted-foreground hover:text-foreground rounded transition-colors"
                title={t("layout.expand")}
              >
                <ChevronsRight className="h-3.5 w-3.5" />
              </button>
            </>
          ) : (
            <>
              <button
                onClick={() => {
                  try { track("feature_use", {}, { name: "theme_toggle" }); } catch {}
                  toggle();
                }}
                className="flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground transition-colors rounded"
                title={dark ? t("layout.light") : t("layout.dark")}
              >
                {dark ? <Sun className="h-3.5 w-3.5" /> : <Moon className="h-3.5 w-3.5" />}
              </button>

              <button
                onClick={() => {
                  try { track("feature_use", {}, { name: "lang_toggle" }); } catch {}
                  i18nHook.changeLanguage(
                    i18nHook.language === "zh-CN" ? "en" : "zh-CN"
                  );
                }}
                className="text-xs text-muted-foreground hover:text-foreground transition-colors rounded"
              >
                <Languages className="h-3.5 w-3.5 inline -mt-px mr-0.5" />
                {i18nHook.language === "zh-CN" ? "EN" : "中"}
              </button>

              <span className="text-[10px] text-muted-foreground/40 tabular-nums">
                {APP_VERSION}
              </span>

              <button
                onClick={() => {
                  try { track("feature_use", {}, { name: "sidebar_toggle" }); } catch {}
                  setCollapsed(true);
                }}
                className="p-0.5 text-muted-foreground hover:text-foreground rounded transition-colors"
                title={t("layout.collapse")}
              >
                <ChevronsLeft className="h-3.5 w-3.5" />
              </button>
            </>
          )}
        </div>
      </aside>

      {/* Main */}
      <div
        data-testid="web-ui-main"
        className="flex-1 flex flex-col min-h-0 min-w-0 overflow-hidden"
      >
        <ConnectionBanner status={sseStatus} retryAttempt={sseRetryAttempt} />
        <main
          data-testid="web-ui-outlet"
          className="flex-1 min-h-0 min-w-0 overflow-auto"
        >
          <Outlet />
        </main>
      </div>
    </div>
  );
}
