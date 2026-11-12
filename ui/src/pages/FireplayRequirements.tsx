import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { api, useData } from "../lib/api";
import { Select } from "../components/Select";
import { FireplayReadDialog } from "../components/FireplayReadDialog";
import { RequirementHistory } from "../components/ManagementRecords";
import { ErrorBox, PageHead } from "../components/common";
import type { Page, Task } from "../lib/types";
import { time } from "../lib/types";

const PAGE_SIZE = 10;

const blockerNames: Record<string, string> = {
  requirement_adapter_unavailable: "平台回传尚未接通",
  remote_schema_not_verified: "平台写入接口尚未核验",
  material_binding_permission_missing: "缺少素材绑定权限",
  status_update_permission_missing: "缺少需求完成权限",
};

type External = { id: string; code?: string; title: string; status: string; ai_mode?: string; delivery_label?: string; ai_mode_label?: string };
type Snapshot = Omit<Page<External>, "total"> & {
  total: number | null;
  total_exact?: boolean;
  source_total?: number;
  returned?: number;
  scanned?: number;
  pages_read?: number;
  filter_mode?: string;
  selected_delivery_types?: string[];
  configured: boolean;
  complete: boolean;
  last_synced_at?: string;
  error?: string;
};
type Connection = {
  configured: boolean;
  credential_configured?: boolean;
  code?: string;
  connected?: boolean;
  business_authorized?: boolean | null;
  message?: string;
  credential_expires_at?: string;
  automatic_renewal?: boolean;
  renewal_status?: string;
};
type LoginState = { status: string; message: string };
const connectionLabels: Record<string, string> = {
  configured: "已配置，待检查",
  protocol_ready: "已连接",
  credentials_missing: "尚未登录",
  credentials_expired: "登录已过期",
  refresh_unknown: "续期未确认，请重新登录",
  refresh_login_required: "登录续期已失效",
  refresh_rejected: "登录续期已失效",
  refresh_busy: "登录正在更新",
  unauthorized: "授权失效或没有查询权限",
  timeout: "连接超时",
  disabled: "连接未启用",
  required_tool_missing: "远端未提供需求查询工具",
};
type Requirement = {
  id: string;
  code: string;
  title: string;
  local_status: string;
  remote_status: string;
  task_ids: string[];
};
type Preview = {
  id: string;
  code: string;
  state: string;
  blockers: string[];
  executable: boolean;
  downloads: { locale: string; sha256: string; url: string }[];
};

export function FireplayRequirements() {
  const [pageIndex, setPageIndex] = useState(0);
  const remote = useData<Snapshot>("/fireplay/projects");
  const local = useData<Page<Requirement>>("/requirements");
  const tasks = useData<Page<Task>>("/tasks");
  const connection = useData<Connection>("/fireplay/connection");
  const [busy, setBusy] = useState(false);
  const inFlight = useRef(false);
  const [busyMessage, setBusyMessage] = useState("");
  const [choosingDelivery, setChoosingDelivery] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [requirementId, setRequirementId] = useState("");
  const [taskId, setTaskId] = useState("");
  const [preview, setPreview] = useState<Preview | null>(null);
  const [brief, setBrief] = useState<Record<string, unknown> | null>(null);
  const [confirmedSources, setConfirmedSources] = useState(false);
  const [checkedConnection, setCheckedConnection] = useState<Connection | null>(null);
  const [loginState, setLoginState] = useState<LoginState | null>(null);
  const loginPending = ["starting", "waiting", "checking"].includes(loginState?.status ?? "");
  useEffect(() => {
    if (!loginPending) return;
    let disposed = false;
    const timer = window.setInterval(() => {
      void api<LoginState>("/fireplay/login").then(async (result) => {
        if (disposed) return;
        if (result.status === "succeeded") {
          const checked = await api<Connection>("/fireplay/probe", { method: "POST" });
          if (!disposed) { setCheckedConnection(checked); connection.reload(); remote.reload(); }
        }
        if (!disposed) setLoginState(result);
      }).catch(() => {
        if (!disposed) setLoginState({ status: "failed", message: "登录状态读取失败，请检查工作台连接" });
      });
    }, 1500);
    return () => { disposed = true; window.clearInterval(timer); };
  }, [loginPending]);
  const currentConnection = checkedConnection ?? connection.data;
  const renewalNeedsLogin = ["unknown", "login_required"].includes(currentConnection?.renewal_status ?? "");
  const expired = renewalNeedsLogin || ["credentials_expired", "unauthorized", "refresh_rejected"].includes(currentConnection?.code ?? "");
  const items = remote.data?.items ?? [];
  const pageCount = Math.max(1, Math.ceil(items.length / PAGE_SIZE));
  const currentPage = Math.min(pageIndex, pageCount - 1);

  async function run(action: () => Promise<void>, message = "正在处理，请稍候…") {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    setBusyMessage(message);
    setError("");
    setNotice("");
    try {
      await action();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "操作失败");
    } finally {
      inFlight.current = false;
      setBusy(false);
      setBusyMessage("");
    }
  }
  async function loadBrief(id: string) {
    await run(async () => {
      setRequirementId(id);
      setBrief(null);
      setConfirmedSources(false);
      setBrief(await api<Record<string, unknown>>(`/fireplay/requirements/${id}/brief`));
    }, "正在读取需求详情…");
  }
  function accept(event: FormEvent) {
    event.preventDefault();
    void run(async () => {
      await api("/requirements/accept", {
        method: "POST",
        body: JSON.stringify({
          requirement_id: requirementId,
          task_id: taskId,
        }),
      });
      local.reload();
      setNotice("已在本项目承接并关联制作任务，平台状态尚未更改。");
    }, "正在核对并关联本地制作任务…");
  }
  return (
    <>
      <PageHead eyebrow="" title="需求中心" />
      <ErrorBox
        message={
          error ||
          remote.error ||
          local.error ||
          connection.error ||
          remote.data?.error ||
          ""
        }
      />
      {busy && <p className="scope-banner" role="status" aria-live="polite">{busyMessage}</p>}
      {loginState && <p role="status">{loginState.message}</p>}
      {remote.loading && <p role="status">正在读取本地需求列表…</p>}
      {notice && (
        <p className="scope-banner" role="status">
          {notice}
        </p>
      )}
      <section className="panel form-stack fireplay-connection">
        <div className="panel-heading"><h2>Fireplay 需求</h2>
          <span className={`status ${expired ? "status-failed" : currentConnection?.connected ? "status-succeeded" : ""}`}>
            {connectionLabels[currentConnection?.code ?? ""] ?? (currentConnection?.configured ? "待检查" : "尚未连接")}
          </span>
        </div>
        {renewalNeedsLogin ? <p role="alert">登录自动续期未确认或已失效，请重新登录。项目不会重复提交原续期请求。</p>
          : expired && <p role="alert">访问凭据{currentConnection?.code === "credentials_expired" && currentConnection?.credential_expires_at ? `已于 ${time(currentConnection.credential_expires_at)} 过期` : "已失效或不具备查询权限"}。请重新授权后检查连接。</p>}
        {currentConnection?.automatic_renewal && !renewalNeedsLogin && <p>已启用登录自动续期；服务端撤销或会话到期后仍需重新登录。</p>}
        {currentConnection?.credential_configured && currentConnection?.automatic_renewal === false && <p>当前只有访问凭据；重新登录一次可取得服务端提供的续期凭据。</p>}
        <div className="toolbar">
          <button className="primary" disabled={busy || loginPending} onClick={() => void run(async () => {
            try {
              setLoginState(await api<LoginState>("/fireplay/login", { method: "POST" }));
            } catch (cause) {
              if (cause instanceof Error && cause.message === "Not Found")
                throw new Error("当前后台尚未加载登录接口，请重启工作台服务后重新登录。");
              throw cause;
            }
          })}>{loginPending ? "等待登录…" : "重新登录"}</button>
          <button
            className="secondary"
            disabled={busy}
            onClick={() =>
              void run(async () => {
                const result = await api<Connection>("/fireplay/probe", {
                  method: "POST",
                });
                setCheckedConnection(result);
                if (result.connected) setNotice("连接成功，可继续读取需求。");
                else setError(result.message ?? connectionLabels[result.code ?? ""] ?? "连接未成功");
              }, "正在检查 MCP 连接…")
            }
          >
            检查连接
          </button>
          <button
            className="secondary"
            disabled={busy || !remote.data?.configured || expired}
            onClick={() => setChoosingDelivery(true)}
          >
            读取平台需求
          </button>
        </div>
        <small>
          最近读取：{time(remote.data?.last_synced_at ?? null)} ·{" "}
          {remote.data?.complete ? "已完整读取本次范围" : "尚未证实完整覆盖"}
          {remote.data?.returned !== undefined && ` · 已返回 ${remote.data.returned} 项`}
          {remote.data?.total_exact && remote.data.total !== null && ` / 共 ${remote.data.total} 项`}
        </small>
        <div className="requirements-table">
        <div className="requirement-list">
        {items.slice(currentPage * PAGE_SIZE, (currentPage + 1) * PAGE_SIZE).map((item) => (
          <div className="external-row requirement-row" key={item.id}>
            <div>
              <strong>
                {item.code} {item.title}
              </strong>
            </div>
            <span className="pill">{item.delivery_label || item.ai_mode_label || item.ai_mode || "未标注"}</span>
            <span className="pill">{({not_started: "Not started", in_progress: "In progress", completed: "Done"} as Record<string, string>)[item.status] ?? item.status}</span>
            <a
              className="secondary"
              href={`#fireplay/${item.id}`}
            >
              查看需求
            </a>
          </div>
        ))}
        </div>
        <nav className="toolbar requirement-pagination" aria-label="需求分页">
          <button className="secondary" disabled={currentPage === 0} onClick={() => setPageIndex(currentPage - 1)}>上一页</button>
          <span>第 {currentPage + 1} / {pageCount} 页 · 已读取 {items.length} 项</span>
          <button className="secondary" disabled={currentPage + 1 >= pageCount} onClick={() => setPageIndex(currentPage + 1)}>下一页</button>
        </nav>
        </div>
      </section>
      {choosingDelivery && <FireplayReadDialog onClose={() => setChoosingDelivery(false)} onRead={() => {
        setChoosingDelivery(false);
        setNotice("已按所选 AI 承接交付方式读取需求。");
        remote.reload();
        setPageIndex(0);
      }} />}
      <details className="panel requirement-link-panel">
        <summary>关联已有制作任务</summary>
        <div className="form-stack">
        <label>
          平台需求 UUID
          <input
            disabled={busy}
            value={requirementId}
            onChange={(e) => {
              setRequirementId(e.target.value);
              setBrief(null);
              setConfirmedSources(false);
            }}
            placeholder="从需求详情复制 UUID"
          />
        </label>
        <button
          className="secondary"
          disabled={busy || !requirementId}
          onClick={() => void loadBrief(requirementId)}
        >
          {busy && busyMessage === "正在读取需求详情…" ? "正在读取需求详情…" : "读取需求详情"}
        </button>
        {brief && (
          <>
            <strong>{String(brief.title ?? "")}</strong>
            <p style={{ whiteSpace: "pre-wrap" }}>
              {String(brief.description ?? "")}
            </p>
            <p style={{ whiteSpace: "pre-wrap" }}>
              {String(brief.refText ?? "")}
            </p>
            <p>
              目标语言：
              {Array.isArray(brief.locales)
                ? brief.locales.join("、")
                : "请核对需求原文"}
            </p>
          </>
        )}
        <form className="form-stack" onSubmit={accept}>
          <label>
            对应的本地制作任务
            <Select
              required
              disabled={busy || tasks.loading}
              value={taskId}
              onChange={(e) => {
                setTaskId(e.target.value);
                setConfirmedSources(false);
              }}
            >
              <option value="">请选择任务</option>
              {(tasks.data?.items ?? []).map((task) => (
                <option key={task.id} value={task.id}>
                  {task.title} · {task.target_locales.join(" / ")}
                </option>
              ))}
            </Select>
          </label>
          <label className="source-confirm">
            <input
              type="checkbox"
              disabled={busy}
              checked={confirmedSources}
              onChange={(e) => setConfirmedSources(e.target.checked)}
            />
            我已核对该任务包含此需求全部原片和目标语言
          </label>
          <button
            className="primary"
            disabled={busy || !brief || !confirmedSources || !taskId}
          >
            在本项目承接并关联
          </button>
        </form>
        </div>
      </details>
      <section className="panel form-stack">
        <h2>已承接需求与回传准备</h2>
        <p>
          本地提交预览会核对全部成片及人工审核哈希。当前远端上传、绑定和完成状态写入尚未启用，预览不会发送任何平台写请求。
        </p>
        {(local.data?.items ?? []).map((item) => (
          <div key={item.id} className="form-stack">
            <strong>
              {item.code} {item.title}
            </strong>
            <span>
              本地：{item.local_status} · 平台最近状态：{item.remote_status}
            </span>
            {item.task_ids.map((id) => (
              <div key={id} className="toolbar">
                <a href={`#tasks/${id}`}>查看制作与审核</a>
                <button
                  className="secondary"
                  disabled={busy}
                  onClick={() =>
                    void run(async () => {
                      setPreview(
                        await api<Preview>(`/tasks/${id}/submission-preview`, {
                          method: "POST",
                        }),
                      );
                    }, "正在生成本地提交预览…")
                  }
                >
                  生成审核成片提交预览
                </button>
              </div>
            ))}
          </div>
        ))}
        {preview && (
          <div className="form-stack" aria-live="polite">
            <strong>{preview.code} · 已生成本地提交预览</strong>
            <p>尚未提交到平台。远端接口与身份核验完成后才能自动回传。</p>
            <small>
              待解决：
              {preview.blockers
                .map((value) => blockerNames[value] ?? "需要核对平台配置")
                .join("；")}
            </small>
            {preview.downloads.map((item) => (
              <div key={item.sha256}>
                <a href={item.url}>下载 {item.locale} 已审核成片</a>
                <small>{item.sha256}</small>
              </div>
            ))}
          </div>
        )}
      </section>
      <RequirementHistory />
    </>
  );
}

export function FireplayRequirementDetail({ id }: { id: string }) {
  const { data, loading, error, reload } = useData<Record<string, unknown>>(
    `/fireplay/requirements/${encodeURIComponent(id)}/brief`,
  );
  return <>
    <a className="back-link" href="#fireplay">返回需求列表</a>
    <PageHead eyebrow="" title={String(data?.code || "需求详情")} />
    <ErrorBox message={error} />
    {loading && <p role="status">正在读取需求…</p>}
    {error && <button className="secondary" onClick={reload}>重新读取</button>}
    {data && <section className="panel form-stack requirement-detail">
      {data.title ? <h2>{String(data.title)}</h2> : null}
      <p>平台状态：{String(data.status ?? "未提供")}</p>
      <p>目标语言：{Array.isArray(data.locales) ? data.locales.join("、") : "未提供"}</p>
      {data.description ? <p>{String(data.description)}</p> : null}
      {data.refText ? <p>{String(data.refText)}</p> : null}
      <details><summary>完整需求信息</summary><pre className="json-view">{JSON.stringify(data, null, 2)}</pre></details>
    </section>}
  </>;
}
