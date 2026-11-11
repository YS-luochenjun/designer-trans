import { ShieldCheck } from "lucide-react";
import { useData } from "../lib/api";
import { actionNames, eventNames, useManagerCommand } from "../lib/management";
import type { ManagerEvent, ManagerState } from "../lib/management";
import { time } from "../lib/types";
import { Empty, ErrorBox, Loading, Status } from "./common";
import "../pages/Management.css";

function EventStatus({ event }: { event: ManagerEvent }) {
  const processing: Record<string, string> = {
    succeeded: "本轮已处理", failed: "本轮处理失败", blocked: "本轮处理受阻",
    queued: "等待处理", running: "正在处理", unknown: "本轮结果待核对", cancelled: "本轮已取消",
  };
  return <span className={`status status-${event.status}`}><i />{processing[event.status] ?? event.status}</span>;
}

export function ManagementEvent({ event }: { event: ManagerEvent }) {
  return <article className="manager-entry">
    <div className="manager-entry-meta"><strong>{eventNames[event.kind] ?? event.kind}</strong>
      <span>{time(event.created_at)}</span><EventStatus event={event} /></div>
    {event.kind === "requirement_found" && event.payload.title && <strong>{event.payload.title}</strong>}
    {(event.payload.text || event.payload.answer) &&
      <p className="manager-user">{event.payload.text || `我的回答：${event.payload.answer}`}</p>}
    {event.reply && <p className="manager-reply">{event.reply}</p>}
    {event.status === "queued" && <p className="muted">已排队，等待管家处理。</p>}
    {event.status === "running" && <p className="muted">正在核对项目状态…</p>}
    {event.error && <p className="manager-error">{event.error}</p>}
  </article>;
}

export function ManagementAudit() {
  const { data, loading, error, reload } = useData<ManagerState>("/manager", 5000);
  return <section className="panel manager-records manager-audit" aria-label="管理执行审计">
    <h2>管理执行审计</h2>
    <ErrorBox message={error} />
    {error && !data && <button className="secondary" onClick={reload}>重新读取</button>}
    {loading && !data ? <Loading /> : data && <>
      <p className="muted">今日调用 {data.usage.calls_today} / {data.settings.max_calls_per_day} · 累计 {data.usage.total_calls} 次 · 最近记录</p>
      <div className="manager-scroll" tabIndex={0} role="region" aria-label="管理执行记录">
        {!data.actions.length && !data.operations.length && <Empty title="暂无执行记录" />}
        {data.actions.map(a => <article className="manager-todo" key={a.id}>
          <div className="manager-entry-meta"><strong>{actionNames[a.kind] ?? a.kind}</strong><Status value={a.status} /><small>{time(a.created_at)}</small></div>
          <p>{a.reason}</p>{a.error && <p className="manager-error">{a.error}</p>}
          {a.kind.endsWith("_task") && <a href={`#tasks/${a.target_id}`}>查看对应任务</a>}
          {a.result?.task_id && a.result.task_id !== a.target_id && <a href={`#tasks/${a.result.task_id}`}>查看创建的任务</a>}
        </article>)}
        {data.operations.map(o => <article className="manager-todo" key={o.id}>
          <div className="manager-entry-meta"><strong>管理模型调用</strong><Status value={o.status} /><small>{time(o.created_at)}</small></div>
          <p>{o.model} · {o.engine}</p>{o.error && <p className="manager-error">{o.error}</p>}
        </article>)}
      </div>
      <details className="manager-records">
        <summary>管理事件原始记录 · {data.events.length} 条最近记录</summary>
        <div className="manager-scroll" tabIndex={0} role="region" aria-label="管理事件原始记录">
          {data.events.map(e => <ManagementEvent key={e.id} event={e} />)}
        </div>
      </details>
    </>}
  </section>;
}

export function AutomationActivity() {
  const { data, loading, error, reload } = useData<ManagerState>("/manager", 5000);
  const { busy, failure, perform } = useManagerCommand(reload);
  // Scheduled rounds have their own complete record on each plan.
  const events = data?.events.filter(e => ["patrol", "task_attention", "task_stalled"].includes(e.kind) &&
    !e.payload.schedule_id) ?? [];
  return <section className="panel manager-records" aria-label="后台巡检">
    <div className="manager-section-head"><h2>后台巡检</h2>
      <button className="secondary" disabled={busy} onClick={() => void perform("/manager/patrol")}><ShieldCheck size={16} />巡检一次</button>
    </div>
    <ErrorBox message={error || failure || data?.error || ""} />
    {error && !data && <button className="secondary" onClick={reload}>重新读取</button>}
    {loading && !data ? <Loading /> : data && <details>
      <summary>最近巡检记录 · {events.length} 条</summary>
      <div className="manager-scroll" tabIndex={0} role="region" aria-label="最近巡检动态">
        {!events.length && <Empty title="暂无巡检记录" />}
        {events.map(event => <article className="manager-entry" key={event.id}>
          {event.payload.task_id ? <>
            <div className="manager-entry-meta"><strong>{event.payload.title || eventNames[event.kind]}</strong>
              <span>{time(event.created_at)}</span><EventStatus event={event} /></div>
            <a href={`#tasks/${event.payload.task_id}`}>查看任务巡检分析</a>
          </> : <ManagementEvent event={event} />}
        </article>)}
      </div>
    </details>}
  </section>;
}

export function TaskManagementContext({ taskId }: { taskId: string }) {
  const { data, loading, error, reload } = useData<ManagerState>("/manager", 5000);
  const { busy, failure, perform } = useManagerCommand(reload);
  const incidents = data?.incidents.filter(i => i.target_id === taskId && i.kind.startsWith("task_") && i.status === "open") ?? [];
  const events = data?.events.filter(e => e.payload.task_id === taskId || e.payload.origin?.task_id === taskId) ?? [];
  const historical = events.filter(e => !incidents.some(i => e.id === i.id || e.payload.root_event_id === i.id));
  if (!loading && !error && !incidents.length && !events.length) return null;
  return <details className="panel manager-records" aria-label="本任务巡检分析">
    <summary>巡检分析{data ? ` · ${incidents.length} 项待跟进` : ""}</summary>
    <ErrorBox message={error || failure} />
    {error && !data && <button className="secondary" onClick={reload}>重新读取</button>}
    {loading && !data ? <Loading /> : <div className="manager-scroll" tabIndex={0} role="region" aria-label="本任务当前巡检事项">
      {!incidents.length && <p className="muted">暂无待跟进的巡检事项。</p>}
      {incidents.map(incident => {
        const latest = data?.events.find(e => e.payload.root_event_id === incident.id || e.id === incident.id);
        return <article className="manager-issue" key={incident.id}>
          <div className="manager-entry-meta"><strong>{incident.kind === "task_stalled" ? "进度需要核对" : "节点需要处理"}</strong><Status value="open" /></div>
          {latest ? <ManagementEvent event={latest} /> : <p className="muted">本轮分析尚未读取，历史回复不代表当前结论。</p>}
          <div className="actions wrap">
            <a href="#overview">前往首页讨论或确认管理建议</a>
            <button className="text-button" disabled={busy} onClick={() => void perform(`/manager/incidents/${incident.id}/dismiss`)}>本次不跟进</button>
          </div>
        </article>;
      })}
      {!!historical.length && <details><summary>历史分析 · {historical.length} 条</summary>
        {historical.map(e => <ManagementEvent key={e.id} event={e} />)}
      </details>}
    </div>}
  </details>;
}

export function RequirementHistory() {
  const { data, loading, error, reload } = useData<ManagerState>("/manager");
  const discovery = data?.discovery_history ?? data?.events.filter(e => e.kind === "requirement_found") ?? [];
  const checks = data?.events.filter(e => e.kind === "requirements_check" && !e.payload.schedule_id) ?? [];
  return <details className="panel manager-records" aria-label="需求检查历史">
    <summary>需求检查历史</summary>
    <ErrorBox message={error} />
    {error && !data && <button className="secondary" onClick={reload}>重新读取</button>}
    {loading && !data ? <Loading /> : <div className="manager-scroll" tabIndex={0} role="region" aria-label="需求检查记录">
      {!discovery.length && !checks.length && <Empty title="暂无需求检查记录" />}
      {checks.map(e => <ManagementEvent key={e.id} event={e} />)}
      {!!discovery.length && <details><summary>旧版需求发现记录 · {data?.discovery_event_total ?? discovery.length} 条</summary>
        {discovery.map(e => <ManagementEvent key={e.id} event={e} />)}
      </details>}
    </div>}
  </details>;
}
