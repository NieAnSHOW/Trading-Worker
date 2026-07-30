// 镜像 src-tauri/src/console.rs 的 StatusReport 与事件 payload 结构。

export type EnvState = "ready" | "incomplete" | "not_installed";

export interface StatusReport {
  env: EnvState;
  service_running: boolean;
  port: number | null;
}

export type BootstrapStage =
  | "venv"
  | "installing"
  | "smoke"
  | "done"
  | "failed";

export interface BootstrapEvent {
  stage: BootstrapStage;
  message?: string;
  ok?: boolean;
}

export interface ChannelInfo {
  enabled?: boolean;
  loaded?: boolean;
  running?: boolean;
  health?: "ok" | "expired" | string;
}

export interface ChannelStatus {
  channels?: Record<string, ChannelInfo>;
}

// === Auth / Login（镜像 src-tauri/src/console.rs 与 auth.rs）===

export interface UserInfo {
  id: number;
  unionid?: string | null;
  avatarUrl?: string | null;
  nickName?: string | null;
  phone?: string | null;
  gender: number;
  status: number;
  loginType: number;
  description?: string | null;
  memberLevel?: MemberLevel | null;
}

export interface MemberLevel {
  id: number;
  name: string;
  code?: string | null;
  levelValue: number;
  expireTime?: string | null;
}

export interface LoginResultView {
  userInfo: UserInfo;
  hasPassword: boolean;
  expireAt: number; // epoch 秒
  message: string;
}

export interface CommandMessage {
  message: string;
}

export interface DesktopConfigImportView {
  userApiUrl: string;
}

export interface AuthStatusView {
  authenticated: boolean;
  userInfo?: UserInfo | null;
  expireAt?: number | null;
}

export interface MemberUsageView {
  total_available: number;
  total_granted: number;
  total_used: number;
  unlimited_quota: boolean;
}

export interface MemberBenefit {
  id: string;
  title: string;
  description?: string | null;
}

export interface MemberBenefitsView {
  benefits: MemberBenefit[];
}

export interface Captcha {
  captchaId: string;
  data: string; // base64 svg（可能含 data: 前缀）
}

// Rust 端 #[serde(tag="variant")] 错误，前端按 e.variant 分流
export interface AuthError {
  variant:
    | "Network"
    | "Api"
    | "LoginExpired"
    | "EnvWrite"
    | "NotAuthenticated";
  message?: string;
  code?: number;
}

export interface ServiceStartError {
  variant:
    | "EnvNotReady"
    | "AlreadyRunning"
    | "LoginExpired"
    | "SpawnFailed"
    | "HealthTimeout"
    | "ProcessExited"
    | "Other";
  message?: string;
  code?: number | null;
}

// === 版本更新 ===

export interface UpdateInfo {
  current: string;
  latest: string;
  hasUpdate: boolean;
  downloadUrl: string;
  assetName: string;
  releaseNotes: string;
}

export interface DownloadProgress {
  downloaded: number;
  total: number;
  done: boolean;
  path?: string | null;
}

// === 广告 ===

export interface AdImage {
  url: string;
  link?: string | null;
}

export interface AdItem {
  id: number;
  title: string;
  type: number; // 1=纯图片 2=纯文本
  position: string;
  images?: AdImage[] | null;
  content?: string | null;
  link?: string | null;
  sort: number;
}
