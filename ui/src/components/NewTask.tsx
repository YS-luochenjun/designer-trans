import { useRef, useState } from "react";
import type { FormEvent } from "react";
import { api } from "../lib/api";
import type { Asset, Task } from "../lib/types";
import { ErrorBox, Modal } from "./common";
import { FireplayIntake } from "./FireplayIntake";
import { DesignIntake } from "./DesignIntake";
import { taskKinds } from "../lib/taskKinds";
import type { TaskKind } from "../lib/taskKinds";
import { initialIntakeOptions, intakePayload, IntakeFiles, TranslationIntakeFields } from "./TranslationIntakeFields";
import "./NewTaskIntake.css";

type IntakeProps = {
  onClose: () => void;
  onCreated: (id: string) => void;
  onBusyChange: (busy: boolean) => void;
};

function ManualIntake({ onClose, onCreated, onBusyChange }: IntakeProps) {
  const [files, setFiles] = useState<File[]>([]);
  const [options, setOptions] = useState(() => initialIntakeOptions("ja-JP"));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [step, setStep] = useState("");
  const submitting = useRef(false);
  const uploaded = useRef(new Map<File, string>());
  const creation = useRef<{ body: string; id: string } | null>(null);
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (submitting.current) return;
    const payload = intakePayload(options);
    if (!files.length || !payload.target_locales.length) {
      setError(!files.length ? "请先选择原片" : "请填写目标语言");
      return;
    }
    submitting.current = true;
    setBusy(true);
    onBusyChange(true);
    setError("");
    try {
      const ids: string[] = [];
      for (const [i, file] of files.entries()) {
        setStep(`上传原片 ${i + 1} / ${files.length}`);
        let id = uploaded.current.get(file);
        if (!id) {
          const form = new FormData();
          form.append("file", file);
          id = (await api<Asset>("/assets", { method: "POST", body: form })).id;
          uploaded.current.set(file, id);
        }
        ids.push(id);
      }
      setStep("创建翻译任务");
      const body = JSON.stringify({ ...payload, title: payload.title || files[0].name, asset_ids: ids });
      if (creation.current?.body !== body) {
        creation.current = { body, id: crypto.randomUUID() };
      }
      const task = await api<Task>("/tasks", {
        method: "POST",
        body: JSON.stringify({ ...JSON.parse(body), creation_request_id: creation.current.id }),
      });
      onCreated(task.id);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "任务创建失败");
    } finally {
      submitting.current = false;
      setBusy(false);
      onBusyChange(false);
    }
  };
  return <form onSubmit={submit} className="form-stack" aria-busy={busy}>
    <IntakeFiles files={files} onChange={setFiles} busy={busy} />
    <TranslationIntakeFields value={options} onChange={setOptions} busy={busy} />
    <ErrorBox message={error} />
    <div className="modal-actions">
      <button type="button" className="secondary" onClick={onClose} disabled={busy}>取消</button>
      <button className="primary" disabled={busy}>{busy ? step : "开始翻译"}</button>
    </div>
  </form>;
}

export function NewTask({ onClose, onCreated }: {
  onClose: () => void;
  onCreated: (id: string) => void;
}) {
  const [mode, setMode] = useState<"manual" | "fireplay">("manual");
  const [kind, setKind] = useState<TaskKind>("video_translation");
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const changeBusy = (value: boolean) => {
    busyRef.current = value;
    setBusy(value);
  };
  const close = () => { if (!busyRef.current) onClose(); };
  return <Modal title="新建任务" onClose={close}>
    <div className="new-task-intake" onKeyDownCapture={(event) => {
      if (event.key === "Escape" && busyRef.current) event.preventDefault();
    }}>
      <div className="intake-modes" role="group" aria-label="新建任务入口">
        <button type="button" className={mode === "manual" ? "primary" : "secondary"}
          aria-pressed={mode === "manual"} disabled={busy}
          onClick={() => { if (!busyRef.current) setMode("manual"); }}>上传素材</button>
        <button type="button" className={mode === "fireplay" ? "primary" : "secondary"}
          aria-pressed={mode === "fireplay"} disabled={busy}
          onClick={() => { if (!busyRef.current) { setMode("fireplay"); if (kind === "face_swap") setKind("video_translation"); } }}>Fireplay 需求号</button>
      </div>
      {mode === "manual" && <div className="intake-kinds" role="group" aria-label="任务类型">
        {taskKinds.map(option => <button type="button" key={option.id} disabled={busy}
          className={kind === option.id ? "primary" : "secondary"} aria-pressed={kind === option.id}
          onClick={() => { if (!busyRef.current) setKind(option.id); }}>{option.label}</button>)}
      </div>}
      <div hidden={mode !== "manual"}>
        {kind === "video_translation"
          ? <ManualIntake onClose={close} onCreated={onCreated} onBusyChange={changeBusy} />
          : <DesignIntake key={kind} kind={kind} onClose={close} onCreated={onCreated} onBusyChange={changeBusy} />}
      </div>
      <div hidden={mode !== "fireplay"}>
        <p className="intake-stage-note">读取需求后，根据“AI承接交付”自动选择图片翻译或视频翻译并填写制作信息。</p>
        <FireplayIntake onClose={close} onCreated={onCreated} onBusyChange={changeBusy} />
      </div>
    </div>
  </Modal>;
}
