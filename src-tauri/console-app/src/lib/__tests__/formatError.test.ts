import { describe, expect, it } from "vitest";
import { formatError } from "../formatError";

describe("formatError", () => {
  it("passes strings through and maps falsy to empty", () => {
    expect(formatError("boom")).toBe("boom");
    expect(formatError("")).toBe("");
    expect(formatError(null)).toBe("");
  });

  it("uses the message of native Errors", () => {
    expect(formatError(new Error("network down"))).toBe("network down");
  });

  it("extracts message/detail/error from plain objects instead of [object Object]", () => {
    expect(formatError({ message: "tauri cmd failed" })).toBe("tauri cmd failed");
    expect(formatError({ detail: "backend detail" })).toBe("backend detail");
    expect(formatError({ error: "nested" })).toBe("nested");
  });

  it("falls back to JSON for shapeless objects and never returns [object Object]", () => {
    expect(formatError({ code: 42 })).toBe('{"code":42}');
    const cyclic: Record<string, unknown> = {};
    cyclic.self = cyclic;
    expect(formatError(cyclic)).not.toBe("[object Object]");
  });
});
