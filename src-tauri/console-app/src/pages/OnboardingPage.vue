<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue";
import { storeToRefs } from "pinia";
import { useRouter } from "vue-router";
import type { UnlistenFn } from "@tauri-apps/api/event";
import { useEnvStore } from "../stores/env";
import { useServiceStore } from "../stores/service";
import { useBootstrapStore } from "../stores/bootstrap";
import { useAuthStore } from "../stores/auth";
import { config, loadPublicConfig } from "../config/prod";
import {
  consoleBootstrap,
  consoleGetMirror,
  consoleOpenLogs,
  consoleOpenWebui,
  consoleToggleMirror,
} from "../ipc/commands";
import { onBootstrapEvent, onBootstrapExit, onServiceStarted } from "../ipc/events";
import type { BootstrapEvent } from "../ipc/types";
import { formatError } from "../lib/formatError";
import AppButton from "../components/AppButton.vue";
import ConfirmDialog from "../components/ConfirmDialog.vue";
import logoPng from "../assets/128x128@2x.png";
import { ArrowLeftRight, FolderOpen, Wrench } from "@lucide/vue";

const env = useEnvStore();
const service = useServiceStore();
const bootstrap = useBootstrapStore();
const auth = useAuthStore();
const router = useRouter();
const { env: envState, serviceRunning, error: envError } = storeToRefs(env);

const pageReady = ref(false);
const pageEntering = ref(false);
const installing = ref(false);
const bootstrapFailed = ref(false);
const errorMsg = ref("");
const displayError = computed(() => errorMsg.value || envError.value);

const needsRepair = computed(() =>
  envState.value === "not_installed" ||
  envState.value === "incomplete" ||
  bootstrapFailed.value ||
  Boolean(displayError.value),
);


const mirrorName = ref("tsinghua");
const mirrorLabel = computed(() => (mirrorName.value === "aliyun" ? "阿里云" : "清华"));

async function onOpenLogs() {
  try {
    await consoleOpenLogs();
  } catch (error) {
    setErr(error);
  }
}

async function onToggleMirror() {
  try {
    mirrorName.value = await consoleToggleMirror();
  } catch (error) {
    setErr(error);
  }
}
function setErr(error: unknown) {
  errorMsg.value = formatError(error);
}

const installStopDialogOpen = ref(false);

async function onRepair() {
  if (installing.value) return;
  if (serviceRunning.value) {
    installStopDialogOpen.value = true;
    return;
  }
  await doInstall();
}

async function onInstallStopDialogClose(value: "ok" | "cancel") {
  installStopDialogOpen.value = false;
  if (value !== "ok") return;
  try {
    await service.stop();
    env.setPort(null);
  } catch (error) {
    setErr(error);
    return;
  }
  await doInstall();
}

async function doInstall() {
  setErr("");
  bootstrapFailed.value = false;
  bootstrap.start();
  installing.value = true;
  try {
    await consoleBootstrap();
  } catch (error) {
    bootstrapFailed.value = true;
    bootstrap.advance("failed", "");
    setErr(error);
    installing.value = false;
  }
}

async function startResearchService() {
  try {
    const port = await service.start();
    env.setPort(port);
    env.serviceRunning = true;
  } catch (error) {
    setErr(error);
  }
}

async function continueStartup() {
  if (envState.value !== "ready") return;

  await loadPublicConfig();

  if (config.enableLogin) {
    await auth.refresh();
    if (!auth.authenticated) {
      await router.replace("/login");
      return;
    }
  }

  // A return from the login surface can leave the sidecar running. Reopen its
  // retained WebUI frame instead of waiting for a new service-start event.
  if (serviceRunning.value) {
    if (env.port != null) {
      try {
        await consoleOpenWebui(env.port);
      } catch (error) {
        setErr(error);
      }
    } else {
      setErr("本地服务端口不可用，请重启服务");
    }
    return;
  }

  await startResearchService();
}

// 托盘「退出」的二次确认已上移到常驻的 App.vue(多页路由下页面级监听会丢事件)。


let unlistens: UnlistenFn[] = [];

onMounted(async () => {
  pageReady.value = true;
  pageEntering.value = true;


  consoleGetMirror()
    .then((name) => {
      mirrorName.value = name;
    })
    .catch(() => {});
  unlistens = await Promise.all([
    onBootstrapEvent((event: BootstrapEvent) => {
      bootstrap.advance(event.stage, event.message ?? "");
      if (event.ok === false) {
        bootstrapFailed.value = true;
        setErr(event.message || "依赖安装失败");
      }
    }),
    onBootstrapExit(async (code: number) => {
      if (code !== 0 && bootstrap.state !== "done") {
        bootstrapFailed.value = true;
        bootstrap.advance("failed", "");
        if (!errorMsg.value) setErr("依赖安装失败");
      }
      if (code === 0) bootstrapFailed.value = false;
      installing.value = false;
      await env.refresh();
      if (code === 0 && envState.value === "ready" && !serviceRunning.value) {
        await continueStartup();
      } else if (code === 0 && envState.value !== "ready") {
        setErr("安装完成，但运行环境尚未就绪，请重试安装");
      }
    }),
    onServiceStarted((port: number) => {
      env.setPort(port);
      env.serviceRunning = true;
    }),
  ]);

  await env.refresh();
  await continueStartup();
});

onUnmounted(() => {
  unlistens.forEach((unlisten) => unlisten());
});

function onStartupAnimationEnd() {
  pageEntering.value = false;
}
</script>

<template>
  <main
    class="onboarding-page"
    :class="{ 'onboarding-page--ready': pageReady, 'onboarding-page--entering': pageEntering }"
    aria-label="应用启动"
    @animationend.self="onStartupAnimationEnd"
  >
    <section class="onboarding-stage">
      <img class="onboarding-logo" alt="Trading Worker" :src="logoPng" />
      <span class="onboarding-name">Trading Worker</span>
      <p class="onboarding-status">
        <i v-if="!needsRepair" class="onboarding-dot" aria-hidden="true"></i>{{ needsRepair ? '请先点击安装环境' : '应用启动中...' }}
      </p>
      <p v-show="displayError" id="err" class="onboarding-error" role="alert">{{ displayError }}</p>
      <AppButton
        v-if="needsRepair"
        variant="primary"
        :busy="installing"
        busy-label="修复中"
        data-test="repair-environment"
        @click="onRepair"
      >
        <Wrench :size="16" aria-hidden="true" />
        安装/修复环境
      </AppButton>
      <div v-if="needsRepair" class="onboarding-quick-actions">
        <AppButton variant="ghost" data-test="open-logs-action" @click="onOpenLogs">
          <FolderOpen :size="16" aria-hidden="true" />
          打开日志
        </AppButton>
        <AppButton variant="ghost" data-test="toggle-mirror-action" @click="onToggleMirror">
          <ArrowLeftRight :size="16" aria-hidden="true" />
          安装源：{{ mirrorLabel }}
        </AppButton>
      </div>
    </section>

    <ConfirmDialog :open="installStopDialogOpen" title="服务运行中，确认停止并修复？" @close="onInstallStopDialogClose">
      检测到后端服务正在运行，修复环境需要先停止服务。<b>停止将中断正在执行的任务</b>，确认停止并继续修复吗？
      <template #confirm-text>停止并修复</template>
    </ConfirmDialog>


  </main>
</template>

<style scoped>
/* 启动门面:近黑蓝画布 + 品牌辉光,等宽品牌名与状态行(设计系统"状态用
   等宽 + 青绿点位"表达),与登录页品牌面板同构 */
.onboarding-page {
  display: grid;
  min-height: 100dvh;
  place-items: center;
  background:
    radial-gradient(
      46% 32% at 50% 36%,
      hsl(var(--brand) / 0.1),
      transparent 70%
    ),
    hsl(var(--bg));
  color: hsl(var(--ink));
}

.onboarding-stage {
  display: flex;
  min-width: 240px;
  flex-direction: column;
  align-items: center;
  gap: 0;
  padding: 32px;
  text-align: center;
}

.onboarding-logo {
  width: 96px;
  height: 96px;
  border-radius: 22px;
  object-fit: contain;
  filter: drop-shadow(0 14px 34px hsl(var(--brand) / 0.22));
}

/* 等宽大写品牌名,与登录页 brand-name 同构 */
.onboarding-name {
  margin-top: 18px;
  font-family: var(--tw-mono);
  font-size: 14px;
  font-weight: 600;
  letter-spacing: 0.12em;
  text-transform: uppercase;
}

.onboarding-status {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  margin-top: 12px;
  color: hsl(var(--ink-dim));
  font-family: var(--tw-mono);
  font-size: 12.5px;
  letter-spacing: 0.06em;
}

/* 启动进行中:青绿点位脉冲(设计系统"进行中使用青绿点位") */
.onboarding-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: hsl(var(--brand));
  animation: pulse 1.4s var(--ease) infinite;
}

/* 视觉主体由全局 #err:not(:empty) 提供,这里只约束宽度 */
.onboarding-error {
  max-width: 340px;
}

/* 主操作:设计系统按钮规范(44px 高 / 8px 圆角 / 等宽标签 + 品牌辉光) */
.onboarding-stage :deep(.btn-primary) {
  margin-top: 22px;
  min-height: 44px;
  padding-inline: 22px;
  border-radius: 8px;
  font-family: var(--tw-mono);
  font-size: 12.5px;
  font-weight: 600;
  letter-spacing: 0.06em;
  box-shadow: 0 6px 20px hsl(var(--brand) / 0.28);
}

/* 快捷操作行(打开日志/切换安装源):ghost 次要按钮 */
.onboarding-quick-actions {
  display: flex;
  gap: 10px;
  margin-top: 14px;
}

.onboarding-quick-actions :deep(.btn-ghost) {
  font-family: var(--tw-mono);
  font-size: 12px;
  letter-spacing: 0.04em;
}

.onboarding-page--ready .onboarding-stage {
  animation: onboarding-enter 220ms ease-out both;
}

@keyframes onboarding-enter {
  from {
    opacity: 0;
    transform: translateY(6px);
  }
}

@media (prefers-reduced-motion: reduce) {
  .onboarding-page--ready .onboarding-stage {
    animation-duration: 0.01ms;
  }

  .onboarding-dot {
    animation-duration: 0.01ms;
  }
}
</style>
