import type { TaskRouteEvent } from "../lib/types";

const labels: Record<string, string> = {
  queued: "Route 已排队", running: "Route 正在协调", planned: "Route 已形成安排",
  waiting_work: "等待 Work 到达安全边界", waiting_reconciliation: "技术 Route 正在核对原请求",
  applied: "Work 已重新排队", forwarded: "方向已交回工作队列",
  acknowledged: "Route 已收到 Work 报备", blocked: "本次协调遇到阻塞",
  unknown: "协调结果待核对", superseded: "已由新依据接续",
};

export function RouteCoordination({ events = [] }: { events?: TaskRouteEvent[] }) {
  if (!events.length) return null;
  return <details className="task-drawer">
    <summary>Route 协调记录</summary>
    <div className="task-drawer-body">
      {events.slice(-8).reverse().map(event => <article key={event.id}>
        <p><strong>{labels[event.status] ?? "协调记录已保留"}</strong>
          {event.status === "waiting_reconciliation" && " · 方向已保留，核清原结果后重新评估下一步。"}
        </p>
        {(event.reason || event.plan?.reason) && <p>{event.reason || event.plan?.reason}</p>}
        {event.reconciliation && <details>
          <summary>本次核对依据与请求身份</summary>
          <p>处理方：{event.reconciliation.owner === "technical_route" ? "技术 Route" : event.reconciliation.owner}；
            待核对 {event.reconciliation.active_count} 项；已有效替代且保留未知费用 {event.reconciliation.retained_unknown_count} 项。</p>
          <ul>{event.reconciliation.active.map(item => <li key={`${item.kind}:${item.id}`}>
            <code>{item.kind} · {item.id}</code> · {item.status}
            {item.classification === "historical_unresolved" ? " · 历史未结记录" : " · 当前请求"}
          </li>)}</ul>
          <p className="muted">这是本次协调的依据快照；派发前仍会复核最新状态。原费用和请求身份继续保留。</p>
        </details>}
      </article>)}
    </div>
  </details>;
}
