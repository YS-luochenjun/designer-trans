import { useEffect, useState } from "react";
import {
  CalendarClock,
  Clapperboard,
  FolderClosed,
  GalleryVerticalEnd,
  LayoutDashboard,
  Menu,
  Network,
  PanelLeftClose,
  Settings2,
  X,
} from "lucide-react";
import { useData } from "./lib/api";
import { NewTask } from "./components/NewTask";
import {
  Dashboard,
  Deliveries,
  Resources,
  Tasks,
} from "./pages/WorkspacePages";
import { TaskDetail } from "./pages/TaskDetail";
import { Schedules } from "./pages/Schedules";
import { FireplayRequirements as Fireplay, FireplayRequirementDetail } from "./pages/FireplayRequirements";

const navigation = [
  { id: "overview", label: "首页", icon: LayoutDashboard },
  { id: "tasks", label: "任务中心", icon: Network },
  { id: "fireplay", label: "需求中心", icon: FolderClosed },
  { id: "deliveries", label: "成品库", icon: GalleryVerticalEnd },
  { id: "schedules", label: "自动化", icon: CalendarClock },
];
function readRoute() {
  const current = location.hash.slice(1) || "overview";
  const destination = current === "management/assistant" || current === "management/admin" ? "overview"
    : current === "management" || current === "management/architecture" ? "resources/developer" : current;
  if (current !== destination) history.replaceState(null, "", `#${destination}`);
  return destination;
}
export default function App() {
  const [route, setRoute] = useState(readRoute),
    [creating, setCreating] = useState(false),
    [mobile, setMobile] = useState(false);
  const health = useData<{ status: string; worker_running: boolean; features?: string[]; manager_protocol_version?: string }>(
    "/health",
    5000,
  );
  useEffect(() => {
    const change = () => {
      setRoute(readRoute());
      setMobile(false);
    };
    window.addEventListener("hashchange", change);
    return () => window.removeEventListener("hashchange", change);
  }, []);
  const go = (destination: string) => {
      location.hash = destination;
      setMobile(false);
    },
    onTask = (id: string) => go(`tasks/${id}`),
    onNew = () => setCreating(true);
  const active = route.split("/")[0];
  return (
    <div className="app-shell">
      <a
        href="#main-content"
        className="skip-link"
        onClick={(e) => {
          e.preventDefault();
          document.getElementById("main-content")?.focus();
        }}
      >
        跳转主内容
      </a>
      <aside className={`sidebar ${mobile ? "open" : ""}`}>
        <div className="brand">
          <div className="brand-mark">
            <Clapperboard size={22} />
          </div>
          <div>
            <strong>视频翻译</strong>

          </div>
          <button
            className="mobile-only icon-button"
            onClick={() => setMobile(false)}
            aria-label="关闭导航"
          >
            <X size={20} />
          </button>
        </div>

        <nav aria-label="主导航">
          {navigation.map(({ id, label, icon: Icon }) => (
            <a
              key={id}
              href={`#${id}`}
              aria-current={active === id ? "page" : undefined}
              className={active === id ? "active" : ""}
            >
              <Icon size={18} />
              <span>{label}</span>
            </a>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <nav aria-label="偏好与配置">
            <a href="#resources" aria-current={active === "resources" ? "page" : undefined}
              className={active === "resources" ? "active" : ""}><Settings2 size={18} /><span>设置</span></a>
          </nav>
          <div className="runtime-state">
            <i className={health.data?.worker_running ? "online" : ""} />
            <span>
              {health.error
                ? "后台连接中断"
                : health.data?.worker_running
                  ? "本机执行器在线"
                  : "执行器未运行"}
            </span>
          </div>

        </div>
      </aside>
      {mobile && (
        <button
          className="nav-overlay"
          aria-label="收起导航"
          onClick={() => setMobile(false)}
        />
      )}
      <div className="main-shell">
        <div className="topbar">
          <div className="topbar-left">
            <button
              className="icon-button mobile-only"
              aria-label="打开导航"
              onClick={() => setMobile(true)}
            >
              <Menu size={20} />
            </button>
            <PanelLeftClose size={17} className="desktop-only" />
            <span>视频翻译 v2</span>
            <span className="separator">/</span>
            <strong>
              {active === "resources" ? "设置" : navigation.find((n) => n.id === active)?.label ?? "首页"}
            </strong>
          </div>
        </div>
        <main id="main-content" tabIndex={-1} key={route}>
          {health.data && !health.data.features?.includes("node_conversations") &&
            <p className="scope-banner" role="note">后台仍是旧版本。请先停止原服务再启动；仅刷新页面不能启用节点对话等新功能。</p>}
          {(active === "overview" || active === "schedules" || active === "resources") && health.data && health.data.manager_protocol_version !== "3" &&
            <p className="scope-banner" role="note">后台尚未加载本轮完整管理功能。请在当前制作到达安全边界后重启原服务；刷新页面仅更新界面。</p>}
          {route.startsWith("tasks/") ? (
            <TaskDetail id={route.split("/")[1]} onBack={() => go("tasks")} />
          ) : active === "tasks" ? (
            <Tasks onTask={onTask} onNew={onNew} />
          ) : active === "schedules" ? (
            <Schedules />
          ) : active === "deliveries" ? (
            <Deliveries onTask={onTask} />
          ) : route.startsWith("fireplay/") ? (
            <FireplayRequirementDetail id={route.split("/")[1]} />
          ) : active === "fireplay" ? (
            <Fireplay />
          ) : active === "resources" ? (
            <Resources section={route.split("/")[1]} view={route.split("/")[2]} />
          ) : (
            <Dashboard />
          )}
        </main>
        <footer className="app-footer">
          <span>视频翻译 v2</span>
        </footer>
      </div>
      {creating && (
        <NewTask
          onClose={() => setCreating(false)}
          onCreated={(id) => {
            setCreating(false);
            onTask(id);
          }}
        />
      )}
    </div>
  );
}
