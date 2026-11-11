import { useRef, useState } from "react";
import { ChevronDown, Settings2 } from "lucide-react";
import { api, useData } from "../lib/api";
import { ErrorBox, Loading } from "./common";
import "./ManagerSettings.css";

type Config = {
  agent_enabled: boolean;
  max_calls_per_day: number;
  auto_pause: boolean;
  stall_minutes: number;
};
type State = { settings: Config; usage: { calls_today: number } };

export function ManagerSettings({ initiallyOpen = false }: { initiallyOpen?: boolean }) {
  const [open, setOpen] = useState(initiallyOpen);
  return <details id="manager-settings" className="manager-settings" open={open}
    onToggle={event => setOpen(event.currentTarget.open)}>
    <summary><Settings2 size={16} />项目管家设置<ChevronDown size={16} /></summary>
    {open && <SettingsForm />}
  </details>;
}

function SettingsForm() {
  const { data, loading, error, reload } = useData<State>("/manager");
  const [draft, setDraft] = useState<Config | null>(null);
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState("");
  const [notice, setNotice] = useState("");
  const inFlight = useRef(false);
  if (!data) return <div className="manager-settings-body">
    {loading ? <Loading /> : <><ErrorBox message={error} />
      <button type="button" className="secondary" onClick={reload}>重新读取管家设置</button></>}
  </div>;
  const current = draft ?? data.settings;
  const change = (value: Partial<Config>) => {
    setDraft({ ...current, ...value });
    setNotice("");
  };
  return <form className="manager-settings-body form-stack" aria-label="项目管家设置"
    onSubmit={async event => {
      event.preventDefault();
      if (inFlight.current) return;
      inFlight.current = true;
      setBusy(true); setFailure(""); setNotice("");
      try {
        await api("/manager/settings", { method: "PUT", body: JSON.stringify(current) });
        setDraft(null); setNotice("管家设置已保存。"); reload();
      } catch (cause) {
        setFailure(cause instanceof Error ? cause.message : "保存失败");
      } finally { inFlight.current = false; setBusy(false); }
    }}>
    <p className="manager-settings-usage">今日管理调用 {data.usage.calls_today} / {data.settings.max_calls_per_day} · 独立于制作额度</p>
    <label className="manager-settings-check"><input type="checkbox" checked={current.agent_enabled}
      disabled={busy} onChange={event => change({ agent_enabled: event.target.checked })} />启用管理 Agent 判断</label>
    <div className="manager-settings-grid">
      <label>每日管理调用上限<input type="number" min={0} max={500} required value={current.max_calls_per_day}
        disabled={busy} onChange={event => change({ max_calls_per_day: Number(event.target.value) })} /></label>
      <label>无实质进展的检查阈值（分钟）<input type="number" min={5} max={1440} required value={current.stall_minutes}
        disabled={busy} onChange={event => change({ stall_minutes: Number(event.target.value) })} /></label>
    </div>
    <label className="manager-settings-check"><input type="checkbox" checked={current.auto_pause}
      disabled={busy} onChange={event => change({ auto_pause: event.target.checked })} />允许在安全边界自动暂停任务</label>
    <p className="manager-settings-hint">默认由你确认暂停建议；开启自动暂停后，管家可在当前步骤结束时暂停任务。</p>
    <div className="actions wrap">
      <button className="secondary" disabled={busy || JSON.stringify(current) === JSON.stringify(data.settings)}>
        {busy ? "保存中…" : "保存管家设置"}
      </button>
      <a href="#overview">返回首页对话</a>
    </div>
    <ErrorBox message={failure || error} />
    {notice && <p role="status">{notice}</p>}
  </form>;
}
