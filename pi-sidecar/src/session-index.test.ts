import { afterAll, beforeAll, describe, expect, test } from "bun:test";
import { mkdtempSync, rmSync, writeFileSync, mkdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { SessionIndex, isValidVibeSessionId, readSessionHeaderId } from "./session-index";

let dir: string;
beforeAll(() => {
  dir = mkdtempSync(join(tmpdir(), "vibe-sidx-"));
});
afterAll(() => {
  rmSync(dir, { recursive: true, force: true });
});

const HDR = (id: string) => JSON.stringify({ type: "session", version: 3, id, cwd: dir, timestamp: "2026-08-30T00:00:00.000Z" });
const VIBE = "a1b2c3d4e5f6";

describe("isValidVibeSessionId", () => {
  test("accepts 12 lowercase hex", () => {
    expect(isValidVibeSessionId("a1b2c3d4e5f6")).toBe(true);
  });
  test("rejects wrong shapes", () => {
    for (const bad of ["", "ABCDEF123456", "a1b2c3d4e5f", "a1b2c3d4e5f60", "../evil"]) {
      expect(isValidVibeSessionId(bad)).toBe(false);
    }
  });
});

describe("readSessionHeaderId", () => {
  test("finds header in first 8KB", () => {
    const p = join(dir, "h1.jsonl");
    writeFileSync(p, HDR("1f9d2a6b9c0d") + "\n" + JSON.stringify({ type: "message", id: "m1", parentId: null }) + "\n");
    expect(readSessionHeaderId(p)).toBe("1f9d2a6b9c0d");
  });
  test("handles 256-byte title slot prefix (title entry precedes header)", () => {
    const p = join(dir, "h2.jsonl");
    const titleSlot = JSON.stringify({ type: "title", title: "t" }).padEnd(256, " ");
    writeFileSync(p, titleSlot + "\n" + HDR("2f9d2a6b9c0d") + "\n");
    expect(readSessionHeaderId(p)).toBe("2f9d2a6b9c0d");
  });
  test("missing file or no header -> null", () => {
    expect(readSessionHeaderId(join(dir, "nope.jsonl"))).toBeNull();
    const p = join(dir, "h3.jsonl");
    writeFileSync(p, '{"type":"message","id":"m1"}\n');
    expect(readSessionHeaderId(p)).toBeNull();
  });
});

describe("SessionIndex", () => {
  test("register + resolve round-trip with header presence validation", () => {
    const jsonl = join(dir, "20260830_000000_a1b2c3d4e5f6.jsonl");
    writeFileSync(jsonl, HDR(VIBE) + "\n");
    const idx = new SessionIndex(join(dir, "index.json"), dir);
    idx.register(VIBE, jsonl); // B2：headerId 记录为 VIBE（文件自身的 header id）
    expect(idx.resolve(VIBE)).toBe(jsonl);
    // 持久化：新实例可读回
    const idx2 = new SessionIndex(join(dir, "index.json"), dir);
    expect(idx2.resolve(VIBE)).toBe(jsonl);
  });

  test("register accepts a Pi-assigned header id different from vibeId (B2 core case)", () => {
    const jsonl = join(dir, "pi-assigned.jsonl");
    writeFileSync(jsonl, HDR("ffffffffffff") + "\n"); // SessionManager.create 自分配 id
    const idx = new SessionIndex(join(dir, "index2.json"), dir);
    idx.register(VIBE, jsonl); // 只要求存在合法 header，不要求 id === vibeId
    expect(idx.resolve(VIBE)).toBe(jsonl);
    expect(idx.all()[VIBE]).toEqual({ path: jsonl, headerId: "ffffffffffff" });
  });

  test("register rejects file without any session header", () => {
    const jsonl = join(dir, "noheader.jsonl");
    writeFileSync(jsonl, '{"type":"message","id":"m1"}\n');
    const idx = new SessionIndex(join(dir, "index2b.json"), dir);
    expect(() => idx.register(VIBE, jsonl)).toThrow(/header/i);
    expect(idx.resolve(VIBE)).toBeNull();
  });

  test("resolve detects file replacement (recorded headerId no longer matches)", () => {
    const jsonl = join(dir, "mutated.jsonl");
    writeFileSync(jsonl, HDR(VIBE) + "\n");
    const idx = new SessionIndex(join(dir, "index3.json"), dir);
    idx.register(VIBE, jsonl);
    writeFileSync(jsonl, HDR("ffffffffffff") + "\n"); // 文件被替换 → headerId 变化
    expect(idx.resolve(VIBE)).toBeNull();
  });

  test("rejects invalid vibe ids", () => {
    const idx = new SessionIndex(join(dir, "index4.json"), dir);
    expect(() => idx.register("BAD ID", join(dir, "x.jsonl"))).toThrow(/invalid/i);
    expect(idx.resolve("../etc/passwd")).toBeNull();
  });
});
