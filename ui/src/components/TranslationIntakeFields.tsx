import { UploadCloud, X } from "lucide-react";
import type { Dispatch, SetStateAction } from "react";
import type { VisualRoute } from "../lib/types";
import { bytes } from "../lib/types";
import { Select } from "./Select";

export interface IntakeOptions {
  title: string;
  languages: string;
  sourceLanguage: string;
  notes: string;
  calls: number | null;
  imageCalls: number | null;
  visualRoute: VisualRoute;
  videoCredits: number | null;
  audioStrategy: string;
  confirmBrief: boolean;
}

export function initialIntakeOptions(languages = ""): IntakeOptions {
  return {
    title: "", languages, sourceLanguage: "", notes: "", calls: null, imageCalls: null,
    visualRoute: "agent_adaptive", videoCredits: null,
    audioStrategy: "preserve_background",
    confirmBrief: true,
  };
}

export function intakeLocales(value: string): string[] {
  return [...new Set(value.split(/[,，\s]+/).filter(Boolean))];
}

export function intakePayload(options: IntakeOptions) {
  return {
    title: options.title.trim(),
    target_locales: intakeLocales(options.languages),
    notes: options.notes,
    ...(options.sourceLanguage.trim() ? { source_language: options.sourceLanguage.trim() } : {}),
    max_paid_calls: options.calls,
    max_image_calls: options.imageCalls,
    visual_route: options.visualRoute,
    max_video_credits: options.videoCredits,
    audio_strategy: options.audioStrategy,
    confirm_brief: options.confirmBrief,
  };
}

export function IntakeFiles({ files, onChange, busy, supplemental = false }: {
  files: File[];
  onChange: (files: File[]) => void;
  busy: boolean;
  supplemental?: boolean;
}) {
  return <>
    <label className="drop-zone">
      <UploadCloud size={28} />
      <strong>{supplemental ? "补传本地原片" : "选择视频文件"}</strong>
      <span>MP4、MOV、MKV、WebM · 单个上限 512 MB</span>
      <input aria-label={supplemental ? "补传本地原片" : "选择视频文件"}
        type="file" accept="video/*,.mkv" multiple disabled={busy}
        onChange={(event) => onChange(Array.from(event.target.files ?? []))} />
    </label>
    {files.map((file, index) => <div className="file-line" key={index}>
      <span>{file.name}<small>{bytes(file.size)}</small></span>
      <button type="button" disabled={busy} className="icon-button"
        aria-label={`移除 ${file.name}`} onClick={() => onChange(files.filter((_, i) => i !== index))}>
        <X size={16} />
      </button>
    </div>)}
  </>;
}

export function TranslationIntakeFields({ value, onChange, busy, requireTitle = false, lockLanguages = false }: {
  value: IntakeOptions;
  onChange: Dispatch<SetStateAction<IntakeOptions>>;
  busy: boolean;
  requireTitle?: boolean;
  lockLanguages?: boolean;
}) {
  const change = <K extends keyof IntakeOptions>(key: K, next: IntakeOptions[K]) =>
    onChange((previous) => ({ ...previous, [key]: next }));
  return <>
    <label>任务名称
      <input value={value.title} maxLength={200} required={requireTitle}
        onChange={(event) => change("title", event.target.value)}
        placeholder="例如：产品短片 · 日本市场" disabled={busy} />
    </label>
    <label>目标语言
      <input aria-label="目标语言" value={value.languages} required
        onChange={(event) => change("languages", event.target.value)}
        placeholder="例如：en-US, de-DE" disabled={busy || lockLanguages} />
    </label>
    <label>制作要求
      <textarea rows={3} value={value.notes} maxLength={8000} disabled={busy}
        onChange={(event) => change("notes", event.target.value)}
        placeholder="品牌用语、语气、音乐与画面保留要求…" />
    </label>
    <label className="source-confirm"><input type="checkbox" checked={value.confirmBrief} disabled={busy}
      onChange={event => change("confirmBrief", event.target.checked)} />理解原片后，先与我确认制作要求</label>
    <details className="intake-advanced" onInvalidCapture={(event) => { event.currentTarget.open = true; }}>
      <summary>高级设置</summary>
      <div className="intake-advanced-fields">
        <label>源语言（可选）
          <input aria-label="源语言（可选）" value={value.sourceLanguage}
            onChange={(event) => change("sourceLanguage", event.target.value)}
            placeholder="留空自动识别，例如：zh-CN" disabled={busy} />
        </label>
        <label>原片音轨处理
          <Select aria-label="原片音轨处理" value={value.audioStrategy} disabled={busy}
            onChange={(event) => change("audioStrategy", event.target.value)}>
            <option value="preserve_background">保留背景音，替换原人声</option>
            <option value="replace_all_audio">替换全部音轨（丢弃音乐和音效）</option>
          </Select>
        </label>
      </div>
    </details>
  </>;
}
