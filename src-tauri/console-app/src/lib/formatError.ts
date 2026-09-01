// 错误对象在模板里直接 String() 会得到 "[object Object]"。
// Tauri invoke 拒绝值、fetch JSON 错误体、原生 Error 各取最有信息量的字段。
export function formatError(error: unknown): string {
  if (!error) return "";
  if (typeof error === "string") return error;
  if (error instanceof Error) return error.message || error.name;
  if (typeof error === "object") {
    const e = error as Record<string, unknown>;
    // Tauri 命令错误常见形如 { message } / 后端 JSON 错误体 { message | detail | error }
    for (const key of ["message", "detail", "error"]) {
      const v = e[key];
      if (typeof v === "string" && v) return v;
    }
    try {
      return JSON.stringify(error);
    } catch {
      return "[无法序列化的错误对象]";
    }
  }
  return String(error);
}
