import type { AgentTrace, Artifact, ProductionDecision, ProductionSession, Task, TaskNode } from "../lib/types";
import { time } from "../lib/types";
import { Empty, ErrorBox, JsonView, Status } from "./common";
import { Select } from "./Select";

const methodNames: Record<string, string> = { local_surface: "逐帧局部补底", flat: "均匀背景修复", donor: "同源帧补底", image_edit: "图片局部编辑", wan: "视频局部编辑" };
const actionNames: Record<string, string> = { confirm_scope: "确认已接受区间的范围复核", inspect_archive: "检查历史结果", rebase_candidate: "本地重合成", defer: "暂缓该实例", inspect_pipeline: "诊断生成链路", reject_candidate: "拒绝候选", restore_candidate: "复用已有候选", inspect: "补充观察", revise_scope: "修订文字范围", repair: "局部修复", accept: "接受候选", finish: "完成清底", blocked: "等待处理", revise: "局部修订" };
const record = (value: unknown): Record<string, unknown> => value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
const records = (value: unknown) => Array.isArray(value) ? value.map(record) : [];
const text = (value: unknown) => typeof value === "string" ? value : typeof value === "number" ? String(value) : "";
const evidence = (value: unknown) => Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
const seconds = (value: unknown) => typeof value === "number" ? `${(value / 1000).toFixed(2)}s` : "—";
function EvidenceList({ value }: { value: unknown }) {
  const items = evidence(value);
  return items.length ? <ul>{items.map((item, index) => <li key={index}>{item}</li>)}</ul> : null;
}
function ArtifactLink({ id, artifacts }: { id: unknown; artifacts: Artifact[] }) {
  const artifact = artifacts.find((item) => item.id === id);
  return artifact ? <a href={artifact.content_url} target="_blank" rel="noreferrer">{artifact.name}</a> : <span>产物 {text(id) || "尚未登记"}</span>;
}
function AcceptedRepairs({ items, artifacts }: { items: Record<string, unknown>[]; artifacts: Artifact[] }) {
  return <>{items.map((item, index) => <article className="evidence-card" key={text(item.id) || index}>
    <div className="evidence-card-header"><strong>{text(item.source_id) || "文字实例"}</strong><span>{methodNames[text(item.method)] || text(item.method)}</span></div>
    <span className="evidence-timing">{seconds(item.start_ms)}–{seconds(item.end_ms)}</span>
    {item.method === "wan" && item.context_start_ms !== undefined && <span className="evidence-timing">
      模型上下文 {seconds(item.context_start_ms)}–{seconds(item.context_end_ms)} · 局部裁切
    </span>}
    <ArtifactLink id={item.id} artifacts={artifacts} /><EvidenceList value={item.evidence} />
  </article>)}</>;
}

export function ProductionSummary({ task }: { task: Task }) {
  const clean = (task.nodes ?? []).filter((node) => node.definition_id === "clean");
  if (!clean.length) return null;
  const ready = clean.filter((node) => node.status === "succeeded").length;
  const skipped = clean.filter((node) => node.status === "skipped").length;
  const decisions = task.production_decisions ?? [];
  return <section className="production-summary" aria-label="制作摘要">
    <div className="production-summary-facts">
      <strong>共用无字底片 {ready} / {clean.length}</strong>
      {skipped > 0 && <span>{skipped} 个原片无需清底</span>}
      <span>图片 {task.max_image_calls === null ? "不限次" : `≤ ${task.max_image_calls ?? 0} 次`} · 视频 {task.max_video_credits === null ? "不限额" : `≤ ${task.max_video_credits ?? 0} credits`}</span>
      {decisions.length > 0 && <span>制作决策 {decisions.filter((item) => item.status === "succeeded").length} / {decisions.length} 已完成</span>}
    </div>
  </section>;
}

export function ProductionEvidence({ node, artifacts, sessions = [], onShowProcess }: {
  node: TaskNode; artifacts: Artifact[]; sessions?: ProductionSession[]; onShowProcess?: () => void;
}) {
  const output = node.output;
  const design = records(output.lettering);
  const accepted = records(output.accepted);
  const layers = records(output.layer_artifacts);
  const spoken = record(output.spoken_texts);
  const revisions = records(output.revisions);
  const nodeSessions = sessions.filter((item) => item.node_id === node.id);
  return <div className="production-evidence">
    {design.length > 0 && <section><h3>{node.definition_id === "visual_analyze" ? "原片文字设计" : "字层适配计划"}</h3>{design.map((item, index) => <article className="evidence-card" key={text(item.source_id) || index}>
      <strong>{text(item.source_id)}</strong>
      {text(item.appearance) && <p>字样：{text(item.appearance)}</p>}
      {text(item.motion_description) && <p>运动：{text(item.motion_description)}</p>}
      {text(item.background_description) && <p>背景：{text(item.background_description)}</p>}
      {text(item.rationale) && <p>{text(item.rationale)}</p>}
      {item.needs_occlusion === true && <p>涉及前景遮挡</p>}
      {text(item.uncertainty) && <p>待核对：{text(item.uncertainty)}</p>}
      <details><summary>字体与轨迹参数</summary><JsonView value={{ style: item.style, motion: item.motion, speech_id: item.speech_id }} /></details>
    </article>)}</section>}
    {accepted.length > 0 && <section><h3>已接受的局部清底</h3><AcceptedRepairs items={accepted} artifacts={artifacts} /></section>}
    {evidence(output.evidence).length > 0 && <section><h3>清底完成证据</h3><EvidenceList value={output.evidence} /></section>}
    {layers.length > 0 && <section><h3>独立译文字层</h3>{layers.map((layer, index) => <article className="evidence-card" key={text(layer.artifact_id) || index}>
      <strong>{text(layer.source_id)}</strong><p><ArtifactLink id={layer.artifact_id} artifacts={artifacts} /></p>
    </article>)}</section>}
    {evidence(output.visual_evidence).length > 0 && <section><h3>画面对照证据</h3><EvidenceList value={output.visual_evidence} /></section>}
    {Object.keys(spoken).length > 0 && <section><h3>{node.status === "succeeded" ? "最终实际口播" : "已记录口播（节点尚未完成）"}</h3><dl className="evidence-spoken">{Object.entries(spoken).map(([id, value]) => <div key={id}><dt>{id}</dt><dd>{text(value)}</dd></div>)}</dl></section>}
    {revisions.length > 0 && <section><h3>口播局部修订</h3>{revisions.map((revision, index) => <article className="evidence-card" key={text(revision.decision_id) || index}>
      <strong>{text(revision.source_id)} · 修订 {index + 1}</strong>
      {text(record(revision.inputs).spoken_text) && <p>修订前：{text(record(revision.inputs).spoken_text)}</p>}
      <p>实际口播：{text(revision.text)}</p><p>{text(revision.reason)}</p>
    </article>)}</section>}
    {nodeSessions.length > 0 && <details className="node-activity-box production-session-box"><summary>局部制作状态</summary><div className="production-session-scroll" tabIndex={0}>{nodeSessions.map((session) => <article className="evidence-card" key={session.id}>
      <div className="production-session-summary">
      <div className="evidence-card-header"><strong>{session.finished ? "本轮制作已完成" : "本轮制作未完成"}</strong><span>已执行 {session.turn} 轮</span></div>
      <p>已接受 {session.accepted.length} 段局部修复{session.candidate ? " · 有候选等待审阅结果" : ""}。接受数量不代表完整解决的文字数量。</p>
      {Boolean(record(session).progress) && <p>
        记录覆盖 {text(record(record(session).progress).coverage_percent)}%
        {" · "}仍有未完成区间的文字 {text(record(record(session).progress).unresolved_instances)} 项
        {" · "}覆盖记录不等于画面验收
      </p>}
      <EvidenceList value={session.evidence} />
      <p>{record(session).progress
        ? "以下为当前尚未完成或尚未验证的区间，不是已通过修复的轻微瑕疵清单。"
        : "当前剩余区间尚未计算，历史暂缓记录不能作为当前未完成清单。"}</p>
      </div>
      {Boolean(record(session).progress) && <>
        {records(record(record(session).progress).remaining).map((item) => {
          const id = text(item.source_id);
          const partial = records(session.accepted).some((repair) => repair.source_id === id);
          const intervals = Array.isArray(item.intervals_ms) ? item.intervals_ms : [];
          return <details key={id}>
            <summary>{partial ? "部分完成，仍有缺口" : "尚未完成"} · {id}</summary>
            <p>剩余区间：{intervals.map((interval) => Array.isArray(interval)
              ? `${seconds(interval[0])}–${seconds(interval[1])}` : "").join("、")}</p>
            {text(item.obstacle) && <details><summary>历史暂缓原因（可能已过时）</summary>
              <p>这是当时记录的原因；其中余额、能力和已处理范围不代表当前状态。</p>
              <p>{text(item.obstacle)}</p>
            </details>}
          </details>;
        })}
      </>}
    </article>)}</div>{onShowProcess && <button className="text-button node-record-link" onClick={onShowProcess}>查看本节点制作过程</button>}</details>}
    {node.error && node.definition_id === "clean" &&
      <details className="production-diagnostics"><summary>诊断详情</summary><div className="node-detail-content"><p>{node.error}</p></div></details>}
  </div>;
}

export function ProductionProcess({ task, nodeId, onNodeChange }: {
  task: Task; nodeId: string; onNodeChange: (id: string) => void;
}) {
  const groups = (task.nodes ?? []).map(node => {
    const decisions = (task.production_decisions ?? []).filter(item => item.node_id === node.id);
    const sessions = (task.production_sessions ?? []).filter(item => item.node_id === node.id);
    const count = decisions.length || sessions.reduce((total, session) => total + (session.history?.length ?? 0), 0);
    return { node, decisions, sessions, count };
  }).filter(group => group.count > 0);
  const count = groups.reduce((total, group) => total + group.count, 0);
  const visible = groups.filter(group => !nodeId || group.node.id === nodeId);
  return <details className="task-drawer production-process" id="task-production-process"><summary>制作过程（{count}）</summary>
    <div className="task-drawer-body">
      <div className="task-record-toolbar">
        <label>节点<Select aria-label="制作过程节点" value={nodeId} onChange={event => onNodeChange(event.target.value)}>
          <option value="">全部节点</option>
          {(task.nodes ?? []).map(node => <option key={node.id} value={node.id}>
            {node.label.replace(/^[①②③④⑤⑥⑦⑧⑨⑩]\s*/, "")} · {node.locale ?? "原片共享"}
          </option>)}
        </Select></label>
        <span>{visible.reduce((total, group) => total + group.count, 0)} 条记录</span>
      </div>
      <div className="task-record-scroll" tabIndex={0} role="region" aria-label="制作过程记录">
      {visible.map(({ node, decisions, sessions }) => <section className="production-evidence" key={node.id}>
        <h3>{node.label} · {node.locale ?? "原片共享"}{task.assets?.find(asset => asset.id === node.asset_id)?.name
          ? ` · ${task.assets.find(asset => asset.id === node.asset_id)!.name}` : ""}</h3>
        <ProductionHistory node={node} nodeDecisions={decisions} nodeSessions={sessions} traces={task.agent_traces ?? []} />
      </section>)}
      {!visible.length && <Empty title="尚无制作过程">节点执行制作决策后会在这里按轮记录。</Empty>}
      </div>
    </div>
  </details>;
}

function ProductionHistory({ node, nodeDecisions, nodeSessions, traces }: {
  node: TaskNode; nodeDecisions: ProductionDecision[]; nodeSessions: ProductionSession[]; traces: AgentTrace[];
}) {
  return <>
      {nodeDecisions.length > 0 ? nodeDecisions.map((decision) => {
        const trace = traces.find(trace => trace.node_id === node.id && trace.decision_id && decision.id.startsWith(trace.decision_id));
        const turn = /^clean-(\d+)$/.exec(decision.key ?? "");
        const history = turn ? nodeSessions.flatMap(session => session.history ?? []).find(item => item.turn === Number(turn[1])) : undefined;
        return <article className="evidence-card" key={decision.id}>
          <div className="evidence-card-header"><strong>{turn ? `第 ${Number(turn[1]) + 1} 轮` : decision.key ?? decision.stage}</strong><Status value={decision.status} /></div>
          <span className="evidence-timing">{time(decision.created_at)}</span>
          {decision.result && <><p>{actionNames[text(decision.result.action)] || text(decision.result.action)}{text(decision.result.reason) ? ` · ${text(decision.result.reason)}` : ""}</p><EvidenceList value={decision.result.evidence} /></>}
          {Boolean(decision.result?.quality) && (() => {
            const quality = record(decision.result?.quality);
            const score = Number(quality.text_removal ?? 0) + Number(quality.subject_preservation ?? 0) + Number(quality.temporal_consistency ?? 0);
            return <p>质量评分 {score} / 100 · 通过线 80
              {evidence(quality.hard_failures).length ? " · 存在硬伤" : ""}
              {evidence(quality.limitations).length > 0 && <span> · {evidence(quality.limitations).join("；")}</span>}
            </p>;
          })()}
          <ErrorBox message={history?.tool_error ?? ""} />
          {trace && <details><summary>观察与技能</summary>
            {trace.frame_times_ms.length > 0 && <p>观察时间：{[...new Set(trace.frame_times_ms)].map(seconds).join(" · ")}</p>}
            {trace.resources.filter(resource => resource.kind !== "prompts").map(resource => <p key={resource.id}>
              {resource.name} · {trace.requested_resources.includes(resource.id) ? "主动查阅" : "随任务提供"}
            </p>)}
            <ErrorBox message={trace.error ?? ""} />
          </details>}
          <details><summary>原始记录</summary><JsonView value={{ decision, trace }} /></details>
        </article>;
      }) : nodeSessions.flatMap(session => (session.history ?? []).map(item => <article className="evidence-card" key={`${session.id}-${item.turn}`}>
        <strong>第 {item.turn + 1} 轮 · {actionNames[text(item.decision.action)] || text(item.decision.action)}</strong>
        <p>{text(item.decision.reason)}</p><EvidenceList value={item.decision.evidence} /><ErrorBox message={item.tool_error ?? ""} />
      </article>))}
  </>;
}
