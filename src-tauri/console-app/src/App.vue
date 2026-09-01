<script setup lang="ts">
import { computed, ref, onMounted, onUnmounted, onErrorCaptured } from "vue";
import type { UnlistenFn } from "@tauri-apps/api/event";
import { useRoute } from "vue-router";
import { loadPublicConfig } from "./config/prod";
import Rail, { THEME_COLOR_EVENT, THEME_MODE_EVENT } from "./components/Rail.vue";
import ToastHost from "./components/ToastHost.vue";
import ConfirmDialog from "./components/ConfirmDialog.vue";
import { useToast } from "./composables/useToast";
import {
  consoleCloseWebui,
  consoleFetchAds,
  consoleOpenExternalUrl,
  consoleQuit,
  consoleTakePendingWebui,
} from "./ipc/commands";
import type { AdItem } from "./ipc/types";
import { onQuitRequested, onWebuiClose, onWebuiOpen } from "./ipc/events";
import { WEBUI_AUTH_EVENT, type WebuiAuthMessage } from "./webuiAuth";
import { formatError } from "./lib/formatError";

const errMsg = ref("");
const route = useRoute();
const isOnboarding = computed(() => route.path === "/");
const isStandaloneSurface = computed(() => isOnboarding.value || route.path === "/login");
const hasRail = computed(() => !isStandaloneSurface.value || webuiVisible.value);
const webuiFrameUrl = ref<string | null>(null);
const webuiVisible = ref(false);
const webuiFrameActive = ref(false);
const webuiFrame = ref<HTMLIFrameElement | null>(null);
const SHELL_TRANSITION_MS = 220;
let unlistens: UnlistenFn[] = [];
let closeTransition: Promise<void> | null = null;
let transitionGeneration = 0;
// 托盘「退出」的二次确认(服务运行中/依赖安装中)。必须挂在常驻壳层:
// 控制台多页路由化后,页面级监听在其他路由/内嵌 WebUI 态收不到
// app://quit-requested,曾导致托盘「退出」无响应。
const quitDialogOpen = ref(false);
const quitInstalling = ref(false);
const quitText = computed(() =>
  quitInstalling.value
    ? "依赖仍在安装中,<b>退出将中断安装</b>,下次需要重新安装。确认要退出吗?"
    : "后端服务仍在运行,<b>退出将终止服务并中断正在执行的任务</b>(回测、研究、实盘等)。确认要退出吗?",
);

async function onQuitDialogClose(value: "ok" | "cancel") {
  quitDialogOpen.value = false;
  if (value !== "ok") return;
  try {
    await consoleQuit();
  } catch (error) {
    useToast().error(`退出失败：${error}`);
  }
}

function prefersReducedMotion(): boolean {
  return typeof window.matchMedia === "function"
    ? window.matchMedia("(prefers-reduced-motion: reduce)").matches
    : false;
}

function syncWebuiTheme() {
  const frame = webuiFrame.value;
  const frameUrl = webuiFrameUrl.value;
  if (!frame?.contentWindow || !frameUrl) return;
  try {
    frame.contentWindow.postMessage(
      {
        type: "vibe-shell:theme",
        dark: document.documentElement.dataset.theme === "dark",
        color: document.documentElement.dataset.brand ?? null,
      },
      new URL(frameUrl, window.location.href).origin,
    );
  } catch {
    // The frame may be navigating; its load handler will retry the sync.
  }
}

function notifyWebuiAuth(event: Event) {
  const message = (event as CustomEvent<WebuiAuthMessage>).detail;
  const frame = webuiFrame.value;
  const frameUrl = webuiFrameUrl.value;
  if (!frame?.contentWindow || !frameUrl) return;
  try {
    frame.contentWindow.postMessage({ type: message }, new URL(frameUrl, window.location.href).origin);
  } catch {
    // The frame may be navigating; a later login/open will refresh its settings.
  }
}

// iframe 内的 WebUI 拿不到 Tauri IPC(仅注入主框架),外链经消息桥委托此处
// 调 open_external_url 拉起系统浏览器。只信任自家 frame 的消息,且仅放行
// http/https(Rust 侧 open_external_url 会再校验一次)。
function isWebUrl(value: string): boolean {
  try {
    const protocol = new URL(value).protocol;
    return protocol === "http:" || protocol === "https:";
  } catch {
    return false;
  }
}

async function onWebuiMessage(event: MessageEvent) {
  if (event.source !== webuiFrame.value?.contentWindow) return;
  const data = event.data as { type?: string; url?: string; position?: string } | null;
  if (data?.type === "vibe-shell:ads-request" && typeof data.position === "string") {
    // 公告/广告数据在会员服务器(cool-admin),iframe 直连会被 CORS 拦下,
    // 由控制台代取后回推;拉取失败回空列表,WebUI 侧静默隐藏公告栏。
    let ads: AdItem[] = [];
    try {
      ads = await consoleFetchAds(data.position);
    } catch {
      // 广告接口失败不阻断消息桥,回空列表即可
    }
    (event.source as Window).postMessage(
      { type: "vibe-shell:ads", position: data.position, ads },
      event.origin,
    );
    return;
  }
  if (data?.type !== "vibe-shell:open-external" || typeof data.url !== "string") return;
  if (!isWebUrl(data.url)) return;
  void consoleOpenExternalUrl(data.url);
}

function openWebui(url: string) {  if (!url) return;
  const generation = ++transitionGeneration;
  closeTransition = null;
  if (webuiFrameUrl.value !== url) webuiFrameUrl.value = url;
  webuiVisible.value = true;
  webuiFrameActive.value = false;
  requestAnimationFrame(() => {
    if (generation !== transitionGeneration) return;
    webuiFrameActive.value = true;
    document.documentElement.classList.remove("desktop-shell-leaving");
    syncWebuiTheme();
  });
}

function finishCloseWebui() {
  webuiVisible.value = false;
  webuiFrameActive.value = false;
  document.documentElement.classList.remove("desktop-shell-leaving");
  document.documentElement.classList.add("desktop-shell-entering");
  requestAnimationFrame(() => document.documentElement.classList.remove("desktop-shell-entering"));
}

function animateCloseWebui(): Promise<void> {
  if (!webuiVisible.value) return Promise.resolve();
  if (closeTransition) return closeTransition;

  const generation = ++transitionGeneration;
  webuiFrameActive.value = false;
  closeTransition = new Promise<void>((resolve) => {
    if (prefersReducedMotion()) {
      finishCloseWebui();
      resolve();
      return;
    }
    window.setTimeout(() => {
      if (generation === transitionGeneration) finishCloseWebui();
      resolve();
    }, SHELL_TRANSITION_MS);
  });
  const current = closeTransition;
  void current.then(() => {
    if (closeTransition === current) closeTransition = null;
  });
  return current;
}

async function returnToConsole() {
  const transition = animateCloseWebui();
  try {
    await consoleCloseWebui();
  } finally {
    await transition;
  }
}
onErrorCaptured((e) => {
  errMsg.value = formatError(e);
  return false; // 阻止向上抛
});

// 面板滚动条平时隐藏、仅滚动期间浮现(见 console.css 的 .is-scrolling 规则)
let surfaceScrollHideTimer: number | undefined;
function onSurfaceScroll(event: Event) {
  const surface = event.currentTarget as HTMLElement;
  surface.classList.add("is-scrolling");
  window.clearTimeout(surfaceScrollHideTimer);
  surfaceScrollHideTimer = window.setTimeout(
    () => surface.classList.remove("is-scrolling"),
    600,
  );
}

// 启动即拉取服务端公共配置（enableLogin/enableAd/checkUpdate 等），失败静默降级默认值
onMounted(async () => {
  document.getElementById("console-rail-bootstrap")?.remove();
  if (document.documentElement.classList.contains("desktop-shell-entering")) {
    requestAnimationFrame(() => document.documentElement.classList.remove("desktop-shell-entering"));
  }
  unlistens = await Promise.all([
    onWebuiOpen(openWebui),
    onWebuiClose(() => void animateCloseWebui()),
    onQuitRequested((payload: any) => {
      quitInstalling.value = !!payload?.installing;
      quitDialogOpen.value = true;
    }),
  ]);
  window.addEventListener(THEME_MODE_EVENT, syncWebuiTheme);
  window.addEventListener(THEME_COLOR_EVENT, syncWebuiTheme);
  window.addEventListener(WEBUI_AUTH_EVENT, notifyWebuiAuth);
  window.addEventListener("message", onWebuiMessage);
  const pendingUrl = await consoleTakePendingWebui();
  if (pendingUrl) openWebui(pendingUrl);
  void loadPublicConfig();
});

onUnmounted(() => {
  unlistens.forEach((unlisten) => unlisten());
  window.clearTimeout(surfaceScrollHideTimer);
  window.removeEventListener(THEME_MODE_EVENT, syncWebuiTheme);
  window.removeEventListener(THEME_COLOR_EVENT, syncWebuiTheme);
  window.removeEventListener(WEBUI_AUTH_EVENT, notifyWebuiAuth);
  window.removeEventListener("message", onWebuiMessage);
});
</script>

<template>
  <div v-if="errMsg" class="fatal">
    控制台发生错误：{{ errMsg }}
  </div>
  <template v-else>
    <!-- The rail belongs to the retained shell, never to the WebUI document. -->
    <Rail
      v-if="!isStandaloneSurface || webuiVisible"
      :webui-active="webuiVisible"
      @navigate-console="returnToConsole"
    />
    <div
      class="shell-content"
      :class="{
        'shell-content--onboarding': isOnboarding,
        'shell-content--standalone': isStandaloneSurface,
        'shell-content--rail': hasRail,
      }"
      data-test="shell-content"
    >
      <div v-show="!webuiVisible" data-test="console-surface" @scroll.passive="onSurfaceScroll">
        <router-view v-slot="{ Component }">
          <Transition name="page" mode="out-in">
            <component :is="Component" />
          </Transition>
        </router-view>
      </div>
    </div>
    <iframe
      v-if="webuiFrameUrl"
      v-show="webuiVisible"
      ref="webuiFrame"
      :src="webuiFrameUrl"
      class="desktop-webui-frame"
      :class="{ 'desktop-webui-frame--active': webuiFrameActive }"
      data-test="desktop-webui-frame"
      title="研究"
      @load="syncWebuiTheme"
    />
    <ConfirmDialog
      data-test="quit-dialog"
      :open="quitDialogOpen"
      title="确认退出客户端？"
      @close="onQuitDialogClose"
    >
      <span v-html="quitText"></span>
      <template #confirm-text>确认退出</template>
    </ConfirmDialog>
    <ToastHost />
  </template>
</template>

<style>
.fatal {
  padding: 24px; color: #ff8080; font-family: ui-monospace, Menlo, monospace;
  background: #0e0f13; min-height: 100vh;
}

.page-enter-active,
.page-leave-active {
  transition: opacity 220ms ease, transform 220ms ease;
}

.page-enter-from,
.page-leave-to {
  opacity: 0;
  transform: translateY(8px);
}

.shell-content:has(> [data-test="console-surface"] > .page-enter-active),
.shell-content:has(> [data-test="console-surface"] > .page-leave-active),
.shell-content:has(> [data-test="console-surface"] > .onboarding-page--entering) {
  overflow: clip;
}

.desktop-webui-frame {
  position: fixed;
  inset: 0 0 0 var(--rail-width);
  z-index: 5;
  width: calc(100vw - var(--rail-width));
  height: 100dvh;
  border: 0;
  background: hsl(var(--bg));
  opacity: 0;
  pointer-events: none;
  transform: translateY(8px);
  transition: opacity 220ms ease, transform 220ms ease;
}

.desktop-webui-frame--active {
  opacity: 1;
  pointer-events: auto;
  transform: translateY(0);
}

@media (prefers-reduced-motion: reduce) {
  .page-enter-active,
  .page-leave-active,
  .desktop-webui-frame {
    transition-duration: 0.01ms;
  }
}
</style>
