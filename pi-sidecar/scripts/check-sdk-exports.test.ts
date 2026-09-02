import { describe, expect, test } from "bun:test";

const REQUIRED_EXPORTS = [
  "createAgentSession",
  "SessionManager",
  "Settings",
  "AuthStorage",
  "ModelRegistry",
  "discoverAuthStorage",
  "createTools",
  "BUILTIN_TOOLS",
] as const;

describe("pinned SDK export surface", () => {
  test("@oh-my-pi/pi-coding-agent@18.0.11 exports required symbols", async () => {
    const pkg: Record<string, unknown> = await import("@oh-my-pi/pi-coding-agent");
    for (const name of REQUIRED_EXPORTS) {
      expect(name in pkg ? pkg[name] : undefined, `missing export: ${name}`).toBeDefined();
    }
  });

  test("installed version is exactly 18.0.11", async () => {
    const pkgJson = await Bun.file(new URL("../package.json", import.meta.url)).json();
    expect(pkgJson.dependencies["@oh-my-pi/pi-coding-agent"]).toBe("18.0.11");
  });
});
