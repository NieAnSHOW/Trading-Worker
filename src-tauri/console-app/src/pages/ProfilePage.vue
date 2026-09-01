<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue";
import { useRouter } from "vue-router";
import { storeToRefs } from "pinia";
import { useAuthStore } from "../stores/auth";
import { useEnvStore } from "../stores/env";
import { useServiceStore } from "../stores/service";
import {
  consoleCustomLlmReadiness,
  consoleLogoutToCustom,
  consoleMemberBenefits,
  consoleMemberUsage,
} from "../ipc/commands";
import type { MemberBenefit, MemberUsageView } from "../ipc/types";
import AppButton from "../components/AppButton.vue";
import ConfirmDialog from "../components/ConfirmDialog.vue";
import { useBusy } from "../composables/useBusy";
import { config as ProdConfig } from "../config/prod";
import { CircleCheck, CircleUserRound, Gift, Headset, RefreshCw, ShieldCheck } from "@lucide/vue";
import { notifyWebuiAuth } from "../webuiAuth";
import { formatError } from "../lib/formatError";

const router = useRouter();
const auth = useAuthStore();
const env = useEnvStore();
const service = useServiceStore();
const { serviceRunning } = storeToRefs(env);
const benefits = ref<MemberBenefit[]>([]);
const loading = ref(true);
const loadError = ref("");
const actionError = ref("");
const logoutOpen = ref(false);
const logoutBusy = useBusy();
const customConfigured = ref(false);
const accountName = computed(() => auth.userInfo?.nickName || auth.userInfo?.phone || "已登录");
const level = computed(() => auth.userInfo?.memberLevel);
const memberTier = computed(() => {
  const current = level.value;
  if (!current) return null;
  const name = current.name?.trim() || "会员";
  const identity = `${current.code ?? ""} ${name}`.toLowerCase();
  const tone = /vip|elite|ultimate|diamond|至尊/.test(identity) || current.levelValue >= 50
    ? "signature"
    : /pro|premium|plus|gold|高级/.test(identity) || current.levelValue >= 20
      ? "pro"
      : "member";
  return { name, tone, label: name.includes("会员") ? name : `${name} 会员` };
});
const logoutText = computed(() => customConfigured.value
  ? "退出后，正在运行的会员任务将继续完成；后续任务将使用本机自定义模型配置。"
  : "退出后，正在运行的会员任务将继续完成；后续任务需要先配置本机自定义模型，否则无法执行。");
const kefuDialogOpen = ref(false);
const rewardDialogOpen = ref(false);
const membershipUpdateNotice = ref(false);
const membershipRestartDialogOpen = ref(false);
const memberUsage = ref<MemberUsageView | null>(null);
const usageRefreshing = ref(false);
const membershipRefreshBusy = useBusy();
const usageNumberFormatter = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });
const kefuQrCode = computed(() => ProdConfig.kefuQrCode.trim());
const rewardQrCode = computed(() => ProdConfig.rewardQrCode.trim());
let usageTimer: ReturnType<typeof setInterval> | null = null;

function formatUsageAmount(value: number) {
  return usageNumberFormatter.format(value);
}

const remainingPercent = computed(() => {
  const usage = memberUsage.value;
  if (!usage || usage.total_granted <= 0) return 0;
  return Math.min(100, Math.max(0, (usage.total_available / usage.total_granted) * 100));
});

function clearMemberUsage() {
  memberUsage.value = null;
  if (usageTimer) {
    clearInterval(usageTimer);
    usageTimer = null;
  }
}

async function refreshMemberUsage() {
  if (!auth.authenticated || usageRefreshing.value) return;
  usageRefreshing.value = true;
  try {
    memberUsage.value = await consoleMemberUsage();
  } catch (error: any) {
    if (error?.variant === "LoginExpired") {
      auth.clear();
      clearMemberUsage();
    }
  } finally {
    usageRefreshing.value = false;
  }
}

async function refreshAuthForMembership() {
  await auth.refresh();
  if (!auth.authenticated) {
    clearMemberUsage();
    return false;
  }
  if (auth.membershipChanged) {
    membershipUpdateNotice.value = true;
    memberUsage.value = null;
  }
  return true;
}

async function refreshMembershipUsageManually() {
  if (!await refreshAuthForMembership()) return;
  await refreshMemberUsage();
  if (!serviceRunning.value && auth.authenticated) {
    membershipUpdateNotice.value = false;
    auth.acknowledgeMembershipChange();
  }
}

async function doRestartForMembershipUpdate() {
  await membershipRefreshBusy.run("刷新中", async () => {
    try {
      await service.stop();
      env.setPort(null);
      serviceRunning.value = false;
      const p = await service.start();
      env.setPort(p);
      serviceRunning.value = true;
      membershipUpdateNotice.value = false;
      auth.acknowledgeMembershipChange();
      await refreshMemberUsage();
    } catch (error) {
      actionError.value = formatError(error);
    }
  });
}

async function onMembershipRestartDialogClose(value: "ok" | "cancel") {
  membershipRestartDialogOpen.value = false;
  if (value === "ok") await doRestartForMembershipUpdate();
}

function onKefuDialogClose() {
  kefuDialogOpen.value = false;
}

function onRewardDialogClose() {
  rewardDialogOpen.value = false;
}

async function loadBenefits() {
  loading.value = true; loadError.value = "";
  try { benefits.value = (await consoleMemberBenefits()).benefits; }
  catch (error: any) {
    if (error?.variant === "LoginExpired" || error?.variant === "NotAuthenticated") { auth.clear(); await router.replace("/login"); return; }
    loadError.value = "权益暂时无法加载";
  } finally { loading.value = false; }
}
async function openLogout() {
  actionError.value = "";
  try {
    customConfigured.value = (await consoleCustomLlmReadiness()).customConfigured;
  } catch {
    customConfigured.value = false;
  }
  logoutOpen.value = true;
}

async function onLogout(v: "ok" | "cancel") {
  logoutOpen.value = false;
  if (v !== "ok") return;
  await logoutBusy.run("退出中", async () => {
    try {
      await consoleLogoutToCustom();
      auth.clear();
      notifyWebuiAuth("vibe-shell:auth-logout");
      await router.replace("/login");
    } catch (error) {
      actionError.value = formatError(error);
    }
  });
}
onMounted(async () => {
  await auth.refresh();
  if (!auth.authenticated) return router.replace("/login");
  if (auth.membershipChanged) {
    membershipUpdateNotice.value = true;
    memberUsage.value = null;
  }
  void refreshMemberUsage();
  usageTimer = setInterval(async () => {
    if (await refreshAuthForMembership()) void refreshMemberUsage();
  }, 300_000);
  await loadBenefits();
});

onUnmounted(() => clearMemberUsage());
</script>

<template>
  <main class="tw-page profile">
    <header>
      <p class="tw-kicker">Membership</p>
      <h1 class="tw-page-title">账户与会员</h1>
      <div class="pf-header__meta">
        <p class="tw-page-sub">确认身份、研究额度与当前可用能力。</p>
      <div class="pf-header__status" :class="{ 'pf-header__status--active': serviceRunning }">
        <span class="pf-status-dot" aria-hidden="true"></span>
        <span>{{ serviceRunning ? "本地服务运行中" : "本地服务未启动" }}</span>
      </div>
      </div>
    </header>

    <div v-if="membershipUpdateNotice" class="pf-notice" role="status">
      <span>会员权益已更新，当前服务仍在使用旧配置。</span>
      <AppButton v-if="serviceRunning" variant="primary" :busy="membershipRefreshBusy.busy.value"
        data-test="membership-refresh-service" @click="membershipRestartDialogOpen = true">
        重启服务并刷新
      </AppButton>
      <AppButton v-else variant="ghost" data-test="membership-refresh-usage" @click="refreshMembershipUsageManually">
        刷新会员用量
      </AppButton>
    </div>

    <!-- 窄屏优先级流:用量 → 身份 → 权益 → 支持 → 退出;桌面端分为独立双列 -->
    <div class="tw-grid pf-workspace" data-test="profile-workspace">
      <div class="pf-workspace__main">
      <section class="tw-panel pf-usage" data-test="member-usage-section" aria-label="研究额度">
        <header class="tw-panel__head pf-panel-head">
          <div>
            <h2 class="tw-panel__label">研究额度</h2>
            <p class="pf-panel-head__hint">用于会员支持的研究与模型能力</p>
          </div>
          <AppButton variant="ghost" :busy="usageRefreshing" busy-label="刷新中" data-test="member-usage-refresh"
            @click="refreshMembershipUsageManually">
            <RefreshCw :size="13" aria-hidden="true" />刷新
          </AppButton>
        </header>
        <div class="tw-panel__body pf-usage__body">
          <template v-if="memberUsage?.unlimited_quota">
            <div class="pf-unlimited">
              <strong data-test="member-usage-unlimited">不限量</strong>
              <span data-test="member-usage-unlimited-note">当前套餐的研究与模型调用不受积分限制</span>
            </div>
          </template>
          <template v-else-if="memberUsage">
            <div class="pf-usage-summary">
              <strong>{{ formatUsageAmount(memberUsage.total_available) }}</strong><span>积分可用</span>
              <small>{{ Math.round(remainingPercent) }}% 剩余</small>
            </div>
            <div class="pf-usage-track" role="progressbar" aria-label="剩余研究额度" :aria-valuenow="remainingPercent"
              aria-valuemin="0" aria-valuemax="100">
              <div class="pf-usage-fill" :style="{ transform: `scaleX(${remainingPercent / 100})` }"></div>
            </div>
            <div class="pf-usage-detail">
              <span>总额度 <b>{{ formatUsageAmount(memberUsage.total_granted) }}</b></span>
              <span>已使用 <b>{{ formatUsageAmount(memberUsage.total_used) }}</b></span>
            </div>
            <p class="pf-usage-note"><ShieldCheck :size="14" aria-hidden="true" />额度用于模型调用、行情研究与回测任务。</p>
          </template>
          <div v-else-if="usageRefreshing" class="pf-usage-skeleton" data-test="member-usage-skeleton"
            role="status" aria-busy="true" aria-label="正在加载研究额度">
            <span class="pf-skeleton pf-skeleton--amount"></span>
            <span class="pf-skeleton pf-skeleton--percent"></span>
            <span class="pf-skeleton pf-skeleton--track"></span>
            <div class="pf-skeleton-details" aria-hidden="true">
              <span class="pf-skeleton pf-skeleton--label"></span><span class="pf-skeleton pf-skeleton--value"></span>
              <span class="pf-skeleton pf-skeleton--label"></span><span class="pf-skeleton pf-skeleton--value"></span>
            </div>
            <span class="pf-skeleton pf-skeleton--note"></span>
          </div>
          <p v-else class="pf-state pf-usage-empty">用量暂未加载，请点击右上角刷新。</p>
        </div>
      </section>


      <section class="tw-panel pf-benefits" data-test="member-capabilities" aria-label="当前会员能力">
        <header class="tw-panel__head">
          <div>
            <h2 class="tw-panel__label">会员能力</h2>
            <p class="pf-panel-head__hint">当前套餐包含的研究工具与服务</p>
          </div>
        </header>
        <div class="tw-panel__body pf-benefits__body">
          <p v-if="loading" class="pf-state">正在加载会员权益...</p>
          <div v-else-if="loadError" class="pf-state pf-state--action">
            <p>{{ loadError }}</p>
            <AppButton variant="ghost" @click="loadBenefits">重试</AppButton>
          </div>
          <p v-else-if="benefits.length === 0" class="pf-state">当前会员暂无可展示权益</p>
            <ul v-else class="pf-benefit-list">
              <li v-for="benefit in benefits" :key="benefit.id" class="pf-benefit">
              <CircleCheck :size="16" aria-hidden="true" />
              <div><b>{{ benefit.title }}</b>
              <p v-if="benefit.description">{{ benefit.description }}</p>
              </div>
            </li>
          </ul>
        </div>
      </section>
      </div>

      <div class="pf-workspace__context">

      <section class="tw-panel pf-identity" aria-label="账户身份">
        <div class="tw-panel__body pf-identity__body">
          <span class="pf-section-overline">当前会员</span>
          <span class="pf-avatar" aria-hidden="true">
            <CircleUserRound :size="28" stroke-width="1.3" />
          </span>
          <h2 class="pf-name">{{ accountName }}</h2>
          <p v-if="auth.userInfo?.phone" class="pf-phone">{{ auth.userInfo.phone }}</p>
          <span v-if="level" class="member-tier" :class="`member-tier--${memberTier?.tone ?? 'member'}`">
            <span class="member-tier-mark">V</span><span class="member-tier-name">{{ memberTier?.label }}</span>
          </span>
          <p v-if="level?.expireTime" class="pf-expire">有效期至 {{ level.expireTime }}</p>
          <p class="pf-local-note"><ShieldCheck :size="14" aria-hidden="true" />账户与配置保存在本机</p>
        </div>
      </section>
      <section v-if="kefuQrCode || rewardQrCode" class="tw-panel pf-support" aria-label="支持与联系">
        <header class="tw-panel__head">
          <div>
            <h2 class="tw-panel__label">支持与联系</h2>
            <p class="pf-panel-head__hint">遇到配置或会员问题时联系我们</p>
          </div>
        </header>
        <div class="tw-panel__body pf-support__body">
          <AppButton v-if="kefuQrCode" variant="ghost" class="pf-contact" data-test="member-kefu-entry"
            @click="kefuDialogOpen = true">
            <Headset :size="15" aria-hidden="true" />联系客服
          </AppButton>
          <AppButton v-if="rewardQrCode" variant="ghost" class="pf-contact" data-test="member-reward-entry"
            @click="rewardDialogOpen = true">
            <Gift :size="15" aria-hidden="true" />支持作者领取会员
          </AppButton>
        </div>
      </section>

      <section class="tw-panel pf-danger" aria-label="账户操作">
        <div class="tw-panel__body pf-danger__body">
          <AppButton variant="danger" class="pf-logout" :busy="logoutBusy.busy.value" data-test="logout-action" @click="openLogout">
            退出登录
          </AppButton>
        </div>
      </section>
      </div>
    </div>

    <p v-if="actionError" class="pf-error" role="alert">{{ actionError }}</p>

    <ConfirmDialog data-test="kefu-dialog" :open="kefuDialogOpen" title="联系客服"
      :image="ProdConfig.imgBase + kefuQrCode" image-alt="客服微信二维码" hide-cancel @close="onKefuDialogClose">
      <p style="margin-top: 8px;">请使用微信扫描上方二维码添加专属客服</p>
      <template #confirm-text>我知道了</template>
    </ConfirmDialog>
    <ConfirmDialog data-test="reward-dialog" :open="rewardDialogOpen" title="支持作者领专业会员"
      :image="ProdConfig.imgBase + rewardQrCode" image-alt="支持作者二维码" hide-cancel @close="onRewardDialogClose">
      <p style="margin-top: 8px;">将打赏后的截图私发客服领取会员</p>
      <template #confirm-text>我知道了</template>
    </ConfirmDialog>
    <ConfirmDialog data-test="membership-restart-dialog" :open="membershipRestartDialogOpen" title="确认刷新会员服务？"
      @close="onMembershipRestartDialogClose">
      会员权益已更新，需要重启本地服务才能使用新的会员配置。<b>重启会中断正在执行的任务</b>，确认继续吗？
      <template #confirm-text>重启并刷新</template>
    </ConfirmDialog>
    <ConfirmDialog data-test="logout-dialog" :open="logoutOpen" title="确认退出登录？" @close="onLogout">{{ logoutText }}<template #confirm-text>确认退出</template></ConfirmDialog>
  </main>
</template>

<style>
/* 页面特有布局;共享词汇(tw-*)见 console.css。桌面端两列独立堆叠 */
@media (min-width: 900px) {
  .profile .pf-workspace {
    display: grid;
    grid-template-columns: minmax(0, 1.65fr) minmax(230px, 0.85fr);
    align-items: start;
    gap: 14px;
  }

  .profile .pf-workspace > * { min-width: 0; }

  .profile .pf-workspace__main,
  .profile .pf-workspace__context {
    display: flex;
    flex-direction: column;
    gap: 14px;
    min-width: 0;
  }
}

@media (max-width: 899px) {
  .profile .pf-workspace__main,
  .profile .pf-workspace__context {
    display: contents;
  }

  .profile .pf-usage { order: 1; }
  .profile .pf-identity { order: 2; }
  .profile .pf-benefits { order: 3; }
  .profile .pf-support { order: 4; }
  .profile .pf-danger { order: 5; }
}
.pf-header__meta {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 18px;
}

.pf-header__status {
  display: inline-flex;
  align-items: center;
  flex: none;
  gap: 7px;
  padding: 7px 10px;
  border: 1px solid hsl(var(--line));
  border-radius: 8px;
  color: hsl(var(--ink-dim));
  font-family: var(--tw-mono);
  font-size: 10px;
  letter-spacing: 0.02em;
}

.pf-header__status--active { color: hsl(var(--ok-fg)); }
.pf-status-dot { width: 6px; height: 6px; border-radius: 50%; background: hsl(var(--ink-dim)); }
.pf-header__status--active .pf-status-dot { background: hsl(var(--ok)); box-shadow: 0 0 0 3px hsl(var(--ok) / 0.14); }
.pf-panel-head { align-items: flex-start; }
.pf-panel-head__hint { margin-top: 3px; color: hsl(var(--ink-dim)); font-size: 11px; line-height: 1.35; }

/* 会员权益变更提示:全幅置顶,唯一实心主按钮 */
.pf-notice {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 8px 14px;
  margin-top: 18px;
  padding: 10px 14px;
  border: 1px solid hsl(var(--brand) / 0.35);
  border-radius: 10px;
  background: hsl(var(--brand) / 0.07);
  font-size: 12.5px;
}

/* 身份面板 */
.pf-identity__body {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 8px;
}

.pf-section-overline {
  color: hsl(var(--tw-gold));
  font-family: var(--tw-mono);
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 0.16em;
  text-transform: uppercase;
}

.pf-avatar {
  display: grid;
  width: 54px;
  height: 54px;
  margin-bottom: 4px;
  place-items: center;
  border: 1px solid hsl(var(--line));
  border-radius: 12px;
  background: hsl(var(--surface-2));
  color: hsl(var(--ink-dim));
}

.pf-name {
  font-family: var(--tw-display);
  font-size: 19px;
  font-weight: 400;
  letter-spacing: 0.01em;
  line-height: 1.3;
  text-wrap: balance;
}

.pf-phone,
.pf-expire {
  color: hsl(var(--ink-dim));
  font-family: var(--tw-mono);
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}

.pf-expire {
  font-size: 11px;
  letter-spacing: 0.02em;
}

.pf-local-note,
.pf-usage-note {
  display: flex;
  align-items: center;
  gap: 6px;
  color: hsl(var(--ink-dim));
  font-size: 11px;
  line-height: 1.4;
}

.pf-local-note { margin-top: 8px; }
.pf-usage-note { margin-top: 16px; }

/* 用量:数据焦点,单一青绿强调 */
.pf-usage-summary {
  display: grid;
  grid-template-columns: auto 1fr;
  align-items: baseline;
  column-gap: 7px;
}

.pf-usage-summary strong {
  overflow: hidden;
  color: hsl(var(--brand));
  font-family: var(--tw-mono);
  font-size: clamp(27px, 4vw, 39px);
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  letter-spacing: -0.01em;
  line-height: 1;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.pf-usage-summary span,
.pf-usage-summary small {
  color: hsl(var(--ink-dim));
  font-size: 12px;
}

.pf-usage__body { padding-top: 20px; padding-bottom: 20px; }

.pf-usage-skeleton {
  display: grid;
  grid-template-columns: max-content 1fr;
  align-items: end;
  min-height: 198px;
  column-gap: 12px;
}

.pf-skeleton {
  display: block;
  overflow: hidden;
  border-radius: 4px;
  background: hsl(var(--surface-2));
}

.pf-skeleton::after {
  content: "";
  display: block;
  width: 45%;
  height: 100%;
  background: hsl(var(--ink) / 0.06);
  animation: pf-skeleton-sweep 1.35s ease-in-out infinite;
}

.pf-skeleton--amount { width: clamp(196px, 34vw, 350px); height: 39px; }
.pf-skeleton--percent { width: 66px; height: 14px; margin-bottom: 4px; }
.pf-skeleton--track { grid-column: 1 / -1; width: 100%; height: 6px; margin-top: 26px; border-radius: 999px; }
.pf-skeleton-details { display: grid; grid-template-columns: 1fr auto; grid-column: 1 / -1; width: 100%; gap: 12px 18px; margin-top: 28px; }
.pf-skeleton--label { width: 48px; height: 13px; }
.pf-skeleton--value { width: 106px; height: 13px; justify-self: end; }
.pf-skeleton--note { grid-column: 1 / -1; width: min(292px, 72%); height: 13px; margin-top: 26px; }

@keyframes pf-skeleton-sweep {
  from { transform: translateX(-120%); }
  to { transform: translateX(320%); }
}

.pf-usage-summary small {
  grid-column: 1 / -1;
  margin-top: 9px;
}

.pf-usage-track {
  height: 6px;
  margin-top: 14px;
  overflow: hidden;
  border-radius: 999px;
  background: hsl(var(--line) / 0.8);
}

.pf-usage-fill {
  height: 100%;
  border-radius: inherit;
  background: hsl(var(--brand));
  transform-origin: left center;
  transition: transform 0.3s var(--ease);
}

.pf-usage-detail {
  display: grid;
  gap: 7px;
  margin-top: 13px;
  color: hsl(var(--ink-dim));
  font-size: 12px;
}

.pf-usage-detail span {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 10px;
}

.pf-usage-detail b {
  color: hsl(var(--ink));
  font-family: var(--tw-mono);
  font-size: 11.5px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}

.pf-unlimited {
  display: grid;
  gap: 7px;
  padding: 2px 0;
}

.pf-unlimited strong {
  color: hsl(var(--brand));
  font-family: var(--tw-display);
  font-size: 26px;
  font-weight: 400;
  line-height: 1.1;
}

.pf-unlimited span,
.pf-state {
  color: hsl(var(--ink-dim));
  font-size: 12px;
}

/* 权益列表:细分隔行 + 品牌点节奏(与登录页品牌要点同构),不用色条 */
.pf-benefit-list {
  list-style: none;
}

.pf-benefit {
  display: grid;
  grid-template-columns: 18px minmax(0, 1fr);
  align-items: start;
  column-gap: 9px;
  padding: 11px 0;
  color: hsl(var(--brand));
}

.pf-benefit + .pf-benefit {
  border-top: 1px solid hsl(var(--line) / 0.6);
}

.pf-benefit b {
  display: block;
  color: hsl(var(--ink));
  font-size: 13px;
  font-weight: 650;
}

.pf-benefit p {
  margin-top: 4px;
  color: hsl(var(--ink-dim));
  font-size: 12px;
  line-height: 1.55;
}

.pf-state--action {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

/* 空/错态保留稳定高度并与加载骨架对齐,避免卡片塌陷成一行留白 */
.pf-usage-empty,
.pf-benefits__body > .pf-state {
  display: flex;
  align-items: center;
  min-height: 160px;
}

/* 支持:整行入口按钮 */
.pf-support__body {
  display: grid;
  gap: 8px;
}

.pf-contact {
  justify-content: flex-start;
}

/* 退出:破坏性弱化为描边款;确认弹窗内保持实心负向色 */
.pf-danger__body {
  padding: 10px 12px;
}

.pf-logout {
  width: 100%;
  justify-content: center;
  background: hsl(var(--bad) / 0.1);
  color: hsl(var(--bad-fg));
  border-color: hsl(var(--bad) / 0.4);
}

.pf-logout:hover:not(:disabled) {
  background: hsl(var(--bad) / 0.18);
  border-color: hsl(var(--bad) / 0.6);
}

.pf-error {
  margin-top: 14px;
  padding: 8px 12px;
  border: 1px solid hsl(var(--bad) / 0.3);
  border-radius: 8px;
  background: hsl(var(--bad) / 0.1);
  color: hsl(var(--bad-fg));
  font-size: 12.5px;
}
</style>
