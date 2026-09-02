import { describe, expect, test } from "bun:test";
import { resolveDefaultModel, vibeProviderRegistrations } from "./model-wiring";

describe("resolveDefaultModel", () => {
  test("maps LANGCHAIN_PROVIDER + LANGCHAIN_MODEL_NAME", () => {
    expect(resolveDefaultModel({ LANGCHAIN_PROVIDER: "deepseek", LANGCHAIN_MODEL_NAME: "deepseek-chat" })).toBe(
      "deepseek/deepseek-chat",
    );
  });

  test("qwen alias maps to dashscope", () => {
    expect(resolveDefaultModel({ LANGCHAIN_PROVIDER: "qwen", LANGCHAIN_MODEL_NAME: "qwen-max" })).toBe(
      "dashscope/qwen-max",
    );
  });

  test("vip_server and openai-codex -> null (legacy-only)", () => {
    expect(resolveDefaultModel({ LANGCHAIN_PROVIDER: "vip_server", LANGCHAIN_MODEL_NAME: "x" })).toBeNull();
    expect(resolveDefaultModel({ LANGCHAIN_PROVIDER: "openai-codex", LANGCHAIN_MODEL_NAME: "x" })).toBeNull();
  });

  test("unset provider defaults to openai", () => {
    expect(resolveDefaultModel({ LANGCHAIN_MODEL_NAME: "gpt-4o" })).toBe("openai/gpt-4o");
  });

  test("no model name -> null", () => {
    expect(resolveDefaultModel({})).toBeNull();
  });
});

describe("vibeProviderRegistrations", () => {
  test("only includes providers whose key env is set", () => {
    const regs = vibeProviderRegistrations({ DEEPSEEK_API_KEY: "sk-x", ZHIPU_API_KEY: "z", GROQ_BASE_URL: "http://127.0.0.1:9/v1" });
    const ids = regs.map((r) => r.id);
    expect(ids).toContain("deepseek");
    expect(ids).toContain("zhipu");
    expect(ids).toContain("groq");
    expect(ids).not.toContain("moonshot");
    expect(regs.find((r) => r.id === "groq")!.baseUrl).toBe("http://127.0.0.1:9/v1"); // env 覆盖
    expect(regs.find((r) => r.id === "deepseek")!.envKey).toBe("DEEPSEEK_API_KEY");
  });

  test("empty env -> empty list", () => {
    expect(vibeProviderRegistrations({})).toEqual([]);
  });
});
