import { useRef, useState } from "react";
import type { Task, TaskNode } from "../lib/types";
import { api } from "../lib/api";
import { ErrorBox } from "./common";
import { Select } from "./Select";
import "./NodeConversation.css";

const states: Record<string, string> = {
  queued: "消息已排队", running: "Agent 正在回复",
  succeeded: "已回复", failed: "回复失败", unknown: "结果未知，未自动重发",
};

export function NodeConversation({ task, node, refresh }: {
  task: Task; node: TaskNode; refresh: () => void;
}) {
  const [text, setText] = useState("");
  const [scope, setScope] = useState("node");
  const [pause, setPause] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const inFlight = useRef(false);
  const submission = useRef<{ body: string; id: string } | null>(null);
  const terminal = ["cancelled", "completed"].includes(task.status);
  const messages = (task.conversations ?? []).filter(m => m.node_id === node.id);
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
  const send = async () => {
    const body = JSON.stringify({ text: text.trim(), scope, pause_after_current: !terminal && pause });
    if (submission.current?.body !== body) submission.current = { body, id: crypto.randomUUID() };
    await api(`/tasks/${task.id}/nodes/${node.id}/messages`, {
      method: "POST", body: JSON.stringify({ ...JSON.parse(body), request_id: submission.current.id }),
    });
    submission.current = null; setText("");
  };
  return <section className="node-conversation" aria-label="节点对话">
    <h3>节点制作沟通</h3>
    <ol className="conversation-list" aria-live="polite">
      {messages.map(message => <li key={message.id}>
        <div className="conversation-user"><strong>你</strong><p>{message.text}</p></div>
        <div className="conversation-agent">
          <span className="muted">{states[message.status] ?? message.status}</span>
          {message.reply && <>
            <p>{message.reply.reply}</p>
            {message.reply.question && <p className="conversation-question">{message.reply.question}</p>}
            {message.reply.proposed_instruction && <div className="conversation-proposal">
              <strong>{message.scope === "node" ? "本节点建议" : "本节点及后续建议"}</strong>
              <p>{message.reply.proposed_instruction}</p>
              {message.applied_revision != null ? <span>已纳入要求 v{message.applied_revision} · 后续执行生效</span>
                : !terminal && <button className="secondary" disabled={busy || !task.control_paused ||
                    task.nodes?.some(n => n.status === "running")}
                  onClick={() => void run(async () => {
                    await api(`/tasks/${task.id}/messages/${message.id}/apply`, { method: "POST" });
                  })}>应用建议</button>}
            </div>}
          </>}
          {message.error && <ErrorBox message={message.error} />}
        </div>
      </li>)}
    </ol>
    <form className="form-stack" onSubmit={event => { event.preventDefault(); if (text.trim()) void run(send); }}>
      <textarea aria-label="发给节点Agent的消息" rows={3} maxLength={4000} value={text}
        placeholder="例如：00:03 的品牌名保留英文，配音语气再自然一些。"
        onChange={event => setText(event.target.value)} disabled={busy} required />
      <Select aria-label="建议作用范围" value={scope} onChange={event => setScope(event.target.value)} disabled={busy}>
        <option value="node">只讨论本节点</option>
        <option value="downstream">本节点及后续步骤</option>
      </Select>
      {!terminal && <label className="conversation-pause"><input type="checkbox" checked={pause}
        onChange={event => setPause(event.target.checked)} disabled={busy} />当前步骤结束后暂停制作</label>}
      <button className="primary" disabled={busy || !text.trim()}>{busy ? "提交中…" : "发送消息"}</button>
    </form>
    <ErrorBox message={error} />
  </section>;
}
