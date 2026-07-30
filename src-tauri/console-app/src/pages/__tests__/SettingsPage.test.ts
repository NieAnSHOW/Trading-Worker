import { flushPromises, mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";
import SettingsPage from "../SettingsPage.vue";

const mocks = vi.hoisted(() => ({
  consoleImportUserApiConfig: vi.fn(),
}));

vi.mock("../../ipc/commands", () => mocks);

describe("SettingsPage", () => {
  beforeEach(() => {
    mocks.consoleImportUserApiConfig.mockReset();
    mocks.consoleImportUserApiConfig.mockResolvedValue({
      userApiUrl: "https://api.example.test",
    });
  });

  it("imports the selected JSON file and displays its user API URL", async () => {
    const wrapper = mount(SettingsPage);
    const content = '{"version":1,"userApiUrl":"https://api.example.test"}';
    const file = new File(
      [content],
      "desktop-config.json",
      { type: "application/json" },
    );
    Object.defineProperty(file, "text", { value: async () => content });

    const input = wrapper.get('[data-test="service-config-file"]');
    Object.defineProperty(input.element, "files", { value: [file] });
    await input.trigger("change");
    await flushPromises();

    expect(mocks.consoleImportUserApiConfig).toHaveBeenCalledWith(await file.text());
    expect(wrapper.get('[role="status"]').text()).toContain("https://api.example.test");
  });

  it("shows a safe error when import fails and leaves the file input usable", async () => {
    mocks.consoleImportUserApiConfig.mockRejectedValueOnce(new Error("invalid config"));
    const wrapper = mount(SettingsPage);
    const file = new File(["not json"], "desktop-config.json", {
      type: "application/json",
    });
    Object.defineProperty(file, "text", { value: async () => "not json" });

    const input = wrapper.get('[data-test="service-config-file"]');
    Object.defineProperty(input.element, "files", { value: [file] });
    await input.trigger("change");
    await flushPromises();

    expect(wrapper.get('[role="alert"]').text()).toContain("无法导入配置");
    expect(wrapper.get('[data-test="service-config-file"]').attributes("disabled")).toBeUndefined();
  });

  it("clears the selected file after an import failure so it can be selected again", async () => {
    mocks.consoleImportUserApiConfig.mockRejectedValueOnce(new Error("invalid config"));
    const wrapper = mount(SettingsPage);
    const file = new File(["not json"], "desktop-config.json", {
      type: "application/json",
    });
    Object.defineProperty(file, "text", { value: async () => "not json" });
    const input = wrapper.get('[data-test="service-config-file"]');
    let selectedPath = "desktop-config.json";
    Object.defineProperty(input.element, "value", {
      configurable: true,
      get: () => selectedPath,
      set: (value: string) => {
        selectedPath = value;
      },
    });
    Object.defineProperty(input.element, "files", { value: [file] });

    await input.trigger("change");
    await flushPromises();

    expect(selectedPath).toBe("");
  });
});
