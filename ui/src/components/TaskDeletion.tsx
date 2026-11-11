import { useRef, useState } from "react";
import { api, useData } from "../lib/api";
import type { Task } from "../lib/types";
import { bytes } from "../lib/types";
import { ErrorBox, Loading, Modal } from "./common";
import "./TaskDeletion.css";

export interface DeletionPreview {
  can_delete: boolean;
  blockers: string[];
  fingerprint: string;
  file_count: number;
  bytes: number;
  includes_final_outputs: boolean;
  shared_assets: { id: string; references: unknown[] }[];
  retained_external_receipts?: number;
  preserved_files: { id: string; reason: string }[];
}
export type PendingDeletion = { task: Pick<Task, "id" | "title">; preview: DeletionPreview; error?: string };
const storageKey = "workbench.pending-task-deletion";

export function readPendingDeletion(): PendingDeletion | null {
  try {
    const value = JSON.parse(sessionStorage.getItem(storageKey) ?? "null");
    return value?.task?.id && /^[a-f0-9]{64}$/.test(value?.preview?.fingerprint ?? "") ? value : null;
  } catch { return null; }
}

export function rememberPendingDeletion(value: PendingDeletion | null) {
  try {
    if (value) sessionStorage.setItem(storageKey, JSON.stringify(value));
    else sessionStorage.removeItem(storageKey);
  } catch { /* The parent retains the same receipt for this page session. */ }
}

export function TaskDeletion({ task, pending, onPending, onDeleted, onClose }: {
  task: Pick<Task, "id" | "title">;
  pending: PendingDeletion | null;
  onPending: (value: PendingDeletion | null) => void;
  onDeleted: () => void;
  onClose: () => void;
}) {
  // Mount the preview reader only before a deletion attempt; a pending cleanup must
  // continue with its original fingerprint, even after the task record is gone.
  return pending ? <DeletionDialog task={task} preview={pending.preview} error={pending.error} retry onPending={onPending} onDeleted={onDeleted} onClose={onClose} />
    : <DeletionReader task={task} onPending={onPending} onDeleted={onDeleted} onClose={onClose} />;
}

function DeletionReader(props: Omit<Parameters<typeof TaskDeletion>[0], "pending">) {
  const { data, error, loading, reload } = useData<DeletionPreview>(`/tasks/${encodeURIComponent(props.task.id)}/deletion-preview`);
  const message = error === "Not Found"
    ? "当前后台尚未加载删除接口，请重启工作台服务后重试。任务尚未删除。"
    : error;
  return <DeletionDialog {...props} preview={data} error={message} loading={loading} reload={reload} />;
}

function DeletionDialog({ task, preview, error = "", loading = false, reload, retry = false, onPending, onDeleted, onClose }: {
  task: Pick<Task, "id" | "title">;
  preview: DeletionPreview | null;
  error?: string;
  loading?: boolean;
  reload?: () => void;
  retry?: boolean;
  onPending: (value: PendingDeletion | null) => void;
  onDeleted: () => void;
  onClose: () => void;
}) {
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState("");
  const [stalePreview, setStalePreview] = useState(error === "删除预览已变化，请重新预览");
  const inFlight = useRef(false);
  const remove = async () => {
    if (inFlight.current || stalePreview || !preview?.can_delete || (!confirmed && !retry)) return;
    inFlight.current = true;
    setBusy(true);
    setActionError("");
    const receipt = { task, preview };
    // Save before sending: network failure may follow a successful server mutation.
    rememberPendingDeletion(receipt);
    try {
      const result = await api<{ deleted: boolean; cleanup_pending: boolean }>(
        `/tasks/${encodeURIComponent(task.id)}?fingerprint=${encodeURIComponent(preview.fingerprint)}`, { method: "DELETE" },
      );
      if (!result.deleted) throw new Error("删除未完成，请使用相同预览重试。");
      if (result.cleanup_pending) onPending(receipt);
      else {
        rememberPendingDeletion(null);
        onDeleted();
      }
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : "删除结果尚未确认，请重试相同删除请求。";
      if (message === "删除预览已变化，请重新预览") {
        setStalePreview(true);
        setConfirmed(false);
      }
      rememberPendingDeletion({ ...receipt, error: message });
      onPending({ ...receipt, error: message });
      setActionError(message);
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  };
  return <Modal title={retry ? "继续完成删除" : "删除任务"} onClose={() => { if (!inFlight.current) onClose(); }}>
    <div className="form-stack task-deletion" aria-busy={busy}>
      <strong className="deletion-title">{task.title}</strong>
      <ErrorBox message={actionError || error} />
      {loading ? <Loading /> : preview && <>
        <p>将删除此任务的本地记录与独占文件，包含已生成的成品，无法撤销。</p>
        <div className="deletion-facts"><span>文件<strong>{preview.file_count} 个</strong></span><span>空间<strong>{bytes(preview.bytes)}</strong></span></div>
        {preview.shared_assets.length > 0 && <p>保留 {preview.shared_assets.length} 项被其他任务或计划引用的共享素材。</p>}
        {preview.preserved_files.length > 0 && <details className="help-disclosure"><summary>保留文件（{preview.preserved_files.length}）</summary><ul>{preview.preserved_files.map((file, index) => <li key={`${file.id}-${index}`}>{file.id}：{({ external_or_unowned_input: "外部或非独占原片", external_or_unowned_output: "外部或非独占产物" } as Record<string, string>)[file.reason] ?? file.reason}</li>)}</ul></details>}
        {!preview.can_delete && <div role="alert" className="deletion-blockers"><strong>当前不能删除</strong><ul>{preview.blockers.map((blocker) => <li key={blocker}>{blocker}</li>)}</ul></div>}
        {preview.retained_external_receipts ? <p>保留 {preview.retained_external_receipts} 项已结束调用的费用与失败回执。</p> : null}
        {retry && <p role="status">上次删除尚未全部确认。重试沿用原来的删除范围；任务记录已删除时，只继续剩余文件清理。</p>}
        {preview.can_delete && !retry && <label className="source-confirm"><input type="checkbox" checked={confirmed} disabled={busy} onChange={(event) => setConfirmed(event.target.checked)} />我确认删除任务及以上文件（含成品）</label>}
      </>}
      <div className="modal-actions">
        <button type="button" className="secondary" disabled={busy} onClick={onClose}>{retry ? "稍后重试" : "取消"}</button>
        {(stalePreview || (reload && !preview?.can_delete)) && <button type="button" className="secondary" disabled={busy || loading} onClick={() => {
          if (stalePreview) { rememberPendingDeletion(null); onPending(null); setStalePreview(false); setActionError(""); }
          reload?.();
        }}>刷新删除预览</button>}
        <button type="button" className="secondary danger" disabled={busy || loading || stalePreview || !preview?.can_delete || (!confirmed && !retry)} onClick={() => void remove()}>{busy ? "正在删除…" : retry ? "重试相同删除" : "确认删除"}</button>
      </div>
    </div>
  </Modal>;
}
