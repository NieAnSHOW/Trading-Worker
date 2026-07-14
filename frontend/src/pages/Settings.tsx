import {
  getConsent,
  setConsent,
  // flushNow
} from "@/lib/telemetry";
import i18n from "@/i18n";
import { useEffect, useMemo, useState, type FormEvent } from "react";
import {
  Database,
  KeyRound,
  Loader2,
  MessageSquareMore,
  Package,
  Play,
  QrCode,
  RefreshCw,
  RotateCcw,
  Save,
  Server,
  SlidersHorizontal,
  Square,
  // Upload,
} from "lucide-react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import {
  api,
  isAuthRequiredError,
  type ChannelRuntimeStatus,
  type DataSourceSettings,
  type LLMProviderOption,
  type LLMSettings,
} from "@/lib/api";
import { getApiAuthKey, setApiAuthKey } from "@/lib/apiAuth";
import { OptionalDepsManager } from "@/components/settings/OptionalDepsManager";
import { QVerisSettings } from "@/components/settings/QVerisSettings"; // QVERIS-INTEGRATION

interface LLMFormState {
  provider: string;
  model_name: string;
  base_url: string;
  temperature: number;
  timeout_seconds: number;
  max_retries: number;
  reasoning_effort: string;
}

const fieldClass =
  "w-full rounded-md border bg-background px-3 py-2 text-sm outline-none transition focus:border-primary focus:ring-2 focus:ring-primary/20 disabled:cursor-not-allowed disabled:opacity-60";
const labelClass = "text-sm font-medium";
const hintClass = "text-xs text-muted-foreground";

function toForm(settings: LLMSettings): LLMFormState {
  return {
    provider: settings.provider,
    model_name: settings.model_name,
    base_url: settings.base_url,
    temperature: settings.temperature,
    timeout_seconds: settings.timeout_seconds,
    max_retries: settings.max_retries,
    reasoning_effort: settings.reasoning_effort || "",
  };
}

// VIP Server 现为后端一等公民 provider(agent/src/providers/llm_providers.json +
// capabilities.py::_PROVIDERS)。前端只保留默认值/判定常量,与后端 json 对齐。
const VIP_PROVIDER_NAME = "vip_server";
const VIP_DEFAULTS = {
  model: "hy3",
  base_url: "https://new.ailjf.cc/v1",
} as const;

// ponytail: "用户是否主动切换过 provider"的本地标记。
// 未标记 = 全新安装/未选过其他供应商 → 默认 VIP;一旦在下拉里切换过就置 "1",之后尊重 .env 当前值。
const PROVIDER_PICKED_KEY = "vt_provider_picked";

export function Settings() {
  const { t } = useTranslation();
  const [settings, setSettings] = useState<LLMSettings | null>(null);
  const [dataSettings, setDataSettings] = useState<DataSourceSettings | null>(
    null,
  );
  const [channelStatus, setChannelStatus] =
    useState<ChannelRuntimeStatus | null>(null);
  const [form, setForm] = useState<LLMFormState | null>(null);
  const [apiKey, setApiKey] = useState("");
  const [localApiKey, setLocalApiKeyState] = useState(() => getApiAuthKey());
  const [clearApiKey, setClearApiKey] = useState(false);
  const [tushareToken, setTushareToken] = useState("");
  const [clearTushareToken, setClearTushareToken] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [dataSaving, setDataSaving] = useState(false);
  const [channelRefreshing, setChannelRefreshing] = useState(false);
  const [channelAction, setChannelAction] = useState<"start" | "stop" | null>(
    null,
  );
  const [pairingCommand, setPairingCommand] = useState("");
  const [pairingBusy, setPairingBusy] = useState(false);
  const [settingsLoadError, setSettingsLoadError] = useState<string | null>(
    null,
  );
  const [usageDataOn, setUsageDataOn] = useState(getConsent());
  // const [flushing, setFlushing] = useState(false);
  const [optionalDeps, setOptionalDeps] = useState<any>(null);
  const [installingPkg, setInstallingPkg] = useState<string | null>(null);

  // WeChat QR login state
  const [weixinQr, setWeixinQr] = useState<{
    loginId: string;
    image: string;
  } | null>(null);
  const [weixinPolling, setWeixinPolling] = useState(false);

  const toggleUsageData = async (on: boolean) => {
    setUsageDataOn(on);
    await setConsent(on);
    toast.success(
      on ? i18n.t("settings.usageData.on") : i18n.t("settings.usageData.off"),
    );
  };

  // 手动触发一次埋点上传（含当天），用于验证上传通路；正常流程是隔天启动自动 flush。
  // const handleTestUpload = async () => {
  //   setFlushing(true);
  //   try {
  //     const { uploaded, retained } = await flushNow({ forceAll: true });
  //     if (uploaded > 0) {
  //       toast.success(
  //         `已上传 ${uploaded} 个埋点批次${retained ? `,${retained} 个保留重试` : ""}`,
  //       );
  //     } else if (retained > 0) {
  //       toast.error(`上传失败,${retained} 个批次保留待重试`);
  //     } else {
  //       toast.info("没有待上传的埋点数据");
  //     }
  //   } catch (error) {
  //     toast.error(
  //       `上传失败:${error instanceof Error ? error.message : "Unknown error"}`,
  //     );
  //   } finally {
  //     setFlushing(false);
  //   }
  // };

  useEffect(() => {
    let alive = true;
    Promise.allSettled([
      api.getLLMSettings(),
      api.getDataSourceSettings(),
      api.getChannelStatus(),
      api.listOptionalDeps(),
    ])
      .then(([llmResult, dataSourceResult, channelResult, depsResult]) => {
        if (!alive) return;

        // ponytail: allSettled — 单接口失败不拖垮整页 (upstream 容错语义)。
        // LLM 分支保留 fork 的 VIP 默认值逻辑 (未主动选过 provider 时默认 VIP)。
        if (llmResult.status === "fulfilled") {
          const llmData = llmResult.value;
          setSettings(llmData);
          const picked = localStorage.getItem(PROVIDER_PICKED_KEY) === "1";
          const formInit = toForm(llmData);
          setForm(
            picked
              ? formInit
              : {
                  ...formInit,
                  provider: VIP_PROVIDER_NAME,
                  model_name: VIP_DEFAULTS.model,
                  base_url: VIP_DEFAULTS.base_url,
                },
          );
        } else {
          const message =
            llmResult.reason instanceof Error
              ? llmResult.reason.message
              : "Unknown error";
          setSettingsLoadError(message);
          if (isAuthRequiredError(llmResult.reason)) {
            toast.error(message);
          } else {
            toast.error(`Failed to load LLM settings: ${message}`);
          }
        }

        if (dataSourceResult.status === "fulfilled") {
          setDataSettings(dataSourceResult.value);
        } else {
          const message =
            dataSourceResult.reason instanceof Error
              ? dataSourceResult.reason.message
              : "Unknown error";
          setSettingsLoadError(message);
          if (isAuthRequiredError(dataSourceResult.reason)) {
            toast.error(message);
          } else {
            toast.error(`Failed to load data source settings: ${message}`);
          }
        }

        if (channelResult.status === "fulfilled") {
          setChannelStatus(channelResult.value);
        } else {
          // channel status 失败不拖垮整页: 保留 LLM/data source 可用, channel 区降级为空。
          const message =
            channelResult.reason instanceof Error
              ? channelResult.reason.message
              : "Unknown error";
          toast.error(`Failed to load channel status: ${message}`);
          setChannelStatus(null);
        }

        if (depsResult.status === "fulfilled") {
          setOptionalDeps(depsResult.value);
        }
        // optional-deps 失败不报错: 桌面可选依赖面板静默降级即可, 不阻塞设置页。
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, []);

  const refreshChannelStatus = async () => {
    setChannelRefreshing(true);
    try {
      const [channelData, depsData] = await Promise.all([
        api.getChannelStatus(),
        api.listOptionalDeps(),
      ]);
      setChannelStatus(channelData);
      setOptionalDeps(depsData);
    } catch (error) {
      toast.error(
        `${t("settings.channels.refreshFailed")}: ${error instanceof Error ? error.message : "Unknown error"}`,
      );
    } finally {
      setChannelRefreshing(false);
    }
  };

  const matchedPkg = (channelName: string, deps: any): string | undefined => {
    if (!deps?.brokers) return undefined;
    // ponytail: match channel name to optional-deps broker id (financial SDKs),
    // not IM channels — wired for future overlap.
    const entry = deps.brokers.find(
      (b: any) =>
        b.id?.toLowerCase() === channelName.toLowerCase() ||
        b.package?.toLowerCase().includes(channelName.toLowerCase()),
    );
    return entry?.package;
  };

  const installChannelDep = async (pkg: string) => {
    setInstallingPkg(pkg);
    try {
      const { job_id } = await api.installOptionalDep(pkg);
      const streamUrl = await api.optionalDepStatusUrl(job_id);
      await new Promise<void>((resolve, reject) => {
        const es = new EventSource(streamUrl);
        es.addEventListener("done", () => {
          es.close();
          resolve();
        });
        es.addEventListener("failed", (ev) => {
          es.close();
          let message = t("settings.channels.depInstallFailed");
          try {
            message = JSON.parse((ev as MessageEvent).data).error || message;
          } catch {
            /* default */
          }
          reject(new Error(message));
        });
        es.onerror = () => {
          es.close();
          reject(new Error(t("settings.channels.depInstallFailed")));
        };
      });
      toast.success(t("settings.channels.depInstalled"));
      await refreshChannelStatus();
    } catch (error) {
      toast.error(
        `${t("settings.channels.depInstallFailed")}: ${error instanceof Error ? error.message : "Unknown error"}`,
      );
    } finally {
      setInstallingPkg(null);
    }
  };

  const setChannelsRunning = async (action: "start" | "stop") => {
    setChannelAction(action);
    try {
      const updated =
        action === "start"
          ? await api.startChannels()
          : await api.stopChannels();
      setChannelStatus(updated);
      toast.success(
        action === "start"
          ? t("settings.channels.started")
          : t("settings.channels.stoppedToast"),
      );
    } catch (error) {
      toast.error(
        `${action === "start" ? t("settings.channels.startFailed") : t("settings.channels.stopFailed")}: ${error instanceof Error ? error.message : "Unknown error"}`,
      );
    } finally {
      setChannelAction(null);
    }
  };

  const submitPairingCommand = async (event: FormEvent) => {
    event.preventDefault();
    const command = pairingCommand.trim();
    if (!command) return;
    setPairingBusy(true);
    try {
      // ponytail: 仅微信开放,pairing 命令固定走 weixin
      const updated = await api.runChannelPairingCommand({
        channel: "weixin",
        command,
      });
      toast.success(updated.reply);
      setPairingCommand("");
    } catch (error) {
      toast.error(
        `Failed to run pairing command: ${error instanceof Error ? error.message : "Unknown error"}`,
      );
    } finally {
      setPairingBusy(false);
    }
  };

  const startWeixinQrLogin = async () => {
    try {
      const { login_id, qr_image } = await api.startWeixinLogin();
      setWeixinQr({ loginId: login_id, image: qr_image });
      // qr_image 实为微信扫码跳转链接(非图片二进制);新页签打开让用户在微信侧完成扫码,
      // 本页弹窗仅做"等待登录"轮询。async await 后 window.open 可能被浏览器拦弹窗,modal 内有兜底按钮。
      if (qr_image?.startsWith("http")) {
        window.open(qr_image, "_blank", "noopener,noreferrer");
      }
    } catch (error) {
      toast.error(
        `获取微信二维码失败: ${error instanceof Error ? error.message : "Unknown error"}`,
      );
    }
  };

  // Poll WeChat QR login status
  useEffect(() => {
    if (!weixinQr) return;
    setWeixinPolling(true);
    const id = setInterval(async () => {
      try {
        const { status } = await api.weixinLoginStatus(weixinQr.loginId);
        if (status === "confirmed") {
          clearInterval(id);
          setWeixinQr(null);
          setWeixinPolling(false);
          toast.success("微信登录成功");
          // 后端在 login/status 确认时已自动重启 weixin 通道加载新身份;
          // 前端只需刷新状态展示。
          await refreshChannelStatus();
        } else if (status === "expired") {
          clearInterval(id);
          setWeixinQr(null);
          setWeixinPolling(false);
          toast.error("二维码已过期，请重新获取");
        }
        // wait / scaned_but_redirect: continue polling
      } catch {
        // network error during poll: keep trying
      }
    }, 2000);
    return () => {
      clearInterval(id);
      setWeixinPolling(false);
    };
  }, [weixinQr]);

  const providers = settings?.providers ?? [];
  const selectedProvider = useMemo<LLMProviderOption | undefined>(
    () => providers.find((provider) => provider.name === form?.provider),
    [form?.provider, providers],
  );

  const applyProviderDefaults = (provider = selectedProvider) => {
    if (!provider || !form) return;
    setForm({
      ...form,
      model_name: provider.default_model,
      base_url: provider.default_base_url,
    });
  };

  const onProviderChange = (name: string) => {
    const provider = providers.find((item) => item.name === name);
    if (!provider || !form) return;
    localStorage.setItem(PROVIDER_PICKED_KEY, "1");
    setForm({
      ...form,
      provider: provider.name,
      model_name: provider.default_model,
      base_url: provider.default_base_url,
    });
    setApiKey("");
    setClearApiKey(false);
  };

  const submitLocalApiKey = (event: FormEvent) => {
    event.preventDefault();
    setApiAuthKey(localApiKey);
    toast.success("Local API key saved");
    window.location.reload();
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!form) return;
    setSaving(true);
    try {
      const updated = await api.updateLLMSettings({
        ...form,
        api_key: apiKey.trim() || undefined,
        clear_api_key: clearApiKey,
      });
      setSettings(updated);
      setForm(toForm(updated));
      setApiKey("");
      setClearApiKey(false);
      toast.success("LLM settings saved");
    } catch (error) {
      toast.error(
        `Failed to save LLM settings: ${error instanceof Error ? error.message : "Unknown error"}`,
      );
    } finally {
      setSaving(false);
    }
  };

  const submitDataSources = async (event: FormEvent) => {
    event.preventDefault();
    setDataSaving(true);
    try {
      const updated = await api.updateDataSourceSettings({
        tushare_token: tushareToken.trim() || undefined,
        clear_tushare_token: clearTushareToken,
      });
      setDataSettings(updated);
      setTushareToken("");
      setClearTushareToken(false);
      toast.success("Data source settings saved");
    } catch (error) {
      toast.error(
        `Failed to save data source settings: ${error instanceof Error ? error.message : "Unknown error"}`,
      );
    } finally {
      setDataSaving(false);
    }
  };

  const localApiAccessSection = (
    <form
      onSubmit={submitLocalApiKey}
      className="rounded-lg border bg-card p-5 shadow-sm"
    >
      <div className="mb-4 space-y-1">
        <div className="flex items-center gap-2">
          <KeyRound className="h-4 w-4 text-primary" />
          <h2 className="text-base font-semibold">{"Local API access"}</h2>
        </div>
        <p className="text-sm text-muted-foreground">
          {
            "For remote or private Web UI deployments, enter the server API key once in this browser. Localhost use can stay blank."
          }
        </p>
      </div>
      <div className="grid gap-3 md:grid-cols-[minmax(0,1fr)_auto]">
        <label className="grid gap-2">
          <span className={labelClass}>{"Server API key"}</span>
          <input
            type="password"
            value={localApiKey}
            onChange={(event) => setLocalApiKeyState(event.target.value)}
            className={fieldClass}
            placeholder={
              "Stored only in this browser. Leave blank to clear it."
            }
            autoComplete="current-password"
          />
        </label>
        <button
          type="submit"
          className="inline-flex items-center justify-center gap-2 self-end rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition hover:opacity-90"
        >
          <Save className="h-4 w-4" />
          {i18n.t("settings.save")}
        </button>
      </div>
      <p className="mt-2 text-xs text-muted-foreground">
        {"Stored only in this browser. Leave blank to clear it."}
      </p>
    </form>
  );

  if (loading || !form || !settings || !dataSettings) {
    return (
      <div className="mx-auto max-w-5xl space-y-6 p-6">
        <div className="space-y-2">
          <h1 className="text-2xl font-semibold tracking-tight">
            {"Settings"}
          </h1>
          <p className="max-w-3xl text-sm text-muted-foreground">
            {
              "Configure model credentials and market data source tokens for this local project."
            }
          </p>
        </div>
        {localApiAccessSection}
        {/* QVERIS-INTEGRATION */}
        <QVerisSettings />
        <div className="rounded-lg border bg-card p-5 shadow-sm">
          <div className="mb-2 flex items-center justify-between">
            <h2 className="text-base font-semibold">
              {i18n.t("settings.usageData.title")}
            </h2>
            <button
              type="button"
              role="switch"
              aria-checked={usageDataOn}
              onClick={() => toggleUsageData(!usageDataOn)}
              className={`relative h-6 w-11 rounded-full transition ${usageDataOn ? "bg-primary" : "bg-muted"}`}
            >
              <span
                className={`absolute top-0.5 h-5 w-5 rounded-full bg-white transition ${usageDataOn ? "left-[22px]" : "left-0.5"}`}
              />
            </button>
          </div>
          <p className="text-sm text-muted-foreground">
            {i18n.t("settings.usageData.description")}
          </p>
        </div>
        <div className="flex min-h-32 items-center justify-center rounded-lg border bg-card p-5 text-sm text-muted-foreground">
          {settingsLoadError ? (
            <div className="text-center">
              <div className="font-medium text-foreground">
                {"Settings are unavailable"}
              </div>
              <div className="mt-1">{settingsLoadError}</div>
            </div>
          ) : (
            <>
              <Loader2 className="mr-2 h-4 w-4 animate-spin" />
              {"Loading..."}
            </>
          )}
        </div>
      </div>
    );
  }

  const keyStatus = settings.api_key_configured
    ? "Configured"
    : settings.api_key_required
      ? "Leave blank to keep the current key"
      : selectedProvider?.auth_type === "oauth" &&
          selectedProvider.login_command
        ? `This provider uses OAuth. Run: ${selectedProvider.login_command}`
        : "This provider does not require an API key.";
  const apiKeyDisabled = !selectedProvider?.api_key_required || clearApiKey;
  const tushareStatus = dataSettings.tushare_token_configured
    ? "Configured"
    : "Leave blank to keep the current token";
  // ponytail: 仅展示微信渠道。其他 IM 渠道暂不开放,不在 WebUI 中露出。
  const channelRows = Object.entries(channelStatus?.channels ?? {})
    .filter(([name]) => name === "weixin")
    .sort(([a], [b]) => a.localeCompare(b));
  const channelEnabledCount = channelRows.filter(
    ([, item]) => item.enabled,
  ).length;
  const channelLoadedCount = channelRows.filter(
    ([, item]) => item.loaded,
  ).length;
  const channelUnavailableCount = channelRows.filter(
    ([, item]) => item.available === false,
  ).length;
  const channelBusy = channelRefreshing || channelAction !== null;
  // channel 状态未知时(getChannelStatus 失败, channelStatus=null)禁用启停,
  // 但 Refresh 仍可用 —— 与 upstream 降级语义一致(状态未知不盲目 start/stop)。
  const channelControlsDisabled = channelBusy || !channelStatus;
  // 桌面端登录后,.env 由 console 自动注入 VIP 大模型配置,WebUI 不再需要手配。
  const hideLlm = !!settings.desktop_login_provisioned;

  return (
    <div className="mx-auto max-w-5xl space-y-6 p-6">
      <div className="space-y-2">
        <h1 className="text-2xl font-semibold tracking-tight">{"Settings"}</h1>
        <p className="max-w-3xl text-sm text-muted-foreground">
          {
            "Configure model credentials and market data source tokens for this local project."
          }
        </p>
      </div>

      {/* {localApiAccessSection} */}
      {/* LLM Settings */}
      {hideLlm ? (
        <div className="rounded-lg border bg-card p-5 shadow-sm flex items-center gap-3 text-sm text-muted-foreground">
          <Server className="h-4 w-4 text-primary shrink-0" />
          <span>大模型已由桌面端登录自动配置（VIP），无需手动设置。</span>
        </div>
      ) : (
        <>
          <div className="space-y-2">
            <h2 className="text-lg font-semibold tracking-tight">
              {"LLM Settings"}
            </h2>
            <p className="max-w-3xl text-sm text-muted-foreground">
              {
                "Choose the model used by the agent and save it to the project-local agent/.env file."
              }
            </p>
          </div>

          <form
            onSubmit={submit}
            className="grid gap-6 lg:grid-cols-[minmax(0,1.4fr)_minmax(320px,0.8fr)]"
          >
            <section className="rounded-lg border bg-card p-5 shadow-sm">
              <div className="mb-5 flex items-center gap-2">
                <Server className="h-4 w-4 text-primary" />
                <h2 className="text-base font-semibold">{"Connection"}</h2>
              </div>

              <div className="grid gap-4">
                <label className="grid gap-2">
                  <span className={labelClass}>
                    {i18n.t("settings.provider")}
                  </span>
                  <select
                    value={form.provider}
                    onChange={(event) => onProviderChange(event.target.value)}
                    className={fieldClass}
                  >
                    {providers.map((provider) => (
                      <option key={provider.name} value={provider.name}>
                        {provider.label}
                      </option>
                    ))}
                  </select>
                  <span className={hintClass}>
                    {
                      "Changing providers updates the recommended model and endpoint."
                    }
                  </span>
                </label>

                {form.provider !== VIP_PROVIDER_NAME && (
                  <label className="grid gap-2">
                    <span className={labelClass}>{"Model"}</span>
                    <div className="flex gap-2">
                      <input
                        value={form.model_name}
                        onChange={(event) =>
                          setForm({ ...form, model_name: event.target.value })
                        }
                        className={fieldClass}
                        required
                      />
                      <button
                        type="button"
                        onClick={() => applyProviderDefaults()}
                        className="inline-flex shrink-0 items-center gap-2 rounded-md border px-3 py-2 text-sm text-muted-foreground transition hover:bg-muted hover:text-foreground"
                        title={"Use provider defaults"}
                      >
                        <RotateCcw className="h-4 w-4" />
                        <span className="hidden sm:inline">
                          {"Use provider defaults"}
                        </span>
                      </button>
                    </div>
                    <span className={hintClass}>
                      {"Use the exact model id required by your provider."}
                    </span>
                  </label>
                )}

                {form.provider !== VIP_PROVIDER_NAME && (
                  <label className="grid gap-2">
                    <span className={labelClass}>
                      {i18n.t("settings.baseUrl")}
                    </span>
                    <input
                      value={form.base_url}
                      onChange={(event) =>
                        setForm({ ...form, base_url: event.target.value })
                      }
                      className={fieldClass}
                      placeholder={selectedProvider?.default_base_url}
                      disabled={selectedProvider?.auth_type === "oauth"}
                    />
                  </label>
                )}

                <label className="grid gap-2">
                  <span className={labelClass}>
                    {selectedProvider?.auth_type === "oauth"
                      ? "OAuth"
                      : "API key"}
                  </span>
                  <div className="relative">
                    <KeyRound className="pointer-events-none absolute left-3 top-2.5 h-4 w-4 text-muted-foreground" />
                    <input
                      type="password"
                      value={apiKey}
                      onChange={(event) => setApiKey(event.target.value)}
                      className={`${fieldClass} pl-9`}
                      placeholder={keyStatus}
                      autoComplete="current-password"
                      disabled={apiKeyDisabled}
                    />
                  </div>
                  <div className="flex items-center justify-between gap-3">
                    <span className={hintClass}>{keyStatus}</span>
                    {selectedProvider?.api_key_required ? (
                      <label className="flex shrink-0 items-center gap-2 text-xs text-muted-foreground">
                        <input
                          type="checkbox"
                          checked={clearApiKey}
                          onChange={(event) => {
                            setClearApiKey(event.target.checked);
                            if (event.target.checked) setApiKey("");
                          }}
                          className="h-3.5 w-3.5 accent-primary"
                        />
                        {"Clear saved API key"}
                      </label>
                    ) : null}
                  </div>
                </label>
              </div>
            </section>

            <section className="rounded-lg border bg-card p-5 shadow-sm">
              <div className="mb-5 flex items-center gap-2">
                <SlidersHorizontal className="h-4 w-4 text-primary" />
                <h2 className="text-base font-semibold">{"Generation"}</h2>
              </div>

              <div className="grid gap-4">
                <label className="grid gap-2">
                  <span className={labelClass}>
                    {i18n.t("settings.temperature")}
                  </span>
                  <input
                    type="number"
                    min={0}
                    max={2}
                    step={0.1}
                    value={form.temperature}
                    onChange={(event) =>
                      setForm({
                        ...form,
                        temperature: Number(event.target.value),
                      })
                    }
                    className={fieldClass}
                  />
                </label>

                <label className="grid gap-2">
                  <span className={labelClass}>
                    {i18n.t("settings.timeoutSeconds")}
                  </span>
                  <input
                    type="number"
                    min={1}
                    max={3600}
                    step={1}
                    value={form.timeout_seconds}
                    onChange={(event) =>
                      setForm({
                        ...form,
                        timeout_seconds: Number(event.target.value),
                      })
                    }
                    className={fieldClass}
                  />
                </label>

                <label className="grid gap-2">
                  <span className={labelClass}>{"Max retries"}</span>
                  <input
                    type="number"
                    min={0}
                    max={20}
                    step={1}
                    value={form.max_retries}
                    onChange={(event) =>
                      setForm({
                        ...form,
                        max_retries: Number(event.target.value),
                      })
                    }
                    className={fieldClass}
                  />
                </label>

                <label className="grid gap-2">
                  <span className={labelClass}>
                    {i18n.t("settings.reasoningEffort")}
                  </span>
                  <select
                    value={form.reasoning_effort}
                    onChange={(event) =>
                      setForm({ ...form, reasoning_effort: event.target.value })
                    }
                    className={fieldClass}
                  >
                    <option value="">{"Off"}</option>
                    <option value="low">low</option>
                    <option value="medium">medium</option>
                    <option value="high">high</option>
                    <option value="max">max</option>
                  </select>
                  <span className={hintClass}>
                    {
                      "How hard the model thinks before answering. Higher is more thorough but slower; leave Off for fastest replies."
                    }
                  </span>
                </label>

                <div className="rounded-md border bg-muted/30 px-3 py-2 text-xs text-muted-foreground">
                  <span className="font-medium text-foreground">
                    {i18n.t("settings.saved")}:{" "}
                  </span>
                  <span className="break-all font-mono">
                    {settings.env_path}
                  </span>
                </div>

                <button
                  type="submit"
                  disabled={saving}
                  className="inline-flex items-center justify-center gap-2 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-70"
                >
                  {saving ? (
                    <Loader2 className="h-4 w-4 animate-spin" />
                  ) : (
                    <Save className="h-4 w-4" />
                  )}
                  {saving ? i18n.t("settings.saving") : i18n.t("settings.save")}
                </button>
              </div>
            </section>
          </form>
        </>
      )}
      {/* QVERIS-INTEGRATION */}
      <QVerisSettings />

      {/* Usage data consent */}
      <div className="rounded-lg border bg-card p-5 shadow-sm">
        <div className="mb-2 flex items-center justify-between">
          <h2 className="text-base font-semibold">
            {i18n.t("settings.usageData.title")}
          </h2>
          <button
            type="button"
            role="switch"
            aria-checked={usageDataOn}
            onClick={() => toggleUsageData(!usageDataOn)}
            className={`relative h-6 w-11 rounded-full transition ${usageDataOn ? "bg-primary" : "bg-muted"}`}
          >
            <span
              className={`absolute top-0.5 h-5 w-5 rounded-full bg-white transition ${usageDataOn ? "left-[22px]" : "left-0.5"}`}
            />
          </button>
        </div>
        <p className="text-sm text-muted-foreground">
          {i18n.t("settings.usageData.description")}
        </p>
        {/* <button
          type="button"
          onClick={handleTestUpload}
          disabled={flushing}
          className="mt-3 inline-flex items-center gap-2 rounded-md border px-3 py-1.5 text-xs text-muted-foreground transition hover:bg-muted hover:text-foreground disabled:cursor-not-allowed disabled:opacity-60"
        >
          {flushing ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
          ) : (
            <Upload className="h-3.5 w-3.5" />
          )}
          测试上传埋点数据
        </button> */}
      </div>

      {/* IM channels */}
      <section className="rounded-lg border bg-card p-5 shadow-sm">
        <div className="mb-5 flex flex-col gap-4 md:flex-row md:items-start md:justify-between">
          <div className="space-y-1">
            <div className="flex items-center gap-2">
              <MessageSquareMore className="h-4 w-4 text-primary" />
              <h2 className="text-base font-semibold">
                {t("settings.channels.title")}
              </h2>
            </div>
            <p className="max-w-3xl text-sm text-muted-foreground">
              {t("settings.channels.description")}
            </p>
          </div>
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              onClick={refreshChannelStatus}
              disabled={channelBusy}
              className="inline-flex items-center justify-center gap-2 rounded-md border px-3 py-2 text-sm text-muted-foreground transition hover:bg-muted hover:text-foreground disabled:cursor-not-allowed disabled:opacity-60"
            >
              {channelRefreshing ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <RefreshCw className="h-4 w-4" />
              )}
              {t("settings.channels.refresh")}
            </button>
            <button
              type="button"
              onClick={() => setChannelsRunning("start")}
              disabled={channelControlsDisabled}
              className="inline-flex items-center justify-center gap-2 rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground transition hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-60"
            >
              {channelAction === "start" ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <Play className="h-4 w-4" />
              )}
              {t("settings.channels.start")}
            </button>
            <button
              type="button"
              onClick={() => setChannelsRunning("stop")}
              disabled={channelControlsDisabled}
              className="inline-flex items-center justify-center gap-2 rounded-md border px-3 py-2 text-sm text-muted-foreground transition hover:bg-muted hover:text-foreground disabled:cursor-not-allowed disabled:opacity-60"
            >
              {channelAction === "stop" ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <Square className="h-4 w-4" />
              )}
              {t("settings.channels.stop")}
            </button>
          </div>
        </div>

        <div className="mb-4 grid gap-3 md:grid-cols-4">
          <div className="rounded-md border bg-muted/20 px-3 py-2">
            <div className="text-xs text-muted-foreground">
              {t("settings.channels.runtime")}
            </div>
            <div className="text-sm font-medium">
              {channelStatus?.running
                ? t("settings.channels.running")
                : t("settings.channels.stopped")}
            </div>
          </div>
          <div className="rounded-md border bg-muted/20 px-3 py-2">
            <div className="text-xs text-muted-foreground">
              {t("settings.channels.enabled")}
            </div>
            <div className="text-sm font-medium">{channelEnabledCount}</div>
          </div>
          <div className="rounded-md border bg-muted/20 px-3 py-2">
            <div className="text-xs text-muted-foreground">
              {t("settings.channels.loaded")}
            </div>
            <div className="text-sm font-medium">{channelLoadedCount}</div>
          </div>
          <div className="rounded-md border bg-muted/20 px-3 py-2">
            <div className="text-xs text-muted-foreground">
              {t("settings.channels.unavailable")}
            </div>
            <div className="text-sm font-medium">{channelUnavailableCount}</div>
          </div>
        </div>

        <div className="overflow-hidden rounded-md border">
          <table className="w-full text-sm">
            <thead className="bg-muted/40 text-xs text-muted-foreground">
              <tr>
                <th className="px-3 py-2 text-left font-medium">
                  {t("settings.channels.channel")}
                </th>
                <th className="px-3 py-2 text-left font-medium">
                  {t("settings.channels.state")}
                </th>
                <th className="px-3 py-2 text-left font-medium">
                  {t("settings.channels.recovery")}
                </th>
              </tr>
            </thead>
            <tbody>
              {channelRows.map(([name, item]) => (
                <tr key={name} className="border-t">
                  <td className="px-3 py-2 align-top">
                    <div className="font-medium">
                      {item.display_name || name}
                    </div>
                    <div className="text-xs text-muted-foreground">{name}</div>
                  </td>
                  <td className="px-3 py-2 align-top">
                    <div className="flex flex-wrap gap-1.5">
                      <span
                        className={`rounded-full px-2 py-0.5 text-xs ${item.enabled ? "bg-primary/10 text-primary" : "bg-muted text-muted-foreground"}`}
                      >
                        {item.enabled
                          ? t("settings.channels.enabled")
                          : t("settings.channels.disabled")}
                      </span>
                      <span
                        className={`rounded-full px-2 py-0.5 text-xs ${item.loaded ? "bg-success/10 text-success" : "bg-muted text-muted-foreground"}`}
                      >
                        {item.loaded
                          ? t("settings.channels.loaded")
                          : t("settings.channels.notLoaded")}
                      </span>
                      {item.health === "expired" ? (
                        <span className="rounded-full px-2 py-0.5 text-xs bg-destructive/10 text-destructive">
                          {t("settings.channels.loginExpired")}
                        </span>
                      ) : (
                        <span
                          className={`rounded-full px-2 py-0.5 text-xs ${item.running ? "bg-success/10 text-success" : "bg-muted text-muted-foreground"}`}
                        >
                          {item.running
                            ? t("settings.channels.running")
                            : t("settings.channels.stopped")}
                        </span>
                      )}
                    </div>
                  </td>
                  <td className="max-w-md px-3 py-2 align-top text-xs text-muted-foreground">
                    <div className="flex flex-wrap items-center gap-2">
                      <span>
                        {item.install_hint ||
                          item.error ||
                          t("settings.channels.noRecovery")}
                      </span>
                      {item.available === false &&
                        matchedPkg(item.name, optionalDeps) && (
                          <button
                            type="button"
                            disabled={installingPkg !== null}
                            onClick={() =>
                              installChannelDep(
                                matchedPkg(item.name, optionalDeps)!,
                              )
                            }
                            className="inline-flex items-center gap-1 rounded-md border px-2 py-1 text-xs transition hover:bg-muted disabled:cursor-not-allowed disabled:opacity-60"
                          >
                            {installingPkg ===
                            matchedPkg(item.name, optionalDeps) ? (
                              <Loader2 className="h-3 w-3 animate-spin" />
                            ) : null}
                            {t("settings.channels.installDep")}
                          </button>
                        )}
                      {name === "weixin" && item.enabled && (
                        <button
                          type="button"
                          disabled={weixinPolling}
                          onClick={startWeixinQrLogin}
                          className="inline-flex items-center gap-1 rounded-md border px-2 py-1 text-xs text-primary transition hover:bg-primary/10 disabled:cursor-not-allowed disabled:opacity-60"
                        >
                          {weixinPolling ? (
                            <Loader2 className="h-3 w-3 animate-spin" />
                          ) : (
                            <QrCode className="h-3 w-3" />
                          )}
                          扫码登录
                        </button>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {/* ponytail: 仅微信开放,channel 固定 weixin,不再展示渠道选择器 */}
        <form
          onSubmit={submitPairingCommand}
          className="mt-4 grid gap-3 md:grid-cols-[minmax(0,1fr)_auto]"
        >
          <label className="grid gap-2">
            <span className={labelClass}>{"Pairing command"}</span>
            <input
              value={pairingCommand}
              onChange={(event) => setPairingCommand(event.target.value)}
              className={fieldClass}
              placeholder={"approve UM59-EGIT"}
            />
          </label>
          <button
            type="submit"
            disabled={pairingBusy || !pairingCommand.trim()}
            className="inline-flex items-center justify-center gap-2 self-end rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-60"
          >
            {pairingBusy ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : (
              <MessageSquareMore className="h-4 w-4" />
            )}
            {"Run pairing"}
          </button>
        </form>
      </section>

      <form
        onSubmit={submitDataSources}
        className="rounded-lg border bg-card p-5 shadow-sm"
      >
        <div className="mb-5 space-y-1">
          <div className="flex items-center gap-2">
            <Database className="h-4 w-4 text-primary" />
            <h2 className="text-base font-semibold">
              {"Data Source Settings"}
            </h2>
          </div>
          <p className="text-sm text-muted-foreground">
            {
              "Configure optional market data credentials used by backtests and research agents."
            }
          </p>
        </div>

        <div className="grid gap-5 lg:grid-cols-[minmax(0,1.1fr)_minmax(280px,0.9fr)]">
          <div className="grid gap-4">
            <label className="grid gap-2">
              <span className={labelClass}>{"Tushare token"}</span>
              <div className="relative">
                <KeyRound className="pointer-events-none absolute left-3 top-2.5 h-4 w-4 text-muted-foreground" />
                <input
                  type="password"
                  value={tushareToken}
                  onChange={(event) => setTushareToken(event.target.value)}
                  className={`${fieldClass} pl-9`}
                  placeholder={tushareStatus}
                  autoComplete="current-password"
                  disabled={clearTushareToken}
                />
              </div>
              <div className="flex items-center justify-between gap-3">
                <span className={hintClass}>
                  {
                    "Used for China A-share, futures, fund, and macro data. If unset, the project falls back to AKShare where available."
                  }
                </span>
                <label className="flex shrink-0 items-center gap-2 text-xs text-muted-foreground">
                  <input
                    type="checkbox"
                    checked={clearTushareToken}
                    onChange={(event) => {
                      setClearTushareToken(event.target.checked);
                      if (event.target.checked) setTushareToken("");
                    }}
                    className="h-3.5 w-3.5 accent-primary"
                  />
                  {"Clear saved Tushare token"}
                </label>
              </div>
            </label>

            <div className="rounded-md border bg-muted/30 px-3 py-2 text-xs text-muted-foreground">
              <span className="font-medium text-foreground">
                {i18n.t("settings.saved")}:{" "}
              </span>
              <span className="break-all font-mono">
                {dataSettings.env_path}
              </span>
            </div>

            <button
              type="submit"
              disabled={dataSaving}
              className="inline-flex items-center justify-center gap-2 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-70"
            >
              {dataSaving ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <Save className="h-4 w-4" />
              )}
              {dataSaving
                ? i18n.t("settings.saving")
                : "Save data source settings"}
            </button>
          </div>

          <div className="rounded-md border bg-muted/20 p-4">
            <div className="mb-3 flex items-center justify-between gap-3">
              <span className="text-sm font-medium">{"BaoStock"}</span>
              <span
                className={`rounded-full px-2 py-0.5 text-xs ${dataSettings.baostock_supported ? "bg-success/10 text-success" : "bg-warning/10 text-warning"}`}
              >
                {dataSettings.baostock_supported
                  ? "Loader available"
                  : "No project loader"}
              </span>
            </div>
            <div className="space-y-2 text-sm text-muted-foreground">
              <p>{dataSettings.baostock_message}</p>
              <p>
                {dataSettings.baostock_installed
                  ? "Python package installed"
                  : "Python package not installed"}
              </p>
            </div>
          </div>
        </div>
      </form>

      {/* Desktop: optional broker SDK management */}
      <section className="mt-6 rounded-xl border bg-card p-4 space-y-3">
        <div className="flex items-center gap-2">
          <Package className="h-5 w-5" />
          <h2 className="text-lg font-semibold">券商支持 (可选依赖)</h2>
        </div>
        <p className="text-sm text-muted-foreground">
          按需安装券商 SDK。安装到本地可写目录，不影响核心依赖。
        </p>
        <OptionalDepsManager />
      </section>

      {/* WeChat QR login modal */}
      {weixinQr && (
        <div
          className="fixed inset-0 bg-black/60 flex items-center justify-center z-50"
          onClick={() => setWeixinQr(null)}
        >
          <div
            className="bg-white dark:bg-gray-800 rounded-lg p-6 max-w-sm"
            onClick={(e) => e.stopPropagation()}
          >
            <h3 className="text-lg font-semibold mb-4">微信扫码登录</h3>
            <div className="flex flex-col items-center gap-3 py-4">
              <Loader2 className="h-10 w-10 animate-spin text-blue-500" />
              <p className="text-sm text-gray-500 text-center">
                请在已打开的页面完成微信扫码登录
                <br />
                本窗口会自动检测登录状态…
              </p>
              {weixinQr.image?.startsWith("http") && (
                <button
                  onClick={() =>
                    window.open(weixinQr.image, "_blank", "noopener,noreferrer")
                  }
                  className="text-xs text-blue-600 hover:underline"
                >
                  未弹出页面?点此重新打开登录链接
                </button>
              )}
            </div>
            <button
              onClick={() => setWeixinQr(null)}
              className="mt-4 w-full rounded-md border px-3 py-2 text-sm"
            >
              取消
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
