import { useEffect, useState } from "react";
import {
  Download,
  Plus,
  RefreshCw,
  Search,
  Settings2,
} from "lucide-react";
import { api, useData } from "../lib/api";
import { TaskDeletion, readPendingDeletion } from "../components/TaskDeletion";
import type { PendingDeletion } from "../components/TaskDeletion";
import { Select } from "../components/Select";
import { ModelSelection } from "../components/ModelSelection";
import { CapabilityCards } from "../components/CapabilityCards";
import { DeveloperView, ProjectSteward } from "./Management";
import type {
  Delivery,
  Overview,
  Page,
  Resource,
  Settings,
  Task,
} from "../lib/types";
import { time } from "../lib/types";
import {
  Empty,
  ErrorBox,
  Loading,
  PageHead,
  Status,
  TaskList,
} from "../components/common";

const TASK_PAGE_SIZE = 8;

type Navigation = { onTask: (id: string) => void; onNew: () => void };
export function Dashboard() {
  const overview = useData<Overview>("/overview", 4000);
  return (
    <>
      <PageHead
        eyebrow=""
        title="首页"
      />
      <ErrorBox message={overview.error} />
      <nav className="home-summary" aria-label="项目运行摘要">
        {[
          ["正在制作", overview.data?.running, "#tasks"],
          ["任务待处理", overview.data?.waiting, "#tasks"],
          ["已交付成品", overview.data?.deliveries, "#deliveries"],
        ].map(([label, value, href]) => (
          <a key={String(label)} href={String(href)}>
            <span>{label}</span>
            <strong>{value ?? "—"}</strong>
          </a>
        ))}
      </nav>
      <ProjectSteward />
    </>
  );
}
export function Tasks({ onTask, onNew }: Navigation) {
  const [search, setSearch] = useState(""),
    [status, setStatus] = useState(""),
    [offset, setOffset] = useState(0);
  const [deleting, setDeleting] = useState<Pick<Task, "id" | "title"> | null>(null);
  const [pendingDeletion, setPendingDeletion] = useState<PendingDeletion | null>(readPendingDeletion);
  const [deleteNotice, setDeleteNotice] = useState("");
  const { data, error, loading, reload } = useData<Page<Task>>(
    `/tasks?q=${encodeURIComponent(search)}&status=${status}&limit=${TASK_PAGE_SIZE}&offset=${offset}`,
    4000,
  );
  useEffect(() => {
    if (data && !loading) {
      const lastOffset = Math.max(0, Math.ceil(data.total / TASK_PAGE_SIZE) - 1) * TASK_PAGE_SIZE;
      if (offset > lastOffset) setOffset(lastOffset);
    }
  }, [data, loading, offset]);
  return (
    <>
      <PageHead
        eyebrow=""
        title="任务中心"
        action={
          <button className="primary" onClick={onNew}>
            <Plus size={16} />
            新建翻译
          </button>
        }
      />
      <div className="toolbar">
        <label className="search">
          <Search size={17} />
          <input
            aria-label="搜索任务"
            placeholder="搜索任务名称或编号"
            value={search}
            onChange={(e) => {
              setSearch(e.target.value);
              setOffset(0);
            }}
          />
        </label>
        <Select
          aria-label="按状态筛选"
          value={status}
          onChange={(e) => {
            setStatus(e.target.value);
            setOffset(0);
          }}
        >
          <option value="">全部状态</option>
          <option value="running">执行中</option>
          <option value="waiting_user">等待确认</option>
          <option value="blocked">需要处理</option>
          <option value="completed">全部完成</option>
          <option value="partial_completed">部分完成</option>
          <option value="cancelled">已取消</option>
        </Select>
        <span className="muted">{data?.total ?? "—"} 项任务</span>
      </div>
      <ErrorBox message={error} />
      {deleteNotice && <p role="status">{deleteNotice}</p>}
      {pendingDeletion && <div className="deletion-pending" role="status"><span>“{pendingDeletion.task.title}” 的删除尚未全部确认。</span><button className="secondary" onClick={() => setDeleting(pendingDeletion.task)}>继续完成删除</button></div>}
      <section className="panel">
        {loading && !data ? (
          <Loading />
        ) : data?.items.length ? (
          <TaskList tasks={data.items} onSelect={onTask} />
        ) : (
          <Empty title={search || status ? "没有匹配的任务" : "尚无翻译任务"}>
            任务与状态将实时出现在这里。
          </Empty>
        )}
      </section>
      {deleting && <TaskDeletion task={deleting} pending={pendingDeletion?.task.id === deleting.id ? pendingDeletion : null}
        onPending={(receipt) => { setPendingDeletion(receipt); reload(); }}
        onDeleted={() => { setPendingDeletion(null); setDeleting(null); setDeleteNotice("任务及独占文件已删除，共享素材已保留。"); reload(); }}
        onClose={() => setDeleting(null)} />}
      {!!data && data.total > 0 && (
        <nav className="pagination" aria-label="任务分页">
          <button
            className="secondary"
            disabled={loading || !offset}
            onClick={() => setOffset(Math.max(0, offset - TASK_PAGE_SIZE))}
          >
            上一页
          </button>
          <span>
            {Math.floor(offset / TASK_PAGE_SIZE) + 1} / {Math.max(1, Math.ceil((data?.total ?? 0) / TASK_PAGE_SIZE))}
          </span>
          <button
            className="secondary"
            disabled={loading || offset + TASK_PAGE_SIZE >= (data?.total ?? 0)}
            onClick={() => setOffset(offset + TASK_PAGE_SIZE)}
          >
            下一页
          </button>
        </nav>
      )}
    </>
  );
}
export function Deliveries({ onTask }: { onTask: (id: string) => void }) {
  const { data, error, loading } = useData<Page<Delivery>>("/deliveries", 5000);
  return (
    <>
      <PageHead eyebrow="" title="成品库"/>
      <ErrorBox message={error} />
      {loading ? (
        <Loading />
      ) : data?.items.length ? (
        <div className="preview-grid">
          {data.items.map((d) => (
            <article className="panel delivery-card" key={d.id}>
              <video controls preload="metadata" src={d.content_url} />
              <div className="delivery-info">
                <div className="panel-heading">
                  <strong>{d.title}</strong>
                  <span className="locale-tag">{d.locale}</span>
                </div>
                <Status value={d.quality_status} />
                <p className="muted">{time(d.created_at)} · 本地交付</p>
                <div className="actions">
                  <button
                    className="secondary"
                    onClick={() => onTask(d.task_id)}
                  >
                    查看任务
                  </button>
                  <a
                    className="primary"
                    href={`${d.content_url}?download=true`}
                    download
                  >
                    <Download size={15} />
                    下载成品
                  </a>
                </div>
              </div>
            </article>
          ))}
        </div>
      ) : (
        <Empty title="尚无已交付成品">
          完成制作与完整视听验收后，成片会保存在这里。
        </Empty>
      )}
    </>
  );
}
interface Projects extends Page<{
  id: string;
  title: string;
  status: string;
  updated_at: string | null;
}> {
  configured: boolean;
  complete: boolean;
  last_synced_at: string | null;
  scope: string;
  error?: string;
}
export function Fireplay() {
  const { data, error, loading, reload } =
    useData<Projects>("/fireplay/projects");
  const [busy, setBusy] = useState(false),
    [actionError, setActionError] = useState("");
  const sync = async () => {
    setBusy(true);
    setActionError("");
    try {
      await api("/fireplay/sync", { method: "POST" });
      reload();
    } catch (e) {
      setActionError(e instanceof Error ? e.message : "同步失败");
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <PageHead
        eyebrow=""
        title="Fireplay 接入"
        action={
          <button
            className="secondary"
            disabled={busy || !data?.configured}
            onClick={() => void sync()}
          >
            <RefreshCw size={16} className={busy ? "spin" : ""} />
            {busy ? "同步中" : "同步状态"}
          </button>
        }
      />
      <ErrorBox message={error || actionError || data?.error || ""} />
      {loading ? (
        <Loading />
      ) : !data?.configured ? (
        <section className="panel">
          <Empty title="当前未连接真实 Fireplay">
            <Settings2 size={18} />{" "}
            在运行环境配置桥接地址、认证与可见范围后，便可查看平台项目。当前聊天的
            MCP 登录不会自动传入后台应用。
          </Empty>
        </section>
      ) : (
        <>
          <div className="scope-banner">
            <span>范围：{data.scope}</span>
            <span>最近同步：{time(data.last_synced_at)}</span>
            <strong>
              {data.complete
                ? "完整同步"
                : `当前 ${data.items.length} / ${data.total} 项，尚未完整`}
            </strong>
          </div>
          <section className="panel">
            {data.items.length ? (
              data.items.map((p) => (
                <div className="external-row" key={p.id}>
                  <div>
                    <strong>{p.title}</strong>
                    <small>{p.id}</small>
                  </div>
                  <span className="pill">平台：{p.status}</span>
                  <span className="muted">{time(p.updated_at)}</span>
                </div>
              ))
            ) : (
              <Empty title="此范围尚无同步数据" />
            )}
          </section>
        </>
      )}
    </>
  );
}
export function Resources({ section, view }: { section?: string; view?: string }) {
  const developer = section === "developer";
  return <>
    <PageHead eyebrow="" title="设置" />
    <div className="tabs" role="tablist" aria-label="设置类别">
      <button role="tab" aria-selected={!developer} className={!developer ? "active" : ""}
        onClick={() => { location.hash = "resources"; }}>模型与资源</button>
      <button role="tab" aria-selected={developer} className={developer ? "active" : ""}
        onClick={() => { location.hash = "resources/developer"; }}>开发者视图</button>
    </div>
    {developer ? <DeveloperView view={view} /> : <ResourceSettings section={section} />}
  </>;
}
function ResourceSettings({ section }: { section?: string }) {
  const { data, error, loading } = useData<Settings>("/settings");
  const [tab, setTab] = useState("capabilities"),
    [selected, setSelected] = useState<string | null>(null);
  return (
    <>
      <ErrorBox message={error} />
      <ModelSelection showManagerSettings={section === "manager"} />
      <div className="tabs" role="tablist" aria-label="资源类别">
        {[
          ["capabilities", "执行能力"],
          ["prompts", "提示词模板"],
          ["knowledge", "知识储备"],
          ["skills", "技能方法"],
        ].map(([id, label]) => (
          <button
            key={id}
            role="tab"
            aria-selected={tab === id}
            className={tab === id ? "active" : ""}
            onClick={() => {
              setTab(id);
              setSelected(null);
            }}
          >
            {label}
            {(id === "capabilities" ? data?.capabilities.length : data?.resources[id]) !== undefined && (
              <small>{id === "capabilities" ? data?.capabilities.length : data?.resources[id]}</small>
            )}
          </button>
        ))}
      </div>
      {tab === "capabilities" ? (
        loading ? (
          <Loading />
        ) : (
          <CapabilityCards items={data?.capabilities ?? []} />
        )
      ) : (
        <ResourceList kind={tab} selected={selected} onSelect={setSelected} />
      )}
    </>
  );
}
function ResourceList({
  kind,
  selected,
  onSelect,
}: {
  kind: string;
  selected: string | null;
  onSelect: (id: string) => void;
}) {
  const { data, error, loading } = useData<Page<Resource>>(
    `/resources/${kind}`,
  );
  const active = data?.items.find((r) => r.id === selected) ?? data?.items[0];
  return (
    <>
      <ErrorBox message={error} />
      {loading ? (
        <Loading />
      ) : data?.items.length ? (
        <div className="resource-layout">
          <div className="resource-index">
            {data.items.map((r) => (
              <button
                key={r.id}
                className={active?.id === r.id ? "active" : ""}
                onClick={() => onSelect(r.id)}
              >
                <strong>{r.name}</strong>
                <small>
                  {r.id} · v{r.version}
                </small>
              </button>
            ))}
          </div>
          <article className="panel resource-content">
            <div className="panel-heading">
              <h2>{active?.name}</h2>
              <span className="pill">v{active?.version}</span>
            </div>
            <pre>{active?.content}</pre>
          </article>
        </div>
      ) : (
        <Empty title="尚无此类资源" />
      )}
    </>
  );
}
