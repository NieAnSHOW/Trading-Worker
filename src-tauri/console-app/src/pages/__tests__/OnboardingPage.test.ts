import { beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { nextTick } from "vue";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { createMemoryHistory, createRouter } from "vue-router";
import type { AuthStatusView, StatusReport } from "../../ipc/types";
import { config } from "../../config/prod";

const mocks = vi.hoisted(() => ({
  consoleStatus: vi.fn(async (): Promise<StatusReport> => ({
    env: "ready" as const,
    service_running: false,
    port: null,
  })),
  consoleAuthStatus: vi.fn(async (): Promise<AuthStatusView> => ({
    authenticated: true,
    userInfo: { id: 1, nickName: "Tester", gender: 0, status: 1, loginType: 2 },
    expireAt: 9999999999,
  })),
  consoleOpenLogs: vi.fn(async (): Promise<void> => {}),
  consoleGetMirror: vi.fn(async (): Promise<string> => "tsinghua"),
  consoleToggleMirror: vi.fn(async (): Promise<string> => "aliyun"),
  bootstrapExitHandler: null as ((code: number) => unknown) | null,
  unlisten: vi.fn(),
}));

vi.mock("../../ipc/commands", () => ({
  consoleStatus: mocks.consoleStatus,
  consoleAuthStatus: mocks.consoleAuthStatus,
  consoleBootstrap: vi.fn(),
  consoleOpenWebui: vi.fn(),
  consoleOpenLogs: mocks.consoleOpenLogs,
  consoleGetMirror: mocks.consoleGetMirror,
  consoleToggleMirror: mocks.consoleToggleMirror,
  consoleQuit: vi.fn(),
  consoleLogout: vi.fn(),
  consoleFetchAds: vi.fn(async () => []),
  consoleGetPublicConfig: vi.fn(async () => ({
    officialUrl: "",
    enableLogin: true,
    checkUpdate: false,
    enableService: false,
    serviceQrCode: "",
    kefuQrCode: "",
    rewardQrCode: "",
    enableAd: true,
    imgBase: "",
  })),
  consoleStartService: vi.fn(async () => 8899),
  consoleStopService: vi.fn(),
  consoleChannelsStatus: vi.fn(),
  consoleCheckUpdate: vi.fn(),
  consoleDownloadUpdate: vi.fn(),
  consoleInstallUpdate: vi.fn(),
  consoleLoginCaptcha: vi.fn(async () => ({
    captchaId: "captcha-1",
    data: "data:image/svg+xml;base64,AA==",
  })),
  consoleLoginSendSms: vi.fn(),
  consoleLoginByPhone: vi.fn(),
  consoleLoginByPassword: vi.fn(),
  consoleLoginRegister: vi.fn(),
  consoleLoginSetPassword: vi.fn(),
}));

vi.mock("../../ipc/events", () => ({
  onBootstrapEvent: vi.fn(async () => mocks.unlisten),
  onBootstrapExit: vi.fn(async (handler: (code: number) => unknown) => {
    mocks.bootstrapExitHandler = handler;
    return mocks.unlisten;
  }),
  onServiceStarted: vi.fn(async () => mocks.unlisten),
  onQuitRequested: vi.fn(async () => mocks.unlisten),
  onChanneldepProgress: vi.fn(async () => mocks.unlisten),
  onChanneldepExit: vi.fn(async () => mocks.unlisten),
  onUpdateProgress: vi.fn(async () => mocks.unlisten),
}));

import OnboardingPage from "../OnboardingPage.vue";
import LoginPage from "../LoginPage.vue";
import { useAuthStore } from "../../stores/auth";

const EmptyRoute = { template: "<div />" };

const router = createRouter({
  history: createMemoryHistory(),
  routes: [
    { path: "/", component: OnboardingPage },
    { path: "/login", component: LoginPage },
    { path: "/profile", component: EmptyRoute },
    { path: "/settings", component: EmptyRoute },
  ],
});

beforeAll(() => {
  Object.defineProperties(HTMLDialogElement.prototype, {
    showModal: {
      configurable: true,
      value(this: HTMLDialogElement) {
        this.open = true;
      },
    },
    close: {
      configurable: true,
      value(this: HTMLDialogElement, returnValue = "") {
        this.returnValue = returnValue;
        this.open = false;
      },
    },
  });
});

beforeEach(async () => {
  vi.clearAllMocks();
  mocks.bootstrapExitHandler = null;
  config.enableLogin = true;
  setActivePinia(createPinia());
  await router.push("/");
  await router.isReady();
});

describe("OnboardingPage", () => {
  it("marks the onboarding page ready after mounting", async () => {
    const wrapper = mount(OnboardingPage, { global: { plugins: [router] } });

    await nextTick();
    expect(wrapper.classes()).toContain("onboarding-page--ready");
  });

  it("keeps the page in its startup state until the content animation ends", async () => {
    const wrapper = mount(OnboardingPage, { global: { plugins: [router] } });

    await nextTick();
    expect(wrapper.classes()).toContain("onboarding-page--entering");

    await wrapper.get(".onboarding-page").trigger("animationend");
    expect(wrapper.classes()).not.toContain("onboarding-page--entering");
  });

  it("keeps the startup placeholder free of the application shell", () => {
    const wrapper = mount(OnboardingPage, { global: { plugins: [router] } });

    expect(wrapper.find(".app-header").exists()).toBe(false);
    expect(wrapper.find(".console-shell").exists()).toBe(false);
  });

  it("shows only the startup placeholder while the environment is ready", async () => {
    const wrapper = mount(OnboardingPage, { global: { plugins: [router] } });

    await flushPromises();

    expect(wrapper.find(".onboarding-logo").exists()).toBe(true);
    expect(wrapper.get(".onboarding-status").text()).toBe("应用启动中...");
    expect(wrapper.find('[data-test="repair-environment"]').exists()).toBe(false);
    expect(wrapper.find(".service-panel").exists()).toBe(false);
  });

  it("redirects signed-out users after the environment is ready", async () => {
    const { consoleStartService } = await import("../../ipc/commands");
    mocks.consoleAuthStatus.mockResolvedValueOnce({
      authenticated: false,
      userInfo: null,
      expireAt: 0,
    });

    mount(OnboardingPage, { global: { plugins: [router] } });
    await flushPromises();

    expect(router.currentRoute.value.path).toBe("/login");
    expect(consoleStartService).not.toHaveBeenCalled();
  });

  it("starts research without checking auth when login is disabled", async () => {
    const { consoleStartService } = await import("../../ipc/commands");
    config.enableLogin = false;

    mount(OnboardingPage, { global: { plugins: [router] } });
    await flushPromises();

    expect(mocks.consoleAuthStatus).not.toHaveBeenCalled();
    expect(consoleStartService).toHaveBeenCalledTimes(1);
  });

  it("starts research for an authenticated user in a ready environment", async () => {
    const { consoleStartService, consoleOpenWebui } = await import("../../ipc/commands");

    mount(OnboardingPage, { global: { plugins: [router] } });
    await flushPromises();

    expect(mocks.consoleAuthStatus).toHaveBeenCalledTimes(1);
    expect(consoleStartService).toHaveBeenCalledTimes(1);
    expect(consoleOpenWebui).toHaveBeenCalledWith(8899);
  });

  it("reopens an already-running service after re-login instead of hanging on onboarding", async () => {
    const { consoleStartService, consoleOpenWebui } = await import("../../ipc/commands");
    mocks.consoleStatus.mockResolvedValueOnce({
      env: "ready",
      service_running: true,
      port: 8899,
    });

    mount(OnboardingPage, { global: { plugins: [router] } });
    await flushPromises();

    expect(mocks.consoleAuthStatus).toHaveBeenCalledTimes(1);
    expect(consoleStartService).not.toHaveBeenCalled();
    expect(consoleOpenWebui).toHaveBeenCalledWith(8899);
  });

  it("resumes service startup after a user returns from login", async () => {
    const { consoleStartService } = await import("../../ipc/commands");
    const flowRouter = createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: "/", component: OnboardingPage },
        { path: "/login", component: EmptyRoute },
      ],
    });
    mocks.consoleAuthStatus.mockResolvedValueOnce({
      authenticated: false,
      userInfo: null,
      expireAt: 0,
    });

    await flowRouter.push("/");
    await flowRouter.isReady();
    mount({ template: "<router-view />" }, { global: { plugins: [flowRouter] } });
    await flushPromises();
    expect(flowRouter.currentRoute.value.path).toBe("/login");

    useAuthStore().setFromLogin({
      userInfo: { id: 1, nickName: "Tester", gender: 0, status: 1, loginType: 2 },
      expireAt: 9999999999,
    });
    await flowRouter.replace("/");
    await flushPromises();

    expect(flowRouter.currentRoute.value.path).toBe("/");
    expect(consoleStartService).toHaveBeenCalledTimes(1);
  });

  it("shows the repair button when the environment is incomplete", async () => {
    mocks.consoleStatus.mockResolvedValueOnce({
      env: "incomplete",
      service_running: false,
      port: null,
    });
    const wrapper = mount(OnboardingPage, { global: { plugins: [router] } });

    await flushPromises();

    expect(wrapper.get('[data-test="repair-environment"]').text()).toContain("安装/修复环境");
    expect(wrapper.get(".onboarding-status").text()).toBe("请先点击安装环境");
    expect(mocks.consoleAuthStatus).not.toHaveBeenCalled();
  });

  it("exposes quick actions to open logs and toggle the install mirror", async () => {
    mocks.consoleStatus.mockResolvedValueOnce({
      env: "incomplete",
      service_running: false,
      port: null,
    });
    const wrapper = mount(OnboardingPage, { global: { plugins: [router] } });
    await flushPromises();

    // 默认镜像名在挂载时读取;按钮展示当前安装源。
    expect(wrapper.get('[data-test="toggle-mirror-action"]').text()).toContain("清华");

    await wrapper.get('[data-test="open-logs-action"]').trigger("click");
    expect(mocks.consoleOpenLogs).toHaveBeenCalledOnce();

    await wrapper.get('[data-test="toggle-mirror-action"]').trigger("click");
    await flushPromises();
    expect(mocks.consoleToggleMirror).toHaveBeenCalledOnce();
    expect(wrapper.get('[data-test="toggle-mirror-action"]').text()).toContain("阿里云");
  });

  it("starts bootstrap from the repair button", async () => {
    const { consoleBootstrap } = await import("../../ipc/commands");
    const bootstrapCommand = vi.mocked(consoleBootstrap);
    mocks.consoleStatus.mockResolvedValueOnce({
      env: "not_installed",
      service_running: false,
      port: null,
    });
    const wrapper = mount(OnboardingPage, { global: { plugins: [router] } });

    await flushPromises();
    await wrapper.get('[data-test="repair-environment"]').trigger("click");

    expect(bootstrapCommand).toHaveBeenCalledTimes(1);
  });

  it("starts the service after a successful bootstrap reaches a ready environment", async () => {
    const { consoleStartService, consoleOpenWebui } = await import("../../ipc/commands");
    const start = vi.mocked(consoleStartService);
    const open = vi.mocked(consoleOpenWebui);
    start.mockResolvedValue(8899);
    mocks.consoleStatus
      .mockResolvedValueOnce({ env: "not_installed", service_running: false, port: null })
      .mockResolvedValue({ env: "ready", service_running: false, port: null });

    mount(OnboardingPage, { global: { plugins: [router] } });
    await flushPromises();
    await mocks.bootstrapExitHandler?.(0);
    await flushPromises();

    expect(start).toHaveBeenCalledTimes(1);
    expect(open).toHaveBeenCalledWith(8899);
  });

  it("returns to login instead of starting service after bootstrap when signed out", async () => {
    const { consoleStartService } = await import("../../ipc/commands");
    mocks.consoleStatus
      .mockResolvedValueOnce({ env: "not_installed", service_running: false, port: null })
      .mockResolvedValue({ env: "ready", service_running: false, port: null });
    mocks.consoleAuthStatus.mockResolvedValueOnce({
      authenticated: false,
      userInfo: null,
      expireAt: 0,
    });

    mount(OnboardingPage, { global: { plugins: [router] } });
    await flushPromises();
    await mocks.bootstrapExitHandler?.(0);
    await flushPromises();

    expect(router.currentRoute.value.path).toBe("/login");
    expect(consoleStartService).not.toHaveBeenCalled();
  });

  it("keeps the onboarding error visible when bootstrap fails", async () => {
    const wrapper = mount(OnboardingPage, { global: { plugins: [router] } });
    await flushPromises();

    await mocks.bootstrapExitHandler?.(1);
    await flushPromises();

    expect(wrapper.get("#err").text()).toContain("依赖安装失败");
    expect(wrapper.get('[data-test="repair-environment"]').text()).toContain("安装/修复环境");
  });
});
