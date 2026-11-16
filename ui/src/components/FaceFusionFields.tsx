import type { FaceFusionProfile, FacePreview } from "../lib/types";
import { Select } from "./Select";

export const defaultFaceProfile = (): FaceFusionProfile => ({
  face_selector_mode: "reference", face_selector_order: "large-small",
  reference_frame_number: 0, reference_face_position: 0, reference_face_distance: 0.3,
  face_mask_types: ["box"], face_mask_blur: 0.3, output_video_quality: 80,
  target_preview_id: null,
});

export const faceStates: Record<string, string> = {
  configuration_required: "待选择目标脸", pending: "等待 FaceFusion 处理",
  running: "FaceFusion 正在处理", succeeded: "结果待完整观看",
  approved: "人工已验收", failed: "执行失败", unknown: "原运行结果待核对",
};
const orders = [
  ["large-small", "面积从大到小"], ["small-large", "面积从小到大"],
  ["left-right", "从左到右"], ["right-left", "从右到左"],
  ["top-bottom", "从上到下"], ["bottom-top", "从下到上"],
  ["best-worst", "置信度从高到低"], ["worst-best", "置信度从低到高"],
];

export function FaceFusionFields({ profile, onChange, busy, preview, onDetect }: {
  profile: FaceFusionProfile;
  onChange: (profile: FaceFusionProfile) => void;
  busy: boolean;
  preview: FacePreview | null;
  onDetect: () => void;
}) {
  const change = <K extends keyof FaceFusionProfile>(key: K, value: FaceFusionProfile[K]) => {
    const invalidate = ["face_selector_mode", "face_selector_order", "reference_frame_number"].includes(key);
    onChange({ ...profile, [key]: value, ...(invalidate ? { target_preview_id: null } : {}) });
  };
  const validPreview = preview && profile.target_preview_id === preview.id;
  return <>
    <div className="design-face-options">
      <label>目标选择方式<Select aria-label="目标选择方式" disabled={busy} value={profile.face_selector_mode}
        onChange={event => change("face_selector_mode", event.target.value as FaceFusionProfile["face_selector_mode"])}>
        <option value="reference">reference · 匹配指定人物</option>
        <option value="many">many · 所有检测到的人脸</option>
        <option value="one">one · 排序后第一张人脸</option>
      </Select></label>
      <label>目标脸排序<Select aria-label="目标脸排序" disabled={busy} value={profile.face_selector_order}
        onChange={event => change("face_selector_order", event.target.value as FaceFusionProfile["face_selector_order"])}>
        {orders.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
      </Select></label>
      {profile.face_selector_mode === "reference" && <>
        <label>参考帧号<input aria-label="参考帧号" type="number" min={0} step={1} required
          value={profile.reference_frame_number} disabled={busy}
          onChange={event => change("reference_frame_number", Number(event.target.value))} /></label>
        <label>匹配距离阈值<input aria-label="匹配距离阈值" type="number" min={0} max={1} step={0.05}
          required value={profile.reference_face_distance} disabled={busy}
          onChange={event => change("reference_face_distance", Number(event.target.value))} /></label>
      </>}
      <label>边界柔化<input aria-label="边界柔化" type="number" min={0} max={1} step={0.05}
        required value={profile.face_mask_blur} disabled={busy}
        onChange={event => change("face_mask_blur", Number(event.target.value))} /></label>
      <label>输出视频质量<input aria-label="输出视频质量" type="number" min={0} max={100} step={1}
        required value={profile.output_video_quality} disabled={busy}
        onChange={event => change("output_video_quality", Number(event.target.value))} /></label>
    </div>
    <fieldset disabled={busy} className="face-mask-options"><legend>融合遮罩</legend>
      {([["box", "人脸框"], ["occlusion", "遮挡保护"], ["region", "面部区域"]] as const).map(([value, label]) =>
        <label key={value}><input type="checkbox" checked={profile.face_mask_types.includes(value)}
          disabled={busy || profile.face_mask_types.length === 1 && profile.face_mask_types.includes(value)}
          onChange={event => change("face_mask_types", event.target.checked
            ? [...profile.face_mask_types, value] : profile.face_mask_types.filter(item => item !== value))} />
          {label}</label>)}
    </fieldset>
    <p className="muted">{profile.face_selector_mode === "reference"
      ? "在参考帧中选择要替换的人物，FaceFusion 在视频中匹配这张脸。位置从 0 开始。"
      : profile.face_selector_mode === "one"
        ? "每帧仅处理排序后的第一张脸；人物交叉或大小变化时，目标可能变化。"
        : "使用来源脸替换视频中所有检测到的人脸。"}</p>
    {profile.face_selector_mode === "reference" && <section className="face-target-preview" aria-label="选择目标人物">
      <button type="button" className="secondary" disabled={busy} onClick={onDetect}>
        {busy ? "正在检测…" : "检测参考帧中的目标脸"}
      </button>
      {validPreview ? <>
        <img className="face-reference-frame" src={preview.frame_url} alt={`原视频第 ${preview.frame_number} 帧`} />
        {!preview.faces.length && <p role="status">此帧未检测到人脸，请选择另一参考帧。</p>}
        <fieldset disabled={busy} className="face-target-choices"><legend>需要替换哪张脸</legend>
          {preview.faces.map(face => <label key={face.position}>
            <img src={face.content_url} alt={`目标脸 ${face.position}`} />
            <span><input type="radio" name={`target-${preview.id}`}
              checked={profile.reference_face_position === face.position}
              onChange={() => change("reference_face_position", face.position)} />目标脸 {face.position}</span>
          </label>)}
        </fieldset>
      </> : <p className="muted">检测后会显示真实参考帧和目标脸；修改视频、参考帧或排序后需要重新检测。</p>}
    </section>}
  </>;
}
