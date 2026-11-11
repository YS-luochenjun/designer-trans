import { useEffect, useState } from "react";
import { api } from "../lib/api";
import type { Task } from "../lib/types";
import { ErrorBox } from "./common";

export function LocalPreview({ task, refresh }: { task: Task; refresh: () => void }) {
  const [asset, setAsset] = useState(task.asset_ids[0] ?? "");
  const [locale, setLocale] = useState(task.target_locales[0] ?? "");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [supported, setSupported] = useState(false);
  const cleanReady = Boolean(task.production_sessions?.length &&
    task.production_sessions.every(s => s.finished) &&
    task.nodes?.filter(n => n.definition_id === "clean").every(n =>
      n.output.quality_passed !== false && n.output.completion_state !== "best_available"));
  useEffect(() => {
    api<{ features: string[] }>("/health").then(result =>
      setSupported(result.features.includes("local_preview"))).catch(() => setSupported(false));
  }, []);
  const render = async () => {
    setBusy(true); setError(""); setMessage("");
    try {
      const result = await api<{ rendered_layers: number; omitted_layers: unknown[] }>(
        `/tasks/${task.id}/local-preview`, {
          method: "POST", body: JSON.stringify({ asset_id: asset, locale }),
        });
      setMessage(`已生成未验收草稿：${result.rendered_layers} 项译文字层，${result.omitted_layers.length} 项尚未呈现。详见草稿记录。`);
      refresh();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally { setBusy(false); }
  };
  return <section className="evidence-card">
    <strong>阶段预览</strong>
    <p>无字底片完成后才能合成译文预览；有原文残留时先检查上方清底工作版。</p>
    <div className="actions">
      <select aria-label="预览原片" value={asset} onChange={e => setAsset(e.target.value)}>
        {(task.assets ?? []).map(a => <option key={a.id} value={a.id}>{a.name}</option>)}
      </select>
      <select aria-label="预览语言" value={locale} onChange={e => setLocale(e.target.value)}>
        {task.target_locales.map(value => <option key={value}>{value}</option>)}
      </select>
      <button onClick={render} disabled={!cleanReady || !supported || busy || task.status === "running" || task.status === "cancelled" || task.control_paused}>
        {busy ? "正在合成草稿…" : "生成未验收草稿"}
      </button>
    </div>
    {!supported && <p>工作台后端需重启以加载草稿接口；已生成的草稿仍可查看。</p>}
    {message && <p role="status">{message}</p>}
    <ErrorBox message={error} />
  </section>;
}
