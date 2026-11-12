import type { RouteReport as Report } from "../lib/types";

export function RouteReport({ report }: { report?: Report }) {
  if (!report) return null;
  return <section className="question" aria-label="Route 汇报" role="status">
    <div className="eyebrow">Route 汇报</div>
    <p>{report.summary}</p>
    <p className="muted">{report.next_step}</p>
  </section>;
}
