export interface ProviderRegistration {
  id: string;
  name: string;
  baseUrl: string;
  envKey: string;
}

type Env = Record<string, string | undefined>;

/** Vibe llm_providers.json 的 OpenAI 兼容子集（recon §7.1 表）。 */
const OPENAI_COMPAT_PROVIDERS = [
  { id: "deepseek", envKey: "DEEPSEEK_API_KEY", baseUrlEnv: "DEEPSEEK_BASE_URL", defaultBase: "https://api.deepseek.com/v1" },
  { id: "zhipu", envKey: "ZHIPU_API_KEY", baseUrlEnv: "ZHIPU_BASE_URL", defaultBase: "https://open.bigmodel.cn/api/paas/v4" },
  { id: "dashscope", envKey: "DASHSCOPE_API_KEY", baseUrlEnv: "DASHSCOPE_BASE_URL", defaultBase: "https://dashscope.aliyuncs.com/compatible-mode/v1" },
  { id: "moonshot", envKey: "MOONSHOT_API_KEY", baseUrlEnv: "MOONSHOT_BASE_URL", defaultBase: "https://api.moonshot.cn/v1" },
  { id: "minimax", envKey: "MINIMAX_API_KEY", baseUrlEnv: "MINIMAX_BASE_URL", defaultBase: "https://api.minimax.chat/v1" },
  { id: "groq", envKey: "GROQ_API_KEY", baseUrlEnv: "GROQ_BASE_URL", defaultBase: "https://api.groq.com/openai/v1" },
] as const;

/** Vibe provider 名 → Pi provider id（qwen 是 dashscope 的别名，recon §7.1）。 */
const PROVIDER_ALIASES: Record<string, string> = { qwen: "dashscope" };

const LEGACY_ONLY = new Set(["vip_server", "openai-codex"]);

export function resolveDefaultModel(env: Env = process.env): string | null {
  const provider = (env.LANGCHAIN_PROVIDER ?? "openai").trim();
  const model = (env.LANGCHAIN_MODEL_NAME ?? "").trim();
  if (!model) return null;
  if (LEGACY_ONLY.has(provider)) return null; // legacy-engine-only（Global Constraint）
  const piId = PROVIDER_ALIASES[provider] ?? provider;
  return `${piId}/${model}`;
}

export function vibeProviderRegistrations(env: Env = process.env): ProviderRegistration[] {
  const out: ProviderRegistration[] = [];
  for (const p of OPENAI_COMPAT_PROVIDERS) {
    const key = env[p.envKey];
    const baseOverride = env[p.baseUrlEnv];
    if (!key && !baseOverride) continue; // 设了 key 或 baseUrl 任一项即视为已配置（测试覆盖 groq 仅 baseUrl 场景）
    out.push({
      id: p.id,
      name: p.id,
      baseUrl: (baseOverride ?? p.defaultBase).replace(/\/$/, ""),
      envKey: p.envKey,
    });
  }
  return out;
}
