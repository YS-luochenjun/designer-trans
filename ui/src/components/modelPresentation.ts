import type { ModelCatalogEntry } from "../lib/types";

export const modelRoles = [
  { id: "transcription", label: "音频转录" },
  { id: "tts", label: "语音合成（TTS）" },
  { id: "video", label: "视频编辑" },
] as const;

export function modelName(label: string) {
  return label.split(/[·（(]/)[0]
    .replace(/\s+(音频理解转写|参考视频局部补底|原视频编辑).*$/, "").trim();
}

export function modelPricing(model: ModelCatalogEntry) {
  if (model.pricing_summary) return model.pricing_summary;
  const rates = model.estimated_credits_per_second;
  if (typeof rates === "number") return `参考单价：${rates} credits / 秒`;
  if (rates && Object.keys(rates).length) {
    return `参考单价：${Object.entries(rates).map(([resolution, rate]) =>
      `${resolution} 每秒 ${rate} credits`).join("；")}`;
  }
  return "未记录可核对的单价，以供应商实际扣费为准。";
}
