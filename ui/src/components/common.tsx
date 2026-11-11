import { useEffect, useId, useRef } from "react";
import type { ReactNode } from "react";
import {
  AlertCircle,
  ArrowUpRight,
  FileVideo,
  LoaderCircle,
  X,
} from "lucide-react";
import { labels, time } from "../lib/types";
import type { Task } from "../lib/types";

export function Status({ value }: { value: string }) {
  return (
    <span className={`status status-${value}`}>
      <i />
      {labels[value] ?? value}
    </span>
  );
}
export function ErrorBox({ message }: { message: string }) {
  return message ? (
    <div className="error-box" role="alert">
      <AlertCircle size={17} />
      <span>{message}</span>
    </div>
  ) : null;
}
export function Loading() {
  return (
    <div className="empty">
      <LoaderCircle className="spin" size={22} />
      <p>正在读取项目数据…</p>
    </div>
  );
}
export function Empty({
  title,
  children,
}: {
  title: string;
  children?: ReactNode;
}) {
  return (
    <div className="empty">
      <FileVideo size={32} strokeWidth={1.25} />
      <h3>{title}</h3>
      {children && <p>{children}</p>}
    </div>
  );
}
export function PageHead({
  eyebrow,
  title,
  children,
  action,
}: {
  eyebrow: string;
  title: string;
  children?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <header className="page-head">
      <div>
        {eyebrow && <div className="eyebrow">{eyebrow}</div>}
        <h1>{title}</h1>
        {children && <p>{children}</p>}
      </div>
      {action && <div className="actions">{action}</div>}
    </header>
  );
}
export function TaskList({
  tasks,
  onSelect,
}: {
  tasks: Task[];
  onSelect: (id: string) => void;
}) {
  return (
    <div className="task-list">
      {tasks.map((task) => {
        const row = (
        <button
          className="task-row"
          key={task.id}
          onClick={() => onSelect(task.id)}
        >
          <div className="task-icon">
            <FileVideo size={22} />
          </div>
          <div className="task-main">
            <strong>{task.title}</strong>
            <small>
              {task.asset_ids.length} 个原片 <span>·</span>{" "}
              {task.target_locales.join(" / ")} <span>·</span>{" "}
              {time(task.created_at)}
            </small>
            <span className="current-node">
              {task.current_nodes.join("、") || "查看流程与产物"}
            </span>
          </div>
          <div className="task-progress">
            <span>
              {task.progress.done}
              <small> / {task.progress.total} 节点</small>
            </span>
            <progress value={task.progress.done} max={task.progress.total} />
          </div>
          <Status value={task.status} />
          <ArrowUpRight size={17} />
        </button>
        );
        return row;
      })}
    </div>
  );
}
export function Modal({
  title,
  onClose,
  children,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  useEffect(() => {
    dialog.current?.showModal();
    return () => dialog.current?.close();
  }, []);
  return (
    <dialog
      ref={dialog}
      aria-labelledby={titleId}
      onCancel={(event) => { event.preventDefault(); onClose(); }}
      onClick={(event) => {
        if (event.target === dialog.current) onClose();
      }}
    >
      <div className="modal-head">
        <h2 id={titleId}>{title}</h2>
        <button className="icon-button" onClick={onClose} aria-label="关闭弹窗">
          <X size={20} />
        </button>
      </div>
      {children}
    </dialog>
  );
}
export function JsonView({ value }: { value: unknown }) {
  return <pre className="json-view">{JSON.stringify(value, null, 2)}</pre>;
}
