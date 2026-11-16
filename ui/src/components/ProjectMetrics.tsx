import { ArrowUpRight, ChevronDown, Clock3, Layers3, PackageCheck, RefreshCw, Wallet } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useData } from "../lib/api";
import { metricsPeriods, metricsQuery } from "../lib/projectMetrics";
import type { MetricsPeriod, ProjectMetricsData } from "../lib/projectMetrics";
import type { Overview } from "../lib/types";
import "./ProjectMetrics.css";

const number = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 });
const elapsed = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 1 });

function duration(seconds: number | null) {
  if (seconds === null) return "暂无数据";
  if (seconds < 60) return `${elapsed.format(seconds)} 秒`;
  const minutes = seconds / 60;
  if (minutes < 60) return `${elapsed.format(minutes)} 分钟`;
  if (minutes < 1440) return `${elapsed.format(minutes / 60)} 小时`;
  return `${elapsed.format(minutes / 1440)} 天`;
}

function costLabel(provider: string, unit: string | null) {
  const name = provider === "kie" ? "KIE" : provider === "codex" ? "Codex" : provider === "agent" ? "Agent" : provider;
  return [name, unit].filter(Boolean).join(" ");
}

export function HomeActivity({ overview }: { overview: Overview | null }) {
  return (
    <nav className="home-activity" aria-label="项目运行摘要">
      <span><i aria-hidden="true" />进行中 <strong>{overview?.running ?? "—"}</strong></span>
      <span>待处理 <strong>{overview?.waiting ?? "—"}</strong></span>
      <a href="#tasks" aria-label="前往任务中心">任务中心<ArrowUpRight size={14} aria-hidden="true" /></a>
    </nav>
  );
}

function CostValue({ data }: { data: ProjectMetricsData }) {
  const values: { provider: string; unit: string | null; confirmed: number | null }[] =
    data.costs.items.filter(cost => cost.confirmed !== null);
  for (const [provider, unit] of [["kie", "credits"], ["codex", null]] as const) {
    if (!values.some(cost => cost.provider === provider)) values.push({ provider, unit, confirmed: null });
  }
  const order = (provider: string) => provider === "kie" ? 0 : provider === "codex" ? 1 : 2;
  values.sort((a, b) => order(a.provider) - order(b.provider));
  return (
    <div className="project-cost-values" tabIndex={0}
      role="region" aria-label="已确认费用，按服务商与单位分别统计">
      {values.map((cost) => (
        <span key={`${cost.provider}:${cost.unit}`}>
          {cost.confirmed === null ? "—" : number.format(cost.confirmed)} <small>{costLabel(cost.provider, cost.unit)}</small>
        </span>
      ))}
    </div>
  );
}

export function ProjectMetrics() {
  const [period, setPeriod] = useState<MetricsPeriod>("month");
  const [day, setDay] = useState(() => new Date().toDateString());
  useEffect(() => {
    const timer = window.setInterval(() => setDay(new Date().toDateString()), 30000);
    return () => window.clearInterval(timer);
  }, []);
  const query = useMemo(() => metricsQuery(period), [period, day]);
  return <section className="project-metrics" aria-labelledby="project-metrics-title">
    <div className="project-metrics-heading">
      <h2 id="project-metrics-title">项目概览</h2>
      <div className="project-metrics-periods" role="group" aria-label="统计范围">
        {metricsPeriods.map(option => <button key={option.value} type="button"
          aria-pressed={period === option.value} onClick={() => setPeriod(option.value)}>{option.label}</button>)}
      </div>
    </div>
    <MetricsContent key={query} query={query} period={period} />
  </section>;
}

function MetricsContent({ query, period }: { query: string; period: MetricsPeriod }) {
  const { data, error, loading, reload } = useData<ProjectMetricsData>(query, 30000);
  if (error && !data) {
    return (
      <div className="panel project-metrics-error" role="status">
        <strong>项目统计暂时无法读取</strong>
        <button className="text-button" onClick={reload}><RefreshCw size={14} />重新读取</button>
      </div>
    );
  }
  if (loading && !data) {
    return (
      <div className="project-metrics-loading" aria-label="项目统计" aria-busy="true">
        <span className="project-metrics-skeleton" />
        <span className="project-metrics-skeleton" />
        <span className="project-metrics-skeleton" />
        <span className="project-metrics-skeleton" />
      </div>
    );
  }
  if (!data) return null;
  return (
    <>
      {error && <p className="project-metrics-stale" role="status">连接中断 · 保留上次数据</p>}
      <div className="project-metrics-grid">
        <article className="panel project-metric-card">
          <a className="project-metric-label" href="#tasks"><Layers3 size={16} aria-hidden="true" />{period === "all" ? "项目任务" : "新增任务"}<ArrowUpRight size={13} aria-hidden="true" /></a>
          <strong className="project-metric-value">{number.format(data.tasks.total)}</strong>
        </article>
        <article className="panel project-metric-card project-cost-card">
          <div className="project-metric-label"><Wallet size={16} aria-hidden="true" />已确认花费</div>
          <CostValue data={data} />
        </article>
        <article className="panel project-metric-card">
          <div className="project-metric-label"><Clock3 size={16} aria-hidden="true" />平均制作时长</div>
          <strong className="project-metric-value">{duration(data.duration.average_seconds)}</strong>
        </article>
        <article className="panel project-metric-card">
          <a className="project-metric-label" href="#deliveries"><PackageCheck size={16} aria-hidden="true" />已交付成品<ArrowUpRight size={13} aria-hidden="true" /></a>
          <strong className="project-metric-value">{number.format(data.deliveries.total)}</strong>
        </article>
      </div>
      <details className="project-metrics-method">
        <summary>统计说明<ChevronDown size={13} aria-hidden="true" /></summary>
        <div tabIndex={0} role="region" aria-label="项目统计口径">
          <p>范围按当前浏览器的当地日历计算，本周从周一开始。任务按创建时间、成品按交付时间、花费按原调用发生时间筛选；旧任务在范围内发生的新调用也计入花费。当前范围内任务关联 {number.format(data.tasks.linked_requirements)} 个需求。</p>
          <p>平均时长取范围内完成的 {number.format(data.duration.measured_tasks)} 个任务的保留节点起止跨度，含等待与暂停；并行不重复累加，不代表模型推理时间，缺失历史不补算。
            {data.duration.longest_open_seconds !== null && ` 范围内新建的未完成任务最长跨度为 ${duration(data.duration.longest_open_seconds)}。`}</p>
          <p>KIE credits 与 Codex、其他 Agent 的费用按服务商和原单位分别显示；任务制作、管家与 Route 中可确认的 Codex 调用均纳入。预估不计入已确认，没有账单金额时显示“—”，不按调用次数推算。当前项目未登记的订阅费、人工和工具费用不计入。</p>
          <p>范围内共 {number.format(data.costs.recorded_calls)} 次登记调用，{number.format(data.costs.unsettled_calls)} 次待结算，{number.format(data.costs.unpriced_calls)} 次未返回可用价格。已交付成品中，{number.format(data.deliveries.with_issues)} 个为接受缺陷的版本，交付不等于全部质量通过。</p>
          {data.costs.items.filter(cost => cost.reserved !== null).map(cost => (
            <p key={`${cost.provider}:${cost.unit}`}>待结算预估：{number.format(cost.reserved ?? 0)} {costLabel(cost.provider, cost.unit)}</p>
          ))}
        </div>
      </details>
    </>
  );
}
