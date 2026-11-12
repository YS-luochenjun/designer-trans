import { useEffect, useRef, useState } from "react";
import { ArrowLeft, Check, Download, RotateCcw, Square, X } from "lucide-react";
import { api, useData } from "../lib/api";
import type { Artifact, Task, Question } from "../lib/types";
import { time } from "../lib/types";
import {
  ErrorBox,
  JsonView,
  Loading,
  PageHead,
  Status,
} from "../components/common";
import { ProductionEvidence, ProductionProcess, ProductionSummary } from "../components/ProductionEvidence";
import { WorkflowGraph } from "../components/WorkflowGraph";
import { AgentActivity } from "../components/AgentActivity";
import { TaskDeletion, readPendingDeletion } from "../components/TaskDeletion";
import type { PendingDeletion } from "../components/TaskDeletion";
import { NodeConversation } from "../components/NodeConversation";
import { RouteReport } from "../components/RouteReport";
import { NodeOverview } from "../components/NodeOverview";
import { LocalPreview } from "../components/LocalPreview";
import { CleanPlate } from "../components/CleanPlate";
import { TaskCallRecords } from "../components/TaskCallRecords";
import { TaskManagementContext } from "../components/ManagementRecords";

function Media({ artifact }: { artifact: Artifact }) {
  return (
    <div className="media-artifact">
      {artifact.kind === "video" ? (
        <video controls preload="metadata" src={artifact.content_url} />
      ) : artifact.kind === "image" ? (
        <img src={artifact.content_url} alt={artifact.name} loading="lazy" />
      ) : artifact.kind === "audio" ? (
        <audio controls preload="metadata" src={artifact.content_url} />
      ) : null}
      <a href={`${artifact.content_url}?download=true`} download>
        <Download size={14} />
        {artifact.name}
      </a>
    </div>
  );
}
function QuestionCard({
  taskId,
  question,
  refresh,
}: {
  taskId: string;
  question: Question;
  refresh: () => void;
}) {
  const [answer, setAnswer] = useState(""),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const [reviewIssues, setReviewIssues] = useState<string[]>([]);
  const send = async (value: string) => {
    setBusy(true);
    setError("");
    try {
      await api(`/tasks/${taskId}/questions/${question.id}/answer`, {
        method: "POST",
        body: JSON.stringify({
          answer: value,
          feedback: question.kind === "review" ? [...reviewIssues, answer].filter(Boolean).join("；") : "",
        }),
      });
      refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : "提交失败");
    } finally {
      setBusy(false);
    }
  };
  return (
    <article className="question">
      <div className="eyebrow">Route 请你确认</div>
      <p>{question.prompt}</p>
      {question.kind === "brief" && question.context.inventory && <details>
        <summary>原片内容清单</summary><JsonView value={question.context.inventory} />
      </details>}
      {question.kind === "review" ? (
        <>
          <p className="muted">
            请完整观看原片与候选后再批准。批准仅适用于当前成片版本。
          </p>
          <label>
            哪些地方需要返工（可多选）
            <div className="actions">{["清底仍有原文", "译文不准确", "字号或动效不合适", "声音有问题"].map(issue =>
              <button key={issue} type="button" className="secondary" aria-pressed={reviewIssues.includes(issue)}
                onClick={() => setReviewIssues(current => current.includes(issue)
                  ? current.filter(x => x !== issue) : [...current, issue])}>{issue}</button>)}</div>
          </label>
          <label>
            其他说明（可选）
            <textarea
              value={answer}
              onChange={(event) => setAnswer(event.target.value)}
              maxLength={4000}
              placeholder="可补充时间点。提交不通过后，Route 会在原额度内安排返工。"
            />
          </label>
          <div className="actions">
            <button
              className="primary"
              disabled={busy}
              onClick={() => void send("approve")}
            >
              <Check size={16} />
              已完整验收，允许交付
            </button>
            <button
              className="secondary"
              disabled={busy}
              onClick={() => void send("reject")}
            >
              不通过，交给 Route 返工
            </button>
          </div>
        </>
      ) : question.options.length > 0 ? (
        <div className="actions">{question.options.map(option =>
          <button key={option} className="secondary" disabled={busy}
            onClick={() => void send(option)}>{option}</button>)}</div>
      ) : (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void send(answer || (question.kind === "brief" ? "按当前制作要求继续" : ""));
          }}
        >
          <label>
            补充说明
            <textarea
              value={answer}
              onChange={(e) => setAnswer(e.target.value)}
              required={question.kind !== "brief"}
              placeholder={question.kind === "brief" ? "可补充品牌用语、保留内容和声音要求；留空按现有要求继续。" : ""}
            />
          </label>
          <button className="primary" disabled={busy}>
            {question.kind === "brief" ? "确认要求，继续制作" : "提交回答"}
          </button>
        </form>
      )}
      <ErrorBox message={error} />
    </article>
  );
}
export function TaskDetail({ id, onBack }: { id: string; onBack: () => void }) {
  const {
    data: task,
    error,
    loading,
    reload,
  } = useData<Task>(`/tasks/${id}`, 2500);
  const [selected, setSelected] = useState<string | null>(null),
    [actionError, setActionError] = useState(""),
    [busy, setBusy] = useState(false);
  const detailPanel = useRef<HTMLElement>(null);
  const [callNode, setCallNode] = useState("");
  const [processNode, setProcessNode] = useState("");
  const showRecords = (kind: "calls" | "process", nodeId: string) => {
    if (kind === "calls") setCallNode(nodeId);
    else setProcessNode(nodeId);
    const panel = document.getElementById(kind === "calls" ? "task-calls" : "task-production-process") as HTMLDetailsElement | null;
    if (panel) {
      panel.open = true;
      panel.scrollIntoView({ block: "start", behavior: "smooth" });
      panel.querySelector("summary")?.focus({ preventScroll: true });
    }
  };
  useEffect(() => {
    const panel = detailPanel.current;
    if (!selected || !panel) return;
    panel.scrollTop = 0;
    const bounds = panel.getBoundingClientRect();
    if (bounds.top < 0 || bounds.top > window.innerHeight - 100) {
      panel.scrollIntoView({ block: "nearest", behavior: "instant" });
    }
    panel.focus({ preventScroll: true });
  }, [selected]);
  const [deleting, setDeleting] = useState(false);
  const [pendingDeletion, setPendingDeletion] = useState<PendingDeletion | null>(readPendingDeletion);
  const action = async (kind: "cancel" | "retry") => {
    setBusy(true);
    setActionError("");
    try {
      await api(`/tasks/${id}/${kind}`, { method: "POST" });
      reload();
    } catch (e) {
      setActionError(e instanceof Error ? e.message : "操作失败");
    } finally {
      setBusy(false);
    }
  };
  if (loading && !task) return <Loading />;
  if (!task) return <ErrorBox message={error} />;
  const nodes = task.nodes ?? [],
    node = nodes.find((n) => n.id === selected),
    questions = (task.questions ?? []).filter((q) => q.status === "open");
  return (
    <>
      <button className="back-link" onClick={onBack}>
        <ArrowLeft size={15} />
        返回任务中心
      </button>
      <PageHead
        eyebrow=""
        title={task.title}
        action={
          <>
            <Status value={task.status} />
      {!["cancelled", "completed"].includes(task.status) && <>
        {task.control_paused && <span role="status">制作已暂停{nodes.some(n => n.status === "running") ? "，等待当前步骤结束" : ""}</span>}
        <button className="secondary" disabled={busy} onClick={async () => {
          setBusy(true); setActionError("");
          try { await api(`/tasks/${id}/control`, { method: "POST", body: JSON.stringify({ paused: !task.control_paused }) }); reload(); }
          catch (cause) { setActionError(cause instanceof Error ? cause.message : "操作失败"); }
          finally { setBusy(false); }
        }}>{task.control_paused ? "继续制作" : "暂停制作"}</button>
      </>}
            {["blocked", "interrupted", "partial_completed"].includes(
              task.status,
            ) && (
              <button
                className="secondary"
                disabled={busy}
                onClick={() => void action("retry")}
              >
                <RotateCcw size={15} />
                重试失败节点
              </button>
            )}
            {!["completed", "cancelled"].includes(task.status) && (
              <button
                className="secondary danger"
                disabled={busy}
                onClick={() => void action("cancel")}
              >
                <Square size={14} />
                停止任务
              </button>
            )}
            <button className="secondary danger" disabled={!!pendingDeletion && pendingDeletion.task.id !== id}
              title={pendingDeletion && pendingDeletion.task.id !== id ? "请先在任务中心完成上一项删除" : undefined}
              onClick={() => setDeleting(true)}>删除任务</button>
          </>
        }
      />
      <ErrorBox message={error || actionError} />
      {task.execution_snapshot?.budget_mode === "testing_unlimited" &&
        <p className="muted" role="status">测试模式：调用、清底动作与返工不设额度上限，消耗持续记录。</p>}
      <RouteReport report={task.route_report} />
      <TaskManagementContext taskId={id} />
      {Boolean(task.task_route_events?.some(event => ["queued", "running", "planned", "blocked"].includes(event.status))) &&
        <details className="task-drawer"><summary>Route 协调记录</summary>
          {task.task_route_events?.slice(-8).map(event => <p key={event.id}>
            {event.status === "applied" ? "已安排返工" : event.status === "blocked" ? "需要解决阻塞" :
              event.status === "running" ? "正在协调" : event.status === "acknowledged" ? "已收到 Work 报备" : "等待协调"}
            {event.reason || event.plan?.reason ? ` · ${event.reason || event.plan?.reason}` : ""}
          </p>)}
        </details>}

      {questions.length > 0 && <a className="review-shortcut" href="#task-review"
        onClick={(event) => {
          event.preventDefault();
          const panel = document.getElementById("task-review") as HTMLDetailsElement;
          panel.open = true;
          panel.scrollIntoView({ behavior: "smooth", block: "start" });
        }}>{questions.length} 项待确认 · 查看候选与审核</a>}
      {(
        <>
          <div className="flow-layout workflow-layout">
            <WorkflowGraph
              nodes={nodes}
              assets={task.assets}
              traces={task.agent_traces}
              sessions={task.production_sessions}
              selected={selected}
              onSelect={setSelected}
            />
            {node && (
              <aside className="node-detail" ref={detailPanel} tabIndex={-1} aria-label={`${node.label}详情`}>
                <div className="panel-heading">
                  <h2>{node.label.replace(/^[①②③④⑤⑥⑦⑧⑨⑩]\s*/, "")}</h2>
                  <button
                    className="icon-button"
                    aria-label="关闭节点详情"
                    onClick={() => setSelected(null)}
                  >
                    <X size={18} />
                  </button>
                </div>
                <NodeConversation key={node.id} task={task} node={node} refresh={reload} />
                <RouteReport report={node.route_report} />
                <NodeOverview task={task} node={node} onShowCalls={() => showRecords("calls", node.id)}
                  notice={node.error && <details><summary>Work 与程序的原始诊断</summary>
                    <ErrorBox message={node.error} />
                  </details>} />
                <AgentActivity node={node} traces={(task.agent_traces ?? []).filter(trace =>
                  !trace.decision_id || !(task.production_decisions ?? []).some(decision =>
                    decision.node_id === node.id && decision.id.startsWith(trace.decision_id!)))} />


                {node.skip_reason && <p>{node.skip_reason}</p>}
                <ProductionEvidence node={node} artifacts={task.artifacts ?? []}
                  sessions={task.production_sessions} onShowProcess={() => showRecords("process", node.id)} />
                <details className="node-metadata"><summary>调试信息</summary><div className="node-detail-content"><dl>
                  <dt>前置步骤</dt>
                  <dd>{node.depends_on.map((dependency) => nodes.find((item) => item.id === dependency)?.label ?? dependency).join("、") || "无"}</dd>
                  <dt>语言</dt>
                  <dd>{node.locale ?? "原片共享"}</dd>
                  <dt>尝试</dt>
                  <dd>{node.attempt}</dd>
                  <dt>开始</dt>
                  <dd>{time(node.started_at)}</dd>
                  <dt>结束</dt>
                  <dd>{time(node.finished_at)}</dd>
                </dl><JsonView value={node.output} /></div></details>
                <details className="node-artifacts"><summary>实际产物</summary>
                {(task.artifacts ?? [])
                  .filter((a) => a.node_id === node.id)
                  .map((a) => (
                    <Media key={a.id} artifact={a} />
                  ))}
                {!task.artifacts?.some((a) => a.node_id === node.id) && (
                  <p className="muted">尚无产物</p>
                )}
                </details>
              </aside>
            )}
          </div>

        </>
      )}
      <details className="task-drawer" id="task-review">
        <summary>原片与成品 {questions.length > 0 && <span className="status">{questions.length} 项待审核</span>}</summary>
        <div className="task-drawer-body">
          {task.production_policy?.mode === "completion_first" && <section className="evidence-card">
            <strong>旧接续路线已停止使用 · 先完成无字底片</strong>
            <p>原文尚未清理完成时不继续叠加译文。历史草稿保留用于定位问题，不作为合格结果。</p>
          </section>}
          <ProductionSummary task={task} />
          <CleanPlate task={task} />
          <LocalPreview task={task} refresh={reload} />
          <div className="preview-grid">
            {(task.assets ?? []).map((a) => (
              <article key={a.id}>
                <div className="eyebrow">原片</div>
                <video controls src={a.content_url} preload="metadata" />
                <h3>{a.name}</h3>
              </article>
            ))}
            {(task.artifacts ?? [])
              .filter(
                (a) =>
                  a.kind === "video" &&
                  ((a.name.startsWith("未验收草稿") && !(task.artifacts ?? []).some(other =>
                    other.name.startsWith("未验收草稿") && other.node_id === a.node_id &&
                    (other.created_at ?? "") > (a.created_at ?? ""))) || nodes.some(
                    (n) =>
                      n.id === a.node_id &&
                      n.definition_id === "compose" &&
                      n.output.artifact_id === a.id,
                  )),
              )
              .map((a) => (
                <article key={a.id}>
                  <div className="eyebrow">{a.name.startsWith("未验收草稿") ? "未验收草稿 · 有待解决项" : "候选"} · {a.locale}</div>
                  <Media artifact={a} />
                  <small className="hash">SHA256 {a.sha256}</small>
                </article>
              ))}
          </div>
          {questions.map((q) => (
            <QuestionCard
              key={q.id}
              taskId={id}
              question={q}
              refresh={reload}
            />
          ))}
          {task.deliveries?.map((d) => (
            <div className="delivery-line" key={d.id}>
              <Status value={d.status} />
              <strong>{d.locale}</strong>
              <a href={d.content_url} download>
                下载成品 <Download size={14} />
              </a>
            </div>
          ))}
        </div>
      </details>
      <TaskCallRecords task={task} nodeId={callNode} onNodeChange={setCallNode} />
      <ProductionProcess task={task} nodeId={processNode} onNodeChange={setProcessNode} />
      {deleting && <TaskDeletion task={task} pending={pendingDeletion?.task.id === id ? pendingDeletion : null}
        onPending={setPendingDeletion}
        onDeleted={() => { setDeleting(false); setPendingDeletion(null); onBack(); }}
        onClose={() => setDeleting(false)} />}
    </>
  );
}
