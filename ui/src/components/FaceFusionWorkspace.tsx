import { useRef, useState } from "react";
import { api } from "../lib/api";
import type { FacePreview, Task } from "../lib/types";
import { ErrorBox } from "./common";
import { defaultFaceProfile, FaceFusionFields, faceStates } from "./FaceFusionFields";

export function FaceFusionWorkspace({ task, refresh }: { task: Task; refresh: () => void }) {
  const [profile, setProfile] = useState(() => ({ ...defaultFaceProfile(), ...task.facefusion }));
  const [preview, setPreview] = useState<FacePreview | null>(task.design_workspace?.target_preview ?? null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [watched, setWatched] = useState("");
  const inFlight = useRef(false);
  const configuration = useRef<{ body: string; request_id: string } | null>(null);
  const execution = task.external_execution;
  const video = task.assets?.find(asset => asset.kind === "video");
  const face = task.assets?.find(asset => asset.kind === "face_source");
  const result = task.artifacts?.find(artifact => artifact.id === execution?.artifact_id);
  const editable = !["pending", "running", "unknown", "approved"].includes(execution?.state ?? "")
    && task.status !== "cancelled";
  const run = async (action: () => Promise<void>) => {
    if (inFlight.current) return;
    inFlight.current = true; setBusy(true); setError("");
    try { await action(); }
    catch (cause) { setError(cause instanceof Error ? cause.message : "FaceFusion 操作失败"); }
    finally { inFlight.current = false; setBusy(false); }
  };
  return <section className="panel designer-workspace" aria-label="视频换脸工作区">
    <div className="panel-heading"><h2>FaceFusion 视频换脸</h2>
      <span role="status">{faceStates[execution?.state ?? "configuration_required"] ?? "待核对外接状态"}</span>
    </div>
    <ol className="design-workflow-steps" aria-label="换脸处理示意">
      {(task.design_workspace?.display_steps ?? ["选择目标人物", "FaceFusion 处理", "完整观看结果"]).map(step =>
        <li key={step}>{step}</li>)}
    </ol>
    <div className="design-inputs">
      {video && <figure><video controls preload="metadata" src={video.content_url} /><figcaption>原视频 · {video.name}</figcaption></figure>}
      {face && <figure><img src={face.content_url} alt={`来源脸：${face.name}`} /><figcaption>来源脸 · {face.name}</figcaption></figure>}
    </div>
    {task.notes && <p className="intake-remote-text">目标人物说明：{task.notes}</p>}
    <div className="design-profile-line">
      <span>模式：{task.facefusion?.face_selector_mode ?? "reference"}</span>
      <span>排序：{task.facefusion?.face_selector_order ?? "large-small"}</span>
      {task.facefusion?.face_selector_mode === "reference" && <>
        <span>参考帧：{task.facefusion.reference_frame_number}</span>
        <span>目标脸：{task.facefusion.reference_face_position}</span>
        <span>匹配阈值：{task.facefusion.reference_face_distance}</span>
      </>}
      <span>保留原音轨</span>
    </div>
    <ErrorBox message={execution?.error ?? ""} />
    {editable && <form className="form-stack" onSubmit={event => {
      event.preventDefault();
      void run(async () => {
        const body = JSON.stringify({ profile, expected_run_id: execution?.id ?? null });
        if (configuration.current?.body !== body) {
          configuration.current = { body, request_id: crypto.randomUUID() };
        }
        await api(`/tasks/${task.id}/facefusion`, { method: "PUT", body: JSON.stringify({
          ...JSON.parse(body), request_id: configuration.current.request_id,
        }) });
        refresh();
      });
    }}>
      <FaceFusionFields profile={profile} onChange={setProfile} busy={busy} preview={preview}
        onDetect={() => void run(async () => {
          const detected = await api<FacePreview>("/facefusion/target-preview", {
            method: "POST", body: JSON.stringify({ asset_id: video?.id, profile }),
          });
          setPreview(detected);
          setProfile(current => ({ ...current, target_preview_id: detected.id,
            reference_face_position: detected.faces[0]?.position ?? 0 }));
        })} />
      <button className="primary" disabled={busy || profile.face_selector_mode === "reference"
        && (!profile.target_preview_id || !preview?.faces.length)}>
        {busy ? "正在处理…" : "按当前人物和参数运行 FaceFusion"}
      </button>
    </form>}
    {result && <div className="face-result">
      <h3>{execution?.state === "approved" ? "已验收视频" : "换脸候选"}</h3>
      <video key={result.sha256} controls preload="metadata" src={result.content_url} />
      <small className="hash">SHA256 {result.sha256}</small>
      <a href={`${result.content_url}?download=true`} download>下载换脸视频</a>
      {execution?.state === "succeeded" && <div className="form-stack">
        <label className="source-confirm"><input type="checkbox" checked={watched === result.sha256}
          onChange={event => setWatched(event.target.checked ? result.sha256 : "")} />
          我已完整观看当前视频，确认目标人物、换脸效果和声音
        </label>
        <button className="primary" disabled={busy || watched !== result.sha256} onClick={() => void run(async () => {
          await api(`/tasks/${task.id}/facefusion/approve`, {
            method: "POST", body: JSON.stringify({ sha256: result.sha256 }),
          });
          refresh();
        })}>批准当前版本</button>
      </div>}
    </div>}
    <ErrorBox message={error} />
  </section>;
}
