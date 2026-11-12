import { useState } from "react";
import type { FormEvent } from "react";
import { CalendarClock, Play, Plus, Trash2 } from "lucide-react";
import { Select } from "../components/Select";
import { AutomationActivity } from "../components/ManagementRecords";
import { api, useData } from "../lib/api";
import type { Page, Schedule } from "../lib/types";
import { time } from "../lib/types";
import {
  Empty,
  ErrorBox,
  Loading,
  Modal,
  PageHead,
  Status,
} from "../components/common";

const kindLabels = {
  project_patrol: "巡检任务",
  requirements_check: "刷新需求并跟进",
  agent_message: "自定义轮询",
};

export function Schedules() {
  const { data, error, loading, reload } = useData<Page<Schedule>>(
    "/schedules",
    5000,
  );
  const [adding, setAdding] = useState(false),
    [editing, setEditing] = useState<Schedule | null>(null),
    [selected, setSelected] = useState<string | null>(null),
    [actionError, setActionError] = useState(""),
    [busy, setBusy] = useState(false);
  const perform = async (path: string, method: string, body?: unknown) => {
    setBusy(true);
    setActionError("");
    try {
      await api(path, {
        method,
        ...(body ? { body: JSON.stringify(body) } : {}),
      });
      reload();
    } catch (e) {
      setActionError(e instanceof Error ? e.message : "操作失败");
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <PageHead
        eyebrow=""
        title="自动化"
        action={
          <button className="primary" onClick={() => setAdding(true)}>
            <Plus size={16} />
            新建计划
          </button>
        }
      />
      <ErrorBox message={error || actionError} />
      {loading && !data ? (
        <Loading />
      ) : data?.items.length ? (
        <div className="schedule-grid">
          {data.items.map((s) => (
            <article className="panel" key={s.id}>
              <div className="panel-heading">
                <CalendarClock size={20} />
                <span className={`pill ${s.enabled ? "on" : ""}`}>
                  {s.enabled ? "已启用" : "已暂停"}
                </span>
              </div>
              <h2>{s.name}</h2>
              <p className="muted">
                {kindLabels[s.kind] ?? "旧版计划（已移除）"} ·{" "}
                {s.trigger === "interval"
                  ? `每 ${s.interval_seconds} 秒`
                  : time(s.run_at)}
              </p>
              {s.kind === "agent_message" && <blockquote className="schedule-message">{s.prompt}</blockquote>}
              <dl>
                <dt>下次运行</dt>
                <dd>{time(s.next_run_at)}</dd>
                <dt>时区</dt>
                <dd>{s.timezone}</dd>
                <dt>最近结果</dt>
                <dd>
                  {s.last_result ? (
                    <Status value={s.last_result} />
                  ) : (
                    "尚未运行"
                  )}
                </dd>
              </dl>
              {s.last_reply && <p className="schedule-reply">{s.last_reply}</p>}
              <ErrorBox message={s.last_error ?? ""} />
              <div className="actions wrap">
                <button
                  className="secondary"
                  disabled={busy}
                  onClick={() =>
                    void perform(`/schedules/${s.id}`, "PATCH", {
                      enabled: !s.enabled,
                    })
                  }
                >
                  {s.enabled ? "暂停" : "启用"}
                </button>
                <button
                  className="secondary"
                  disabled={busy}
                  onClick={() => setEditing(s)}
                >
                  编辑
                </button>
                <button
                  className="secondary"
                  disabled={busy}
                  onClick={() => void perform(`/schedules/${s.id}/run`, "POST")}
                >
                  <Play size={14} />
                  立即运行
                </button>
                <button
                  className="text-button"
                  onClick={() => setSelected(s.id)}
                >
                  记录
                </button>
                <button
                  className="icon-button danger"
                  aria-label={`删除计划 ${s.name}`}
                  disabled={busy}
                  onClick={() => {
                    if (
                      window.confirm(
                        `删除计划“${s.name}”？已创建的任务和触发历史将保留。`,
                      )
                    )
                      void perform(`/schedules/${s.id}`, "DELETE");
                  }}
                >
                  <Trash2 size={16} />
                </button>
              </div>
            </article>
          ))}
        </div>
      ) : (
        <Empty title="还没有定时计划" />
      )}
      {(adding || editing) && (
        <ScheduleForm
          existing={editing}
          onClose={() => {
            setAdding(false);
            setEditing(null);
          }}
          onSaved={() => {
            setAdding(false);
            setEditing(null);
            reload();
          }}
        />
      )}
      {selected && <Runs id={selected} onClose={() => setSelected(null)} />}
      <AutomationActivity />
    </>
  );
}
function Runs({ id, onClose }: { id: string; onClose: () => void }) {
  const { data, error, loading } = useData<Page<{
    id: string; status: string; triggered_at: string; reply?: string; error?: string; message?: string;
  }>>(
    `/schedules/${id}/runs`,
    3000,
  );
  return (
    <Modal title="触发记录" onClose={onClose}>
      <ErrorBox message={error} />
      {loading ? (
        <Loading />
      ) : data?.items.length ? (
        <div className="schedule-run-list">{data.items.map(run => <article key={run.id} className="schedule-run">
          <div className="schedule-run-head"><Status value={run.status} /><time>{time(run.triggered_at)}</time></div>
          {run.message && <blockquote className="schedule-message">{run.message}</blockquote>}
          {run.reply && <p>{run.reply}</p>}
          <ErrorBox message={run.error ?? ""} />
        </article>)}</div>
      ) : (
        <Empty title="尚无触发记录" />
      )}
    </Modal>
  );
}
function ScheduleForm({
  existing,
  onClose,
  onSaved,
}: {
  existing: Schedule | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [name, setName] = useState(existing?.name ?? ""),
    [kind, setKind] = useState<Schedule["kind"]>(existing?.kind ?? "project_patrol"),
    [trigger, setTrigger] = useState(existing?.trigger ?? "interval"),
    [seconds, setSeconds] = useState(existing?.interval_seconds ?? 300),
    [prompt, setPrompt] = useState(existing?.prompt ?? ""),
    [refreshRequirements, setRefreshRequirements] = useState(existing?.refresh_requirements ?? false),
    [runAt, setRunAt] = useState(""),
    [timezone, setTimezone] = useState(existing?.timezone ?? "Asia/Shanghai"),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const payload = {
        name,
        kind,
        trigger,
        interval_seconds: trigger === "interval" ? seconds : null,
        run_at:
          trigger === "once"
            ? runAt
              ? new Date(runAt).toISOString()
              : existing?.run_at
            : null,
        timezone,
        prompt: kind === "agent_message" ? prompt : "",
        refresh_requirements: kind === "agent_message" && refreshRequirements,
      };
      await api(existing ? `/schedules/${existing.id}` : "/schedules", {
        method: existing ? "PATCH" : "POST",
        body: JSON.stringify(payload),
      });
      onSaved();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "保存失败");
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal title={existing ? "编辑计划" : "新建计划"} onClose={onClose}>
      <form className="form-stack" onSubmit={submit}>
        <label>
          计划名称
          <input
            required
            maxLength={200}
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
        </label>
        <label>
          工作类型
          <Select
            aria-label="工作类型"
            value={kind}
            onChange={(e) => {
              const next = e.target.value as Schedule["kind"];
              setKind(next);
              if (!existing) setSeconds(next === "project_patrol" ? 300 : 1800);
            }}
          >
            {Object.entries(kindLabels).map(([value, label]) =>
              <option key={value} value={value}>{label}</option>)}
          </Select>
        </label>
        {kind === "agent_message" && <>
          <label>发给管理 Agent 的消息
            <textarea rows={5} maxLength={4000} required value={prompt} onChange={e => setPrompt(e.target.value)}
              placeholder="检查运行中的任务；有新问题时说明原因并给出处理建议。" />
          </label>
          <label className="schedule-checkbox"><input type="checkbox" checked={refreshRequirements}
            onChange={e => setRefreshRequirements(e.target.checked)} />执行前刷新需求</label>
        </>}
        <div className="form-grid">
          <label>
            触发方式
            <Select
              aria-label="触发方式"
              value={trigger}
              onChange={(e) =>
                setTrigger(e.target.value as "once" | "interval")
              }
            >
              <option value="interval">固定间隔</option>
              <option value="once">一次性</option>
            </Select>
          </label>
          {trigger === "interval" ? (
            <label>
              间隔秒数
              <input
                type="number"
                min={60}
                value={seconds}
                onChange={(e) => setSeconds(Number(e.target.value))}
              />
            </label>
          ) : (
            <label>
              执行时间（浏览器本地）
              <input
                type="datetime-local"
                required={!existing?.run_at}
                value={runAt}
                onChange={(e) => setRunAt(e.target.value)}
              />
            </label>
          )}
        </div>
        <label>
          计划时区
          <input
            aria-label="计划时区"
            value={timezone}
            onChange={(e) => setTimezone(e.target.value)}
            required
          />
        </label>
        <ErrorBox message={error} />
        <div className="modal-actions">
          <button
            type="button"
            className="secondary"
            disabled={busy}
            onClick={onClose}
          >
            取消
          </button>
          <button className="primary" disabled={busy}>
            {busy ? "正在保存…" : "保存计划"}
          </button>
        </div>
      </form>
    </Modal>
  );
}
