import type { ReactNode } from "react";
import type { ProductionSession, Task, TaskNode } from "../lib/types";
import { time } from "../lib/types";
import { Status } from "./common";
import { imageRequestState } from "./TaskCallRecords";

export function nodeProgress(node: TaskNode, sessions: ProductionSession[] = [], nodes: TaskNode[] = []): string {
  if (node.output.completion_state === "best_available") {
    return node.definition_id === "clean" ? "清底未完成 · 有残字，旧接续路线已停用" : "历史候选 · 尚未质量通过";
  }
  if (node.status === "skipped") return "跳过";
  if (["succeeded", "completed"].includes(node.status)) return "100%";
  if (node.status === "pending" && !node.attempt) return "尚未开始";
  const session = sessions.find(item => item.node_id === node.id);
  if (session) {
    const recorded = object(object(session).progress);
    if (typeof recorded.coverage_percent === "number") {
      return `记录覆盖 ${recorded.coverage_percent}% · ${session.accepted.length} 段已接受`;
    }
    const source = nodes.find(item => item.definition_id === "observe" && item.asset_id === node.asset_id);
    const texts = Array.isArray(source?.output.texts) ? source.output.texts.map(object)
      .filter(item => item.treatment === "translate") : [];
    let total = 0, covered = 0;
    for (const item of texts) {
      const revised = object(session.text_overrides?.[String(item.id)]);
      const start = Number(revised.start_ms ?? item.start_ms), end = Number(revised.end_ms ?? item.end_ms);
      if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) continue;
      total += end - start;
      const intervals = session.accepted.filter(repair => repair.source_id === item.id && !repair.needs_scope_review)
        .map(repair => [Math.max(start, Number(repair.start_ms)), Math.min(end, Number(repair.end_ms))])
        .filter(([a, b]) => Number.isFinite(a) && Number.isFinite(b) && b > a).sort((a, b) => a[0] - b[0]);
      let cursor = start;
      for (const [a, b] of intervals) {
        covered += Math.max(0, b - Math.max(a, cursor));
        cursor = Math.max(cursor, b);
      }
    }
    return total ? `覆盖约 ${Math.min(99, Math.round(covered / total * 100))}% · ${session.accepted.length} 项已接受`
      : `已接受 ${session.accepted.length} 项 · 第 ${session.turn + 1} 轮`;
  }
  if (node.status === "waiting_external") return "等待模型结果";
  if (node.status === "waiting_user") return "等待确认";
  if (["failed", "blocked", "unknown", "interrupted"].includes(node.status)) return "进度停留 · 待处理";
  if (node.status === "cancelled") return "已停止";
  return node.artifact_ids.length ? `已产出 ${node.artifact_ids.length} 项 · 处理中` : "处理中";
}

const object = (value: unknown): Record<string, unknown> =>
  value && typeof value === "object" ? value as Record<string, unknown> : {};
const kindNames: Record<string, string> = {
  video_edit: "视频编辑", image_edit: "图片编辑", synthesize: "配音", audio_understanding: "音频理解",
};

function callModel(task: Task, op: Record<string, unknown>): string {
  if (typeof op.model === "string") return op.model;
  const models = object(object(task.model_snapshot).models);
  const role = { video_edit: "video", synthesize: "tts", audio_understanding: "transcription" }[String(op.kind)];
  const model = role ? object(models[role]).model : op.kind === "image_edit" ? object(task.image_snapshot).model : undefined;
  // Frozen task configuration is useful context, but is not a per-call model receipt.
  return typeof model === "string" ? `${model}（任务配置）` : kindNames[String(op.kind)] ?? String(op.kind);
}

export function NodeOverview({ task, node, notice, onShowCalls }: {
  task: Task; node: TaskNode; notice?: ReactNode; onShowCalls?: () => void;
}) {
  const traces = (task.agent_traces ?? []).filter(item => item.node_id === node.id);
  const resources = new Map<string, { name: string; kind: string; consulted: boolean }>();
  for (const trace of traces) for (const resource of trace.resources) {
    if (resource.kind === "prompts") continue;
    resources.set(resource.id, { name: resource.name, kind: resource.kind,
      consulted: Boolean(resources.get(resource.id)?.consulted || trace.requested_resources.includes(resource.id)) });
  }
  const calls = [...new Map((task.operations ?? []).filter(op => op.node_id === node.id)
    .map(op => [String(op.id), op])).values()]
    .sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)));
  const agentAction = (op: Record<string, unknown>) => {
    const trace = traces.find(item => item.created_at <= String(op.created_at) &&
      (!item.finished_at || item.finished_at >= String(op.finished_at ?? op.created_at)));
    const action = String(trace?.result?.action ?? "");
    return ({ inspect: "补充观察", repair: "选择修复方案", accept: "检查并接受候选",
      restore_candidate: "复用已有候选",
      inspect_archive: "检查历史结果", rebase_candidate: "本地重合成", defer: "暂缓该实例",
      inspect_pipeline: "诊断生成链路", reject_candidate: "拒绝候选",
      revise_scope: "修订范围", consult: "查阅知识", finish: "完成检查", blocked: "报告阻塞" }[action])
      ?? (op.status === "running" ? "正在判断" : "制作判断");
  };
  const callRow = (op: Record<string, unknown>) => <li key={String(op.id)}>
    <div><span>{op.provider === "agent" ? "调用 Agent" : `调用 ${String(op.provider).toUpperCase()}`}：{
      op.provider === "agent" ? agentAction(op) : callModel(task, op)}</span>
      <Status value={op.status === "unknown" && op.journal_state === "waiting" && op.resume_safe ? "waiting_external" : String(op.status)} /></div>
    <time>{time(String(op.created_at))}</time>
  </li>;
  return <section className="node-overview" aria-label="节点进展">
    <p className="node-progress-label" title="清底覆盖按已接受修复的文字时段估算，不代表成片质量验收。">{nodeProgress(node, task.production_sessions, task.nodes)}</p>
    {notice}
    {(task.imagegen_requests ?? []).filter(r => r.node_id === node.id &&
      (r.status === "pending" || r.status === "claimed")).map(request => <div key={request.id}>
      <p>图片去字 · {(request.time_ms / 1000).toFixed(3)} 秒：{imageRequestState(request.lifecycle)}</p>
      {request.dispatch_blocked_reason && <p>{request.dispatch_blocked_reason}</p>}
      {onShowCalls && <button className="text-button" onClick={onShowCalls}>查看请求内容与执行回执</button>}
    </div>)}
    <details className="node-activity-box node-resource-box"><summary>技能与知识 · {resources.size}</summary><div className="node-activity-scroll" tabIndex={0}>
      <ul className="node-brief-list">{[...resources].map(([id, resource]) => <li key={id}>
        <span>{resource.kind === "skills" ? "技能" : "知识"} · {resource.name}</span>
        <small>{resource.consulted ? "主动查阅" : "随任务提供"}</small>
      </li>)}</ul>
      {!resources.size && <span>暂无记录</span>}
    </div></details>
    <details className="node-activity-box"><summary>最近调用 · {Math.min(5, calls.length)} / {calls.length}</summary><div className="node-activity-scroll" tabIndex={0}>
      <ul className="node-brief-list">{calls.slice(0, 5).map(callRow)}</ul>
      {!calls.length && <span>暂无调用</span>}
    </div>{onShowCalls && calls.length > 0 && <button className="text-button node-record-link" onClick={onShowCalls}>查看本节点全部调用</button>}</details>
  </section>;
}
