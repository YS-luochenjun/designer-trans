import type { AgentTrace, TaskNode } from "../lib/types";
import { time } from "../lib/types";
import { ErrorBox, JsonView, Status } from "./common";

const actions: Record<string, string> = {
  inspect_archive: "检查历史结果", rebase_candidate: "本地重合成", defer: "暂缓该实例",
  restore_candidate: "复用已有候选",
  inspect_pipeline: "诊断生成链路", reject_candidate: "拒绝候选并保留证据",
  inspect: "补帧观察", revise_scope: "修订文字范围", consult: "查阅方法", repair: "局部修复", accept: "确认候选",
  finish: "完成判断", revise: "局部修订", blocked: "能力不足", clarify: "需要补充",
};
const methods: Record<string, string> = {
  flat: "纯色背景补底", donor: "同源帧补底", image_edit: "图片修复", wan: "局部视频编辑",
};
const object = (value: unknown): Record<string, unknown> =>
  value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
const text = (value: unknown) => typeof value === "string" ? value : "";

export function activityLabel(trace?: AgentTrace) {
  if (!trace) return "";
  if (trace.status === "running") return "Agent 正在判断";
  if (trace.status === "unknown") return "调用结果待核对";
  if (trace.status === "failed") return "Agent 调用失败";
  const result = trace.result ?? {};
  return actions[text(result.action)] || "Agent 已返回方案";
}

export function AgentActivity({ node, traces }: { node: TaskNode; traces: AgentTrace[] }) {
  const records = traces.filter((trace) => trace.node_id === node.id);
  const output = node.output;
  const inventory = node.definition_id === "observe" ? output : {};
  const speech = Array.isArray(inventory.speech) ? inventory.speech.length : 0;
  const texts = Array.isArray(inventory.texts)
    ? inventory.texts.filter((item) => object(item).treatment === "translate").length : 0;
  const hasInventory = Array.isArray(inventory.texts) && Array.isArray(inventory.speech);
  return <div className="agent-activity">
    {hasInventory && <section className="content-diagnosis">
      <span className="eyebrow">按内容清单分类</span>
      <strong>{speech && texts ? "人声 + 画面文字" : speech ? "人声主导" : texts ? "画面文字主导" : "无待译内容"}</strong>
      <p>{text(inventory.summary)}</p>
      <div className="resource-chips"><span>{speech} 段人声</span><span>{texts} 处待译文字</span></div>
      {Array.isArray(inventory.unknowns) && inventory.unknowns.length > 0 &&
        <ErrorBox message={inventory.unknowns.join("；")} />}
    </section>}
    {records.length > 0 && <section aria-label="Agent 实际活动">
      <h3>Agent 活动</h3>
      <ol className="agent-timeline">
        {records.map((trace, index) => {
          const result = trace.status === "succeeded" ? trace.result ?? {} : {};
          const resources = trace.resources.filter((resource) => resource.kind !== "prompts");
          const method = text(result.method);
          const reason = text(result.reason) || text(result.rationale) || text(result.summary);
          return <li key={trace.id}>
            <div className="activity-heading"><strong>{index + 1}. {activityLabel(trace)}</strong><Status value={trace.status} /></div>
            {reason && <p>{reason}</p>}
            {method && <p>选择：{methods[method] || method}</p>}
            {trace.frame_times_ms.length > 0 && <p className="activity-meta">观察 {trace.frame_times_ms.map((value) => `${(value / 1000).toFixed(1)}s`).join(" · ")}</p>}
            <ErrorBox message={trace.error ?? ""} />
            {resources.length > 0 && <details className="activity-resources">
              <summary>提供的技能与知识 · {resources.length}</summary>
              {resources.map((resource) => <div className="resource-reference" key={resource.id}>
                <strong>{resource.name}</strong><span>{resource.version} · {trace.requested_resources.includes(resource.id) ? "Agent 主动查阅" : "随任务提供"}</span>
                <code title={resource.sha256}>{resource.id}</code>
              </div>)}
            </details>}
            <details><summary>判断结果与输入记录</summary>
              <span className="activity-meta">{time(trace.created_at)} · 第 {trace.attempt} 次执行</span>
              <JsonView value={{ result: trace.result, resources: trace.resources, frame_times_ms: trace.frame_times_ms }} />
            </details>
          </li>;
        })}
      </ol>
    </section>}
  </div>;
}
