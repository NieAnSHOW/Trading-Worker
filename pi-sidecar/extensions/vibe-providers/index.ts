import type { ExtensionAPI } from "@oh-my-pi/pi-coding-agent";
import { vibeProviderRegistrations } from "../../src/model-wiring";

/**
 * factory load 时把 Vibe 的 OpenAI 兼容 provider 注册进 Pi（Global Constraint: model wiring）。
 * 签名以 pinned SDK 的 `ExtensionAPI.registerProvider(name, config)` 为准（Task 0 Step 7 已核对）：
 * provider 名 = 第一参数；`apiKey` 传环境变量名（字符串），Pi 在请求时经 env 链解析。
 */
export default function (pi: ExtensionAPI) {
  for (const reg of vibeProviderRegistrables()) {
    pi.registerProvider(reg.id, {
      baseUrl: reg.baseUrl,
      api: "openai-completions",
      apiKey: reg.envKey,
    });
  }
}

function vibeProviderRegistrables() {
  // 延迟读取，便于测试注入 env
  return vibeProviderRegistrations(process.env as Record<string, string | undefined>);
}
