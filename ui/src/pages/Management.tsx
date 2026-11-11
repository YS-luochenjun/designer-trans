import { useEffect, useRef, useState } from "react";
import { MessageSquare, Send } from "lucide-react";
import { useData } from "../lib/api";
import { actionNames, useManagerCommand } from "../lib/management";
import type { ManagerQuestion, ManagerState } from "../lib/management";
import { Empty, ErrorBox, Loading } from "../components/common";
import { ProjectArchitecture } from "../components/ProjectArchitecture";
import { ManagementAudit, ManagementEvent } from "../components/ManagementRecords";
import "./Management.css";

export function DeveloperView({ view }: { view?: string }) {
  const audit = view === "audit";
  return <>
    <div className="tabs" role="tablist" aria-label="开发者内容">
      <button role="tab" aria-selected={!audit} className={!audit ? "active" : ""}
        onClick={() => { location.hash = "resources/developer"; }}>项目架构</button>
      <button role="tab" aria-selected={audit} className={audit ? "active" : ""}
        onClick={() => { location.hash = "resources/developer/audit"; }}>执行审计</button>
    </div>
    {audit ? <ManagementAudit /> : <ProjectArchitecture />}
  </>;
}

export function ProjectSteward() {
  const { data, loading, error, reload } = useData<ManagerState>("/manager", 3000);
  const { busy, failure, perform } = useManagerCommand(reload);
  const [text, setText] = useState("");
  const [submittedAction, setSubmittedAction] = useState<string | null>(null);
  const pending = useRef<{ id: string; text: string } | null>(null);
  const log = useRef<HTMLDivElement>(null);
  const followLatest = useRef(true);
  useEffect(() => {
    if (followLatest.current && log.current) log.current.scrollTop = log.current.scrollHeight;
  }, [data]);

  const send = async () => {
    if (!text.trim() || busy) return;
    if (!pending.current || pending.current.text !== text.trim()) {
      pending.current = { id: crypto.randomUUID(), text: text.trim() };
    }
    followLatest.current = true;
    if (await perform("/manager/messages", { request_id: pending.current.id, text: pending.current.text })) {
      setText(""); pending.current = null;
    }
  };
  const questions = data?.questions.filter(q => q.status === "open") ?? [];
  const actions = data?.actions.filter(a => a.status === "waiting_user") ?? [];
  const feedback = data?.actions.find(a => a.id === submittedAction);
  const feedbackLabels: Record<string, string> = {
    queued: "正在等待执行", running: "正在核对并执行", succeeded: "已执行",
    blocked: "执行受阻", failed: "执行失败", unknown: "结果尚未确认，请先核对",
    superseded: "建议已失效，请重新讨论", rejected: "本次不执行",
  };
  const conversations = (data?.events ?? []).filter(e =>
    e.kind === "message" || (e.kind === "answer" && !e.payload.task_id &&
      !e.payload.origin?.task_id && !e.payload.origin?.schedule_id)).slice().reverse();
  const modelUnavailable = !!data && (!data.settings.agent_enabled ||
    data.usage.calls_today >= data.settings.max_calls_per_day);

  return <>
    <ErrorBox message={error || failure || data?.error || ""} />
    {!loading && !data && <button className="secondary" onClick={reload}>重新读取项目状态</button>}
    {loading && !data ? <Loading /> : data && <>
      {modelUnavailable && <p className="scope-banner" role="status">
        {!data.settings.agent_enabled ? "管家判断已关闭。" : "今日管家调用次数已用完。"}
        可前往<a href="#resources/manager">管家设置</a>调整后重新发送。
      </p>}
      <div className={`manager-layout ${questions.length + actions.length ? "has-decisions" : ""}`}>
        <section className="panel manager-conversation" aria-label="项目管家对话">
          <div className="manager-section-head">
            <h2><MessageSquare size={18} />项目管家</h2>
            <span className="manager-runtime"><i className={`manager-indicator ${data.running ? "online" : ""}`} />
              {data.running ? "在线" : "执行器未运行"}</span>
          </div>
          <div ref={log} className="manager-log" tabIndex={0} role="region" aria-label="项目对话记录"
            onScroll={e => { const el = e.currentTarget; followLatest.current = el.scrollHeight - el.scrollTop - el.clientHeight < 60; }}>
            {!conversations.length && <Empty title="告诉管家你想做什么">
              可以询问进展或讨论下一步，需要你决定的事项会列在旁边。
            </Empty>}
            {conversations.map(event => <ManagementEvent key={event.id} event={event} />)}
          </div>
          <form className="manager-compose" onSubmit={e => { e.preventDefault(); void send(); }}>
            <label htmlFor="manager-message">向项目管家提问</label>
            <textarea id="manager-message" rows={3} maxLength={4000} value={text} onChange={e => setText(e.target.value)}
              placeholder="例如：检查项目进展，说明有哪些事情需要我决定。" />
            <div className="manager-compose-footer"><button className="primary" disabled={busy || !text.trim() || modelUnavailable}><Send size={15} />发送</button></div>
          </form>
          <details className="manager-purpose">
            <summary>管家能做什么</summary>
            <p>理解目标、核对进展、提出管理建议。制作与验收进入任务中心，轮询计划进入自动化。</p>
            <p>目前支持建议暂停、恢复、重试或刷新需求。自动创建轮询计划、修改代码和维护 Agent 尚未接入。</p>
          </details>
        </section>
        <aside className="manager-todos" aria-label="需要你决定">
          <section className="panel" id="manager-decisions">
            <div className="manager-section-head"><h2>待决定事项</h2><span>{questions.length + actions.length}</span></div>
            {feedback && <div className="manager-decision-feedback" role="status">
              <p>{actionNames[feedback.kind] ?? feedback.kind}：{feedbackLabels[feedback.status] ?? "正在读取执行状态"}</p>
              {feedback.error && <p className="manager-error">{feedback.error}</p>}
              {feedback.kind.endsWith("_task") && <a href={`#tasks/${feedback.target_id}`}>查看对应任务</a>}
            </div>}
            <div className="manager-scroll" tabIndex={0} role="region" aria-label="管理待决定事项">
              {!questions.length && !actions.length && <p className="muted">当前没有需要你决定的事项。</p>}
              {questions.map(q => {
                const origin = data.events.find(e => e.id === (q.scope_key ?? q.event_id));
                const taskId = q.task_id || origin?.payload.task_id || origin?.payload.origin?.task_id;
                return <QuestionForm key={q.id} question={q} taskId={taskId} disabled={busy}
                  onAnswer={answer => perform(`/manager/questions/${q.id}/answer`, { answer })} />;
              })}
              {actions.map(a => <article className="manager-todo" key={a.id}>
                <strong>{actionNames[a.kind] ?? a.kind}</strong><p>{a.reason}</p>
                {a.kind.endsWith("_task") && <a href={`#tasks/${a.target_id}`}>查看对应任务</a>}
                {a.kind === "sync_requirements" && <a href="#fireplay">查看需求中心</a>}
                <div className="actions wrap">
                  <button className="primary" disabled={busy} onClick={async () => {
                    if (await perform(`/manager/actions/${a.id}/authorize`, { approve: true }))
                      setSubmittedAction(a.id);
                  }}>确认执行</button>
                  <button className="secondary" disabled={busy} onClick={async () => {
                    if (await perform(`/manager/actions/${a.id}/authorize`, { approve: false }))
                      setSubmittedAction(a.id);
                  }}>不执行</button>
                </div>
              </article>)}
            </div>
          </section>
        </aside>
      </div>
    </>}
  </>;
}

function QuestionForm({ question, taskId, disabled, onAnswer }: {
  question: ManagerQuestion; taskId?: string; disabled: boolean; onAnswer: (answer: string) => Promise<boolean>;
}) {
  const [answer, setAnswer] = useState("");
  return <form className="manager-todo form-stack" onSubmit={e => {
    e.preventDefault();
    if (answer.trim()) void onAnswer(answer.trim()).then(ok => { if (ok) setAnswer(""); });
  }}>
    <label>{question.prompt}<textarea aria-label={`回答：${question.prompt}`} rows={2} maxLength={4000}
      required value={answer} onChange={e => setAnswer(e.target.value)} disabled={disabled} /></label>
    {taskId && <a href={`#tasks/${taskId}`}>查看对应任务</a>}
    <button className="secondary" disabled={disabled || !answer.trim()}>提交回答并继续跟进</button>
  </form>;
}
