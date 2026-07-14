import { useEffect, useRef, useState } from "react";
import { CheckCircle2, Download, Loader2, Package, Trash2, XCircle } from "lucide-react";
import { toast } from "sonner";
import {
  api,
  isAuthRequiredError,
  type MirrorInfo,
  type OptionalDepBroker,
} from "@/lib/api";
import { cn } from "@/lib/utils";

type JobState = Record<string, { stage: string; message: string; status: "running" | "done" | "failed" }>;

const MIRROR_OPTIONS: { value: MirrorInfo["name"]; label: string }[] = [
  { value: "tsinghua", label: "清华源 (默认)" },
  { value: "aliyun", label: "阿里云" },
  { value: "official", label: "官方 PyPI" },
  { value: "off", label: "关闭镜像 (回退官方)" },
  { value: "custom", label: "自定义" },
];

export function OptionalDepsManager() {
  const [brokers, setBrokers] = useState<OptionalDepBroker[]>([]);
  const [loading, setLoading] = useState(true);
  const [jobs, setJobs] = useState<JobState>({});
  const [mirror, setMirror] = useState<MirrorInfo | null>(null);
  const [mirrorSaving, setMirrorSaving] = useState(false);
  const [customUrl, setCustomUrl] = useState("");
  const jobStreams = useRef<Record<string, EventSource>>({});
  const mountedRef = useRef(true);
  const subscriptionGenerations = useRef<Record<string, number>>({});

  const load = () => {
    Promise.all([api.listOptionalDeps(), api.getOptionalDepsMirror()])
      .then(([list, mirrorInfo]) => {
        setBrokers(list.brokers ?? []);
        setMirror(mirrorInfo);
        setCustomUrl(mirrorInfo.custom_index_url ?? "");
      })
      .catch((err) => {
        if (!isAuthRequiredError(err)) {
          toast.error(`加载可选依赖失败: ${err instanceof Error ? err.message : String(err)}`);
        }
      })
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    mountedRef.current = true;
    load();
    return () => {
      mountedRef.current = false;
      // 关闭所有未完成的 SSE
      Object.values(jobStreams.current).forEach((es) => es.close());
      jobStreams.current = {};
    };
  }, []);

  const subscribe = async (jobId: string, pkg: string, generation: number) => {
    const url = await api.optionalDepStatusUrl(jobId);
    if (!mountedRef.current || subscriptionGenerations.current[pkg] !== generation) return;
    const es = new EventSource(url);
    jobStreams.current[pkg] = es;

    es.addEventListener("progress", (ev) => {
      try {
        const data = JSON.parse((ev as MessageEvent).data);
        setJobs((prev) => ({
          ...prev,
          [pkg]: { stage: data.stage, message: data.message, status: "running" },
        }));
      } catch { /* ignore malformed */ }
    });
    es.addEventListener("done", () => {
      setJobs((prev) => ({
        ...prev,
        [pkg]: { stage: "done", message: "安装完成", status: "done" },
      }));
      es.close();
      delete jobStreams.current[pkg];
      load(); // 刷新已装状态
    });
    es.addEventListener("failed", (ev) => {
      let message = "安装失败";
      try {
        message = JSON.parse((ev as MessageEvent).data).error || message;
      } catch { /* keep default */ }
      setJobs((prev) => ({
        ...prev,
        [pkg]: { stage: "failed", message, status: "failed" },
      }));
      es.close();
      delete jobStreams.current[pkg];
      toast.error(`${pkg} 安装失败: ${message}`);
    });
    es.onerror = () => {
      es.close();
      delete jobStreams.current[pkg];
    };
  };

  const install = async (pkg: string) => {
    try {
      setJobs((prev) => ({
        ...prev,
        [pkg]: { stage: "starting", message: "启动安装…", status: "running" },
      }));
      const { job_id } = await api.installOptionalDep(pkg);
      const generation = (subscriptionGenerations.current[pkg] ?? 0) + 1;
      subscriptionGenerations.current[pkg] = generation;
      void subscribe(job_id, pkg, generation).catch((err) => {
        if (!mountedRef.current || subscriptionGenerations.current[pkg] !== generation) return;
        const msg = err instanceof Error ? err.message : String(err);
        toast.error(`无法订阅安装状态: ${msg}`);
        setJobs((prev) => ({
          ...prev,
          [pkg]: { stage: "failed", message: msg, status: "failed" },
        }));
      });
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      toast.error(`无法启动安装: ${msg}`);
      setJobs((prev) => ({
        ...prev,
        [pkg]: { stage: "failed", message: msg, status: "failed" },
      }));
    }
  };

  const uninstall = async (pkg: string) => {
    try {
      await api.uninstallOptionalDep(pkg);
      toast.success(`已开始卸载 ${pkg}`);
      load();
    } catch (err) {
      toast.error(`卸载失败: ${err instanceof Error ? err.message : String(err)}`);
    }
  };

  const saveMirror = async () => {
    if (!mirror) return;
    setMirrorSaving(true);
    try {
      const updated = await api.updateOptionalDepsMirror({
        name: mirror.name,
        custom_index_url: mirror.name === "custom" ? customUrl : "",
      });
      setMirror(updated);
      toast.success("镜像源已更新");
    } catch (err) {
      toast.error(`保存镜像失败: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setMirrorSaving(false);
    }
  };

  if (loading) {
    return (
      <div className="flex items-center gap-2 text-sm text-muted-foreground">
        <Loader2 className="h-4 w-4 animate-spin" /> 加载可选依赖…
      </div>
    );
  }

  return (
    <section className="space-y-4">
      {/* Mirror selector */}
      {mirror && (
        <div className="rounded-lg border bg-card p-4 space-y-3">
          <div className="flex items-center gap-2 text-sm font-medium">
            <Package className="h-4 w-4" /> PyPI 镜像源
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <select
              className="rounded-md border bg-background px-3 py-2 text-sm"
              value={mirror.name}
              onChange={(e) => setMirror({ ...mirror, name: e.target.value as MirrorInfo["name"] })}
            >
              {MIRROR_OPTIONS.map((opt) => (
                <option key={opt.value} value={opt.value}>{opt.label}</option>
              ))}
            </select>
            {mirror.name === "custom" && (
              <input
                className="flex-1 min-w-[240px] rounded-md border bg-background px-3 py-2 text-sm"
                placeholder="https://your.mirror/simple"
                value={customUrl}
                onChange={(e) => setCustomUrl(e.target.value)}
              />
            )}
            <button
              onClick={saveMirror}
              disabled={mirrorSaving}
              className="rounded-md bg-primary px-3 py-2 text-sm text-primary-foreground disabled:opacity-60"
            >
              {mirrorSaving ? "保存中…" : "保存镜像"}
            </button>
          </div>
        </div>
      )}

      {/* Broker list */}
      <div className="space-y-2">
        {brokers.map((b) => {
          const job = jobs[b.package];
          const running = job?.status === "running";
          return (
            <div key={b.package} className="rounded-lg border bg-card p-4">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="font-medium text-sm">{b.label}</span>
                    {b.installed ? (
                      <span className="inline-flex items-center gap-1 text-xs text-green-600">
                        <CheckCircle2 className="h-3.5 w-3.5" />
                        已安装{b.installed_version ? ` · ${b.installed_version}` : ""}
                      </span>
                    ) : null}
                  </div>
                  <p className="text-xs text-muted-foreground mt-0.5">
                    {b.package} — {b.description}
                  </p>
                  <p className="text-[11px] text-muted-foreground/70 mt-0.5">
                    平台: {b.platforms.join(" / ")}
                  </p>
                </div>
                <div className="flex items-center gap-2 shrink-0">
                  {b.installed ? (
                    <button
                      onClick={() => uninstall(b.package)}
                      disabled={running}
                      className="inline-flex items-center gap-1 rounded-md border px-3 py-1.5 text-xs hover:bg-muted disabled:opacity-60"
                    >
                      <Trash2 className="h-3.5 w-3.5" /> 卸载
                    </button>
                  ) : (
                    <button
                      onClick={() => install(b.package)}
                      disabled={running}
                      className="inline-flex items-center gap-1 rounded-md bg-primary px-3 py-1.5 text-xs text-primary-foreground disabled:opacity-60"
                    >
                      {running ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Download className="h-3.5 w-3.5" />}
                      {running ? "安装中…" : "安装"}
                    </button>
                  )}
                </div>
              </div>
              {job && (
                <div
                  className={cn(
                    "mt-2 rounded-md px-2 py-1 text-[11px] font-mono",
                    job.status === "failed"
                      ? "bg-red-500/10 text-red-600"
                      : job.status === "done"
                        ? "bg-green-500/10 text-green-600"
                        : "bg-muted text-muted-foreground"
                  )}
                >
                  {job.status === "failed" && <XCircle className="inline h-3 w-3 mr-1" />}
                  {job.message}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </section>
  );
}
