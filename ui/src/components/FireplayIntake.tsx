import { useRef, useState } from "react";
import type { FormEvent } from "react";
import { api } from "../lib/api";
import type { Asset, Task } from "../lib/types";
import { ErrorBox } from "./common";
import { initialIntakeOptions, intakeLocales, intakePayload, IntakeFiles, TranslationIntakeFields } from "./TranslationIntakeFields";

const sourceKindLabels: Record<string, string> = {
  library_video: "Fireplay素材库原视频",
  reference_video: "视频直链",
  youtube: "YouTube引用",
  unsupported: "其他引用",
};

interface FireplayPreview {
  preview_id: string;
  requirement: {
    id: string;
    code: string;
    title: string;
    description: string;
    ref_text: string;
    locales: string[];
  };
  sources: { id: string; name: string; kind: string; downloadable: boolean; reason?: string }[];
  suggested_target_locales: string[];
  notes: string;
  warnings: string[];
  needs_source_upload: boolean;
}

type CreatePayload = ReturnType<typeof intakePayload> & {
  preview_id: string;
  source_ids: string[];
  asset_ids: string[];
  idempotency_key: string;
  confirm_sources: boolean;
};

export function FireplayIntake({ onClose, onCreated, onBusyChange, managed = false, initialReference = "" }: {
  onClose: () => void;
  onCreated: (id: string) => void;
  onBusyChange: (busy: boolean) => void;
  managed?: boolean;
  initialReference?: string;
}) {
  const [reference, setReference] = useState(initialReference);
  const [preview, setPreview] = useState<FireplayPreview | null>(null);
  const [sourceIds, setSourceIds] = useState<string[]>([]);
  const [confirmSources, setConfirmSources] = useState(false);
  const [files, setFiles] = useState<File[]>([]);
  const [options, setOptions] = useState(initialIntakeOptions);
  const [previewing, setPreviewing] = useState(false);
  const [creating, setCreating] = useState(false);
  const [submitted, setSubmitted] = useState(false);
  const [error, setError] = useState("");
  const [step, setStep] = useState("");
  const [kieLimit, setKieLimit] = useState(0);
  const [previewRevision, setPreviewRevision] = useState(0);
  const operation = useRef<"preview" | "create" | null>(null);
  const revision = useRef(0);
  const currentPreview = useRef<FireplayPreview | null>(null);
  const idempotencyKey = useRef("");
  const submittedPayload = useRef<CreatePayload | null>(null);
  const uploaded = useRef(new Map<File, string>());
  const busy = previewing || creating;

  const invalidatePreview = () => {
    revision.current += 1;
    currentPreview.current = null;
    setPreview(null);
    setSourceIds([]);
    setConfirmSources(false);
    setFiles([]);
    setOptions(initialIntakeOptions());
    setSubmitted(false);
    submittedPayload.current = null;
    uploaded.current.clear();
    idempotencyKey.current = "";
    setPreviewRevision(revision.current);
    setError("");
  };

  const loadPreview = async (event: FormEvent) => {
    event.preventDefault();
    if (operation.current) return;
    if (!reference.trim()) {
      setError("请填写 Fireplay 需求号");
      return;
    }
    invalidatePreview();
    const requestedRevision = revision.current;
    operation.current = "preview";
    setPreviewing(true);
    onBusyChange(true);
    try {
      const result = await api<FireplayPreview>("/fireplay/intake/preview", {
        method: "POST", body: JSON.stringify({ reference: reference.trim() }),
      });
      // Edits to the reference invalidate even an in-flight preview response.
      if (requestedRevision !== revision.current) return;
      currentPreview.current = result;
      idempotencyKey.current = crypto.randomUUID();
      setPreview(result);
      setSourceIds([]);
      const locales = result.requirement.locales.length
        ? result.requirement.locales : result.suggested_target_locales;
      setOptions({
        ...initialIntakeOptions(locales.join(", ")),
        title: result.requirement.title,
        notes: result.notes,
      });
    } catch (cause) {
      if (requestedRevision === revision.current) {
        setError(cause instanceof Error ? cause.message : "需求预览失败");
      }
    } finally {
      operation.current = null;
      setPreviewing(false);
      onBusyChange(false);
    }
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (operation.current) return;
    const active = currentPreview.current;
    if (!active || active !== preview) {
      setError("请先查询并检查当前需求预览");
      return;
    }
    const selectedIds = [...new Set(active.sources
      .filter((source) => source.downloadable && sourceIds.includes(source.id))
      .map((source) => source.id))];
    const fields = intakePayload(options);
    if (active.requirement.locales.length) fields.target_locales = [...new Set(active.requirement.locales)];
    if (selectedIds.length && !confirmSources) {
      setError("请确认所选需求引用就是本次待翻译原片");
      return;
    }
    if (!fields.title || !fields.target_locales.length || (!selectedIds.length && !files.length)) {
      setError(!fields.title ? "请填写任务名称" : !fields.target_locales.length
        ? "需求未提供目标语言，请补充后再创建" : "请勾选可下载原片，或补传本地视频");
      return;
    }
    if (managed && (!options.confirmBrief || [options.calls, options.imageCalls, options.videoCredits].some(v => v === null))) {
      setError("管理派工需开启制作要求确认，并在高级设置中显式填写调用、图片及视频额度（允许为0）");
      return;
    }
    operation.current = "create";
    setCreating(true);
    onBusyChange(true);
    setError("");
    try {
      if (!submittedPayload.current) {
        const assetIds: string[] = [];
        for (const [index, file] of files.entries()) {
          setStep(`上传原片 ${index + 1} / ${files.length}`);
          let id = uploaded.current.get(file);
          if (!id) {
            const body = new FormData();
            body.append("file", file);
            id = (await api<Asset>("/assets", { method: "POST", body })).id;
            uploaded.current.set(file, id);
          }
          assetIds.push(id);
        }
        submittedPayload.current = {
          ...fields,
          preview_id: active.preview_id,
          source_ids: selectedIds,
          asset_ids: assetIds,
          idempotency_key: idempotencyKey.current,
          confirm_sources: selectedIds.length > 0 && confirmSources,
        };
        setSubmitted(true);
      }
      setStep("创建翻译任务");
      const task = await api<Task>(managed ? "/manager/intakes" : "/fireplay/intake/create", {
        method: "POST", body: JSON.stringify({
          ...submittedPayload.current, ...(managed ? { max_kie_credits: kieLimit } : {}),
        }),
      });
      invalidatePreview();
      onCreated(task.id);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "任务创建失败");
    } finally {
      operation.current = null;
      setCreating(false);
      onBusyChange(false);
    }
  };

  const canCreate = Boolean(preview && options.title.trim() && intakeLocales(options.languages).length
    && (sourceIds.length || files.length) && (!sourceIds.length || confirmSources));

  return <div className="fireplay-intake">
    <form className="form-stack intake-preview-form" onSubmit={loadPreview} aria-busy={previewing}>
      <label>Fireplay 需求号
        <input aria-label="Fireplay 需求号" value={reference} required disabled={creating}
          placeholder="输入需求编号或需求 ID" autoComplete="off"
          onChange={(event) => { setReference(event.target.value); invalidatePreview(); }} />
      </label>
      <button type="submit" className="secondary" disabled={busy || !reference.trim()}>
        {previewing ? "正在查询需求…" : preview ? "重新查询需求" : "查询需求预览"}
      </button>
      {!preview && <ErrorBox message={error} />}
      {!preview && <button type="button" className="text-button" onClick={onClose} disabled={busy}>取消</button>}
    </form>
    {preview && <form onSubmit={submit} className="form-stack intake-create-form" aria-busy={creating}>
      <section className="intake-requirement" aria-label="需求预览">
        <span className="intake-code">{preview.requirement.code || preview.requirement.id}</span>
        <h3>{preview.requirement.title || "需求未填写标题"}</h3>
        <h4>需求要求</h4>
        <p className="intake-remote-text">{preview.requirement.description || "需求未提供说明，请补充制作要求。"}</p>
        {preview.requirement.ref_text && <details className="help-disclosure"><summary>参考内容</summary>
          <p className="intake-remote-text">{preview.requirement.ref_text}</p></details>}
        <small>需求语言：{preview.requirement.locales.join("、") || "未提供"}</small>
      </section>
      {preview.warnings.length > 0 && <div className="intake-warnings" role="status">
        <strong>请检查以下提示</strong>
        <ul>{preview.warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul>
      </div>}
      <fieldset className="intake-sources" disabled={busy || submitted}>
        <legend>待翻译原片候选</legend>
        {!preview.sources.length && <p>需求中未找到可用的视频引用，请补传本地原片。</p>}
        {preview.sources.map((source) => <label className="intake-source" key={source.id}>
          <input type="checkbox" checked={sourceIds.includes(source.id)} disabled={!source.downloadable}
            onChange={(event) => {
              setConfirmSources(false);
              setSourceIds((ids) => event.target.checked
                ? [...new Set([...ids, source.id])] : ids.filter((id) => id !== source.id));
            }} />
          <span><strong>{source.name}</strong><small>{sourceKindLabels[source.kind] ?? "其他引用"} · {source.downloadable ? "可下载" : "不可下载"}</small>
            {source.reason && <small>{source.reason}</small>}</span>
        </label>)}
      </fieldset>
      {sourceIds.length > 0 && <label className="intake-source intake-source-confirm">
        <input type="checkbox" checked={confirmSources} disabled={busy || submitted}
          onChange={(event) => setConfirmSources(event.target.checked)} />
        <span>我确认所选需求引用就是本次待翻译原片</span>
      </label>}
      {(preview.needs_source_upload || !sourceIds.length) && <p className="intake-hint" role="status">
        请补传可用的本地原片；创建前至少需要一个已选原片或补传视频。
      </p>}
      <IntakeFiles key={previewRevision} files={files} onChange={setFiles} busy={busy || submitted} supplemental />
      {!intakeLocales(options.languages).length && <p className="intake-hint">缺少目标语言，请填写后再创建。</p>}
      <TranslationIntakeFields value={options} onChange={setOptions} busy={busy || submitted}
        requireTitle lockLanguages={preview.requirement.locales.length > 0} />
      {managed && <label>KIE 累计额度上限
        <input type="number" aria-label="KIE 累计额度上限" min={0} step="0.01" required
          value={kieLimit} disabled={busy || submitted} onChange={e => setKieLimit(Number(e.target.value))} />
      </label>}
      {submitted && !creating && <p className="intake-hint">
        创建结果尚未确认。重试会复用相同提交内容，不会更换幂等标识；如需修改，请重新查询需求。
      </p>}
      <ErrorBox message={error} />
      <div className="modal-actions">
        <button type="button" className="secondary" onClick={onClose} disabled={busy}>取消</button>
        <button type="submit" className="primary" disabled={busy || !canCreate}>
          {creating ? step : managed ? "确认并委派此条需求" : "创建并开始翻译"}
        </button>
      </div>
    </form>}
  </div>;
}
