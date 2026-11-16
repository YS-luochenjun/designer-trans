import { useRef, useState } from "react";
import type { FormEvent } from "react";
import { api } from "../lib/api";
import type { Asset, Task, FacePreview } from "../lib/types";
import type { TaskKind } from "../lib/taskKinds";
import { ErrorBox } from "./common";
import { initialIntakeOptions, intakeLocales, intakePayload, IntakeFiles, TranslationIntakeFields } from "./TranslationIntakeFields";
import { defaultFaceProfile, FaceFusionFields } from "./FaceFusionFields";

export function DesignIntake({ kind, onCreated, onClose, onBusyChange }: {
  kind: Exclude<TaskKind, "video_translation">;
  onCreated: (id: string) => void;
  onClose: () => void;
  onBusyChange: (busy: boolean) => void;
}) {
  const [files, setFiles] = useState<File[]>([]);
  const [faces, setFaces] = useState<File[]>([]);
  const [options, setOptions] = useState(() => initialIntakeOptions("en"));
  const [profile, setProfile] = useState(defaultFaceProfile);
  const [preview, setPreview] = useState<FacePreview | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [step, setStep] = useState("");
  const submitted = useRef(false);
  const uploaded = useRef(new Map<File, string>());
  const creation = useRef<{ body: string; id: string } | null>(null);
  const image = kind === "image_translation";
  const upload = async (file: File, role: string) => {
    let id = uploaded.current.get(file);
    if (!id) {
      const body = new FormData(); body.append("file", file);
      id = (await api<Asset>(`/assets?kind=${role}`, { method: "POST", body })).id;
      uploaded.current.set(file, id);
    }
    return id;
  };
  const detect = async () => {
    if (submitted.current) return;
    if (files.length !== 1) { setError("请先选择一个原视频。"); return; }
    submitted.current = true;
    setBusy(true); onBusyChange(true); setError(""); setStep("检测参考帧中的目标脸");
    try {
      const id = await upload(files[0], "video");
      const result = await api<FacePreview>("/facefusion/target-preview", {
        method: "POST", body: JSON.stringify({ asset_id: id, profile }),
      });
      setPreview(result);
      setProfile(current => ({ ...current, target_preview_id: result.id,
        reference_face_position: result.faces[0]?.position ?? 0 }));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "目标脸检测失败");
    } finally {
      submitted.current = false; setBusy(false); onBusyChange(false);
    }
  };
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (submitted.current) return;
    if (!files.length || image && !intakeLocales(options.languages).length || !image && (files.length !== 1 || faces.length !== 1)) {
      setError(image ? "请选择来源图片和至少一种目标语言。" : "请选择一个原视频和一张来源脸图片。");
      return;
    }
    if (!image && profile.face_selector_mode === "reference" &&
        (!profile.target_preview_id || !preview?.faces.some(face => face.position === profile.reference_face_position))) {
      setError("请检测参考帧并选择真实目标脸后开始换脸。"); return;
    }
    submitted.current = true;
    setBusy(true); onBusyChange(true); setError("");
    try {
      const ids = [];
      const inputs = [...files.map(file => ({ file, role: image ? "image" : "video" })),
        ...faces.map(file => ({ file, role: "face_source" }))];
      for (const [index, { file, role }] of inputs.entries()) {
        setStep(`上传素材 ${index + 1} / ${inputs.length}`);
        ids.push(await upload(file, role));
      }
      setStep(image ? "创建图片翻译任务" : "创建 FaceFusion 任务");
      const body = JSON.stringify({
          ...(image ? intakePayload(options) : { notes: options.notes }),
          title: options.title.trim() || files[0].name, task_kind: kind,
          asset_ids: ids, target_locales: image ? intakeLocales(options.languages) : [],
          design_profile: { workflow_id: kind },
          ...(!image ? { facefusion: profile } : {}),
        });
      if (!creation.current || creation.current.body !== body) {
        creation.current = { body, id: crypto.randomUUID() };
      }
      const task = await api<Task>("/tasks", {
        method: "POST", body: JSON.stringify({ ...JSON.parse(body), creation_request_id: creation.current.id }),
      });
      onCreated(task.id);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "保存失败");
    } finally {
      submitted.current = false; setBusy(false); onBusyChange(false);
    }
  };
  return <form className="form-stack design-intake" onSubmit={submit} aria-busy={busy}>
    <p className="intake-stage-note" role="status">
      {image ? "Route 与 Work 按来源图和语言制作，逐张审核后生成语言包。" :
        "FaceFusion 按所选人物和参数处理完整视频，保留原音轨。"}
    </p>
    <IntakeFiles files={files} onChange={value => {
      setFiles(value); setPreview(null);
      setProfile(current => ({ ...current, target_preview_id: null }));
    }} busy={busy}
      accept={image ? ".png,.jpg,.jpeg,.webp,.bmp" : "video/*,.mkv"}
      label={image ? "选择来源图片" : "选择原视频"}
      hint={image ? "PNG、JPEG、WebP、BMP · 保留实际来源集合" : undefined}
      multiple={image} />
    {!image && <IntakeFiles files={faces} onChange={setFaces} busy={busy}
      accept=".png,.jpg,.jpeg,.webp,.bmp" label="选择来源脸图片" hint="一张清晰的来源脸图片" multiple={false} />}
    {image ? <>
      <TranslationIntakeFields value={options} onChange={setOptions} busy={busy} image />
      <p className="muted">{files.length} 张来源图 × {intakeLocales(options.languages).length} 种语言</p>
    </> : <>
      <label>任务名称<input value={options.title} maxLength={200} disabled={busy}
        onChange={event => setOptions(current => ({ ...current, title: event.target.value }))} placeholder="留空使用素材名称" /></label>
      <label>目标人物说明<textarea value={options.notes} rows={2} maxLength={8000} disabled={busy}
        onChange={event => setOptions(current => ({ ...current, notes: event.target.value }))}
        placeholder="例如：替换画面左侧的人物。实际选脸以参考帧与所选模式为准。" /></label>
      <FaceFusionFields profile={profile} onChange={setProfile} busy={busy} preview={preview}
        onDetect={() => void detect()} />
    </>}
    <ErrorBox message={error} />
    <div className="modal-actions">
      <button type="button" className="secondary" disabled={busy} onClick={onClose}>取消</button>
      <button className="primary" disabled={busy}>{busy ? step : image ? "开始图片翻译" : "开始 FaceFusion 换脸"}</button>
    </div>
  </form>;
}
