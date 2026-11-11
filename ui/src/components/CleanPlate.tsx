import type { Task } from "../lib/types";

const object = (value: unknown): Record<string, unknown> =>
  value && typeof value === "object" ? value as Record<string, unknown> : {};

export function CleanPlate({ task }: { task: Task }) {
  return <section className="clean-plate-section">
    <h3>无字底片 · 独立产物</h3>
    <div className="preview-grid">{(task.nodes ?? []).filter(n => n.definition_id === "clean").map(node => {
      const session = task.production_sessions?.find(s => s.node_id === node.id);
      const base = object(object(session).base);
      const hash = String(base.sha256 ?? node.output.sha256 ?? "");
      const artifact = task.artifacts?.find(a => a.kind === "video" && a.sha256 === hash && a.node_id === node.id);
      const progress = object(object(session).progress);
      const remaining = Array.isArray(progress.remaining) ? progress.remaining : [];
      const ready = Boolean(session?.finished && !remaining.length &&
        node.output.quality_passed !== false && node.output.completion_state !== "best_available");
      return <article key={node.id} className="evidence-card">
        <strong>{ready ? "无字底片 · 待完整视听验收" : "清底工作版 · 未完成，有原文残留或待核实区间"}</strong>
        <p>{ready ? "局部记录已覆盖，仍需检查实际整片效果。" :
          `已接受 ${session?.accepted.length ?? 0} 段局部修补；尚未做好的无字底片不能作为合格成品。`}</p>
        {artifact ? <>
          <video controls preload="metadata" src={artifact.content_url} aria-label="无字底片视频" />
          <a href={`${artifact.content_url}?download=true`} download>下载{ready ? "无字底片" : "清底工作版"}</a>
          <small className="hash">SHA256 {hash}</small>
        </> : <p>暂无可核验的无字底片文件；不会使用译片代替。</p>}
        {remaining.length > 0 && <details><summary>尚未清理完成的文字 · {remaining.length} 项</summary>
          {remaining.map((entry, i) => {
            const row = object(entry);
            return <p key={i}>{String(row.source_id)} · {JSON.stringify(row.intervals_ms)} ms</p>;
          })}</details>}
      </article>;
    })}</div>
  </section>;
}
