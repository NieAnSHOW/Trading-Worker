import { invoke } from "@tauri-apps/api/core";
import type {
  AuthStatusView,
  Captcha,
  CommandMessage,
  EnvironmentReport,
  LoginResultView,
  MemberUsageView,
  MemberBenefitsView,
  StatusReport,
  AdItem,
  UpdateInfo,
  Settings,
  PublicConfig,
  LLMSettings,
  DataSourceSettings,
  CustomLLMReadiness,
} from "./types";

// 与 src-tauri/src/console.rs 的 #[tauri::command] 一一对应。
// 所有命令透传 invoke;Rust 侧 camelCase ↔ snake_case 由 Tauri 自动转换。

export const consoleStatus = (): Promise<StatusReport> =>
  invoke<StatusReport>("console_status");

export const consoleBootstrap = (): Promise<void> =>
  invoke<void>("console_bootstrap");

export const consoleStartService = (): Promise<number> =>
  invoke<number>("console_start_service");

export const consoleStopService = (): Promise<void> =>
  invoke<void>("console_stop_service");

export const consoleOpenWebui = (port: number): Promise<boolean> =>
  invoke<boolean>("console_open_webui", { port });

export const consoleTakePendingWebui = (): Promise<string | null> =>
  invoke<string | null>("console_take_pending_webui");

export const consoleCloseWebui = (): Promise<void> =>
  invoke<void>("console_close_webui");

export const consoleOpenLogs = (): Promise<void> =>
  invoke<void>("console_open_logs");

export const consoleGetMirror = (): Promise<string> =>
  invoke<string>("console_get_mirror");

export const consoleToggleMirror = (): Promise<string> =>
  invoke<string>("console_toggle_mirror");

export const consoleClearLogs = (): Promise<number> =>
  invoke<number>("console_clear_logs");

export const consoleClearVenv = (): Promise<void> =>
  invoke<void>("console_clear_venv");

export const consoleUninstallLegacyApp = (): Promise<void> =>
  invoke<void>("console_uninstall_legacy_app");

export const consoleCheckEnvironment = (): Promise<EnvironmentReport> =>
  invoke<EnvironmentReport>("console_check_environment");

export const consoleRepairEnvironment = (): Promise<void> =>
  invoke<void>("console_repair_environment");

export const consoleGetSettings = (): Promise<Settings> =>
  invoke<Settings>("console_get_settings");

export const consoleSetThemeMode = (mode: Settings["theme_mode"]): Promise<void> =>
  invoke<void>("console_set_theme_mode", { mode });

export const consoleSetThemeColor = (color: Settings["theme_color"]): Promise<void> =>
  invoke<void>("console_set_theme_color", { color });

export const consoleSetApiAuthKey = (key: string): Promise<void> =>
  invoke<void>("console_set_api_auth_key", { key });

export const consoleStartChannels = (port: number): Promise<string> =>
  invoke<string>("console_start_channels", { port });

export const consoleChannelsStatus = (port: number): Promise<string> =>
  invoke<string>("console_channels_status", { port });

// LLM / 数据源设置:Rust 代理转发本地 backend,返回原始 JSON 文本,这里解析为类型。
export const consoleGetLlmSettings = (port: number): Promise<LLMSettings> =>
  invoke<string>("console_get_llm_settings", { port }).then((raw) => JSON.parse(raw));

export const consoleSetLlmSettings = (
  port: number,
  settings: Record<string, unknown>,
): Promise<LLMSettings> =>
  invoke<string>("console_set_llm_settings", {
    port,
    body: JSON.stringify(settings),
  }).then((raw) => JSON.parse(raw));

export const consoleGetDataSourceSettings = (port: number): Promise<DataSourceSettings> =>
  invoke<string>("console_get_data_source_settings", { port }).then((raw) => JSON.parse(raw));

export const consoleSetDataSourceSettings = (
  port: number,
  settings: Record<string, unknown>,
): Promise<DataSourceSettings> =>
  invoke<string>("console_set_data_source_settings", {
    port,
    body: JSON.stringify(settings),
  }).then((raw) => JSON.parse(raw));

export const consoleInstallChannelDep = (channel: string): Promise<void> =>
  invoke<void>("console_install_channel_dep", { channel });

// ── 消息渠道(自 WebUI 设置页迁移;Rust 代理转发本地 backend,返回原始 JSON 文本) ──
export const consoleStopChannels = (port: number): Promise<string> =>
  invoke<string>("console_stop_channels", { port });

export const consoleRunPairingCommand = (
  port: number,
  body: Record<string, unknown>,
): Promise<string> =>
  invoke<string>("console_run_pairing_command", {
    port,
    body: JSON.stringify(body),
  });

export const consoleWeixinLoginStart = (port: number): Promise<string> =>
  invoke<string>("console_weixin_login_start", { port });

export const consoleWeixinLoginStatus = (port: number, loginId: string): Promise<string> =>
  invoke<string>("console_weixin_login_status", { port, loginId });

export const consoleQuit = (): Promise<void> =>
  invoke<void>("console_quit");

// 与 src-tauri/src/console.rs 的 #[tauri::command] 一一对应。
export const consoleLoginCaptcha = (): Promise<Captcha> =>
  invoke<Captcha>("console_login_captcha");

export const consoleLoginSendSms = (
  phone: string,
  captchaId: string,
  code: string,
): Promise<CommandMessage> =>
  invoke<CommandMessage>("console_login_send_sms", { phone, captchaId, code });

export const consoleLoginByPhone = (
  phone: string,
  smsCode: string,
  remember: boolean,
): Promise<LoginResultView> =>
  invoke<LoginResultView>("console_login_by_phone", { phone, smsCode, remember });

export const consoleLoginByPassword = (
  phone: string,
  password: string,
  remember: boolean,
): Promise<LoginResultView> =>
  invoke<LoginResultView>("console_login_by_password", { phone, password, remember });

export const consoleLoginRegister = (
  phone: string,
  smsCode: string,
  password: string,
): Promise<LoginResultView> =>
  invoke<LoginResultView>("console_login_register", { phone, smsCode, password });

export const consoleLoginSetPassword = (password: string): Promise<void> =>
  invoke<void>("console_login_set_password", { password });

export const consoleAuthStatus = (): Promise<AuthStatusView> =>
  invoke<AuthStatusView>("console_auth_status");

export const consoleMemberUsage = (): Promise<MemberUsageView> =>
  invoke<MemberUsageView>("console_member_usage");

export const consoleMemberBenefits = (): Promise<MemberBenefitsView> =>
  invoke<MemberBenefitsView>("console_member_benefits");

export const consoleLogout = (): Promise<void> =>
  invoke<void>("console_logout");

export const consoleCustomLlmReadiness = (): Promise<CustomLLMReadiness> =>
  invoke<CustomLLMReadiness>("console_custom_llm_readiness");

export const consoleLogoutToCustom = (): Promise<CustomLLMReadiness> =>
  invoke<CustomLLMReadiness>("console_logout_to_custom");

export const consoleLoginActivateVip = (): Promise<number | null> =>
  invoke<number | null>("console_login_activate_vip");

export const consoleFetchAds = (position: string): Promise<AdItem[]> =>
  invoke<AdItem[]>("console_fetch_ads", { position });

export const consoleGetPublicConfig = (): Promise<PublicConfig> =>
  invoke<PublicConfig>("console_get_public_config");

export const consoleOpenExternalUrl = (url: string): Promise<void> =>
  invoke<void>("open_external_url", { url });

// ── 版本检查与更新 ──────────────────────────────────────────────────
export const consoleCheckUpdate = (): Promise<UpdateInfo> =>
  invoke<UpdateInfo>("console_check_update");

export const consoleDownloadUpdate = (info: UpdateInfo): Promise<string> =>
  invoke<string>("console_download_update", { info });

export const consoleInstallUpdate = (path: string): Promise<void> =>
  invoke<void>("console_install_update", { path });
