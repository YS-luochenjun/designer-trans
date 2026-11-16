export type MetricsPeriod = "today" | "week" | "month" | "all";

export const metricsPeriods: { value: MetricsPeriod; label: string }[] = [
  { value: "today", label: "今日" },
  { value: "week", label: "本周" },
  { value: "month", label: "本月" },
  { value: "all", label: "全部" },
];

export function metricsQuery(period: MetricsPeriod, now = new Date()) {
  if (period === "all") return "/project-metrics";
  const start = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const end = new Date(start);
  if (period === "today") end.setDate(end.getDate() + 1);
  if (period === "week") {
    start.setDate(start.getDate() - (start.getDay() + 6) % 7);
    end.setTime(start.getTime());
    end.setDate(end.getDate() + 7);
  }
  if (period === "month") {
    start.setDate(1);
    end.setTime(start.getTime());
    end.setMonth(end.getMonth() + 1);
  }
  return `/project-metrics?${new URLSearchParams({ start: start.toISOString(), end: end.toISOString() })}`;
}

export interface ProjectMetricsData {
  as_of: string;
  window: { start: string | null; end: string | null };
  tasks: { total: number; linked_requirements: number };
  deliveries: { total: number; with_issues: number };
  duration: {
    average_seconds: number | null;
    measured_tasks: number;
    longest_open_seconds: number | null;
    scope: "completed_task_wall_clock";
  };
  costs: {
    items: {
      provider: string;
      unit: string | null;
      confirmed: number | null;
      reserved: number | null;
      confirmed_operations: number;
      unsettled_operations: number;
      unknown_operations: number;
    }[];
    unsettled_calls: number;
    unpriced_calls: number;
    recorded_calls: number;
  };
}
