import { useRef, useState } from "react";
import type { Task, TaskNode } from "../lib/types";
import { api } from "../lib/api";
import { ErrorBox } from "./common";
import "./NodeConversation.css";

const states: Record<string, string> = {
  queued: "消息已排队", running: "Agent 正在回复",
  succeeded: "已回复", failed: "回复失败", unknown: "结果未知，未自动重发",
};

export function NodeConversation({ task, node, refresh }: {
  task: Task; node: TaskNode; refresh: () => void;
}) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const inFlight = useRef(false);
  const submission = useRef<{ body: string; id: string } | null>(null);
  const terminal = ["cancelled", "completed"].includes(task.status);
  const messages = (task.conversations ?? []).filter(m => m.node_id === node.id);
  const settled = !task.nodes?.some(n => ["running", "waiting_external", "unknown"].includes(n.status));
  const canContinue = node.definition_id === "clean" && task.output_goal === "complete_candidate" &&
    (["failed", "interrupted"].includes(node.status) ||
      node.status === "succeeded" && node.output.quality_passed === false);
  const continuationState = task.control_paused ? "任务已暂停，当前没有继续制作。"
    : node.status === "running" ? "后台正在执行清底制作。"
    : ["pending", "queued"].includes(node.status) ? "清底已重新排队，等待后台领取。"
    : node.status === "waiting_external" ? "等待本次制作的原外部请求返回。"
    : node.status === "succeeded" ? "本次清底执行已完成，请核对当前结果。"
    : node.route_report?.summary || "本次继续尚未完成，问题交给 Route 协调。";
  const run = async (action: () => Promise<void>) => {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true); setError("");
    try { await action(); refresh(); }
    catch (cause) {
      const message = cause instanceof Error ? cause.message : "操作失败";
      setError(message === "Not Found" ? "当前后台未加载节点对话，请重启原工作台服务。" : message);
    } finally { inFlight.current = false; setBusy(false); }
  };
  const send = async (value = text.trim()) => {
    const body = JSON.stringify({ text: value, scope: "downstream", interaction: "route", pause_after_current: false });
    if (submission.current?.body !== body) submission.current = { body, id: crypto.randomUUID() };
    await api(`/tasks/${task.id}/nodes/${node.id}/messages`, {
      method: "POST", body: JSON.stringify({ ...JSON.parse(body), request_id: submission.current.id }),
    });
    submission.current = null; setText("");
  };
  return <section className="node-conversation" aria-label="与 Route 沟通">
    <h3>与 Route 沟通</h3>
    <p className="conversation-execution">
      {task.control_paused ? "任务目前已暂停。" : node.status === "running" ? "Work 正在制作。" : "Route 负责协调本节点及后续制作。"}
      问进度会直接答复；修改要求由 Route 转达，Work 在安全边界接收。发送消息不会暂停制作。
    </p>
    <ol className="conversation-list" aria-live="polite">
      {messages.map(message => <li key={message.id}>
        <div className="conversation-user"><strong>你</strong><p>{message.text}</p></div>
        <div className="conversation-agent">
          <span className="muted">{states[message.status] ?? message.status}</span>
          {message.reply && <>
            <p>{message.reply.reply}</p>
            {message.reply.question && <p className="conversation-question">{message.reply.question}</p>}
            {message.reply.proposed_instruction && <div className="conversation-proposal">
              <strong>{message.interaction === "route" ? "制作方向" : "旧版制作建议"}</strong>
              <p>{message.reply.proposed_instruction}</p>
              {message.direction ? <p role="status">
                {task.control_paused ? "方向已保存；任务暂停中，恢复后交给 Work。" :
                  message.direction.status === "received_by_work" ? "Work 已收到新方向。" :
                    "Route 已转达方向，等待 Work 在安全边界接收。"}
              </p> : message.applied_revision != null ? <span>旧版要求 v{message.applied_revision} 已保存 · 当时未重新安排制作</span>
                : !terminal && message.interaction !== "route" && <button className="secondary" disabled={busy || !task.control_paused ||
                    task.nodes?.some(n => n.status === "running")}
                  onClick={() => void run(async () => {
                    await api(`/tasks/${task.id}/messages/${message.id}/apply`, { method: "POST" });
                  })}>仅保存要求</button>}
              {message.continuation ? <p role="status">{continuationState}</p>
                : !terminal && canContinue && message.interaction !== "route" &&
                  (message.applied_revision == null || message.applied_revision === task.brief_revision) &&
                  <div className="conversation-continue">
                  <p>继续会从原清底会话重新排队，使依赖它的旧译片等待更新。保留已接受补片、历史产物和累计费用，使用原剩余额度。</p>
                  <button className="primary" disabled={busy || !task.control_paused || !settled}
                    onClick={() => void run(async () => {
                      await api(`/tasks/${task.id}/messages/${message.id}/continue-clean`, {
                        method: "POST", body: JSON.stringify({
                          expected_revision: task.brief_revision ?? 0,
                          expected_attempt: node.attempt,
                          expected_status: node.status,
                          expected_output_sha256: typeof node.output.sha256 === "string" ? node.output.sha256 : null,
                        }),
                      });
                    })}>{message.applied_revision != null ? "按此要求继续清底" : "应用并继续清底"}</button>
                </div>}
            </div>}
          </>}
          {message.reply?.action === "clarify" && <div className="actions">
            {message.reply.options?.map(option => <button key={option} className="secondary" disabled={busy}
              onClick={() => void run(() => send(option))}>{option}</button>)}
          </div>}
          {message.direction_error && <ErrorBox message={message.direction_error} />}
          {message.error && <ErrorBox message={message.error} />}
        </div>
      </li>)}
    </ol>
    <form className="form-stack" onSubmit={event => { event.preventDefault(); if (text.trim()) void run(send); }}>
      <textarea aria-label="发给 Route 的消息" rows={3} maxLength={4000} value={text}
        placeholder="问进度或直接说修改方向，例如：先清除残余原文，再继续排版。"
        onChange={event => setText(event.target.value)} disabled={busy} required />
      <button className="primary" disabled={busy || !text.trim()}>{busy ? "提交中…" : "发送给 Route"}</button>
    </form>
    <ErrorBox message={error} />
  </section>;
}
