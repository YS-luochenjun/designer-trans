import { useRef, useState } from "react";
import { api } from "./api";

export type ManagerEvent = {
  id: string; kind: string; status: string; created_at: string; reply: string | null; error: string | null;
  payload: {
    text?: string; title?: string; answer?: string; task_id?: string; schedule_id?: string;
    schedule_name?: string; root_event_id?: string; parent_event_id?: string;
    origin?: { task_id?: string; schedule_id?: string };
  };
};
export type ManagerAction = {
  id: string; kind: string; target_id: string; reason: string; status: string; error?: string;
  result?: { task_id?: string }; created_at: string;
};
export type ManagerQuestion = {
  id: string; prompt: string; status: string; event_id?: string; scope_key?: string; task_id?: string;
};
export type ManagerState = {
  running: boolean; error: string | null;
  settings: { agent_enabled: boolean; max_calls_per_day: number };
  events: ManagerEvent[]; event_total: number;
  incidents: { id: string; kind: string; title: string; target_id: string; status: string }[];
  questions: ManagerQuestion[]; actions: ManagerAction[];
  operations: { id: string; model: string; engine: string; status: string; created_at: string; error?: string }[];
  discovery_history?: ManagerEvent[]; discovery_event_total?: number;
  usage: { calls_today: number; total_calls: number };
};
export const actionNames: Record<string, string> = {
  pause_task: "暂停任务", resume_task: "恢复任务", retry_task: "重试任务",
  sync_requirements: "刷新需求", create_task: "创建制作任务",
};
export const eventNames: Record<string, string> = {
  message: "项目对话", answer: "回答跟进", task_attention: "任务异常",
  task_stalled: "进度核对", requirement_found: "发现需求", patrol: "任务巡检",
  requirements_check: "需求刷新", production_answer: "节点回答跟进", scheduled_message: "定时消息",
};

export function useManagerCommand(reload: () => void) {
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState("");
  const inFlight = useRef(false);
  const perform = async (path: string, body?: unknown) => {
    if (inFlight.current) return false;
    inFlight.current = true;
    setBusy(true); setFailure("");
    try {
      await api(path, { method: "POST", ...(body !== undefined ? { body: JSON.stringify(body) } : {}) });
      reload();
      return true;
    } catch (cause) {
      setFailure(cause instanceof Error ? cause.message : "操作失败");
      return false;
    } finally {
      inFlight.current = false; setBusy(false);
    }
  };
  return { busy, failure, perform };
}
