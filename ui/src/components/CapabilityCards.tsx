import { useId, useState } from "react";
import { AudioLines, Bot, ChevronDown, Clapperboard, FolderSync, Mic, MonitorPlay, Waves } from "lucide-react";
import type { Settings } from "../lib/types";
import "./CapabilityCards.css";

type Capability = Settings["capabilities"][number];
const descriptions = {
  media: { icon: MonitorPlay, text: "视频探测、画面合成与音轨对齐。" },
  agent: { icon: Bot, text: "理解原片、制定方案并判断制作结果。" },
  asr: { icon: Mic, text: "提取人声内容，供翻译与时间对齐使用。" },
  tts: { icon: AudioLines, text: "将目标语言文本合成为配音。" },
  "video-edit": { icon: Clapperboard, text: "按制作方案生成或编辑局部视频画面。" },
  separation: { icon: Waves, text: "分离人声与伴奏，辅助原音轨处理。" },
  fireplay: { icon: FolderSync, text: "读取需求与原片信息，衔接任务接单。" },
};

export function CapabilityCards({ items }: { items: Capability[] }) {
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const prefix = useId();
  return <div className="capabilities-grid">
      {items.map(item => {
        const description = descriptions[item.id as keyof typeof descriptions];
        const Icon = description?.icon ?? MonitorPlay;
        const open = Boolean(expanded[item.id]);
        const bodyId = `${prefix}-${item.id}`;
        return <article className={`capability-tile ${item.configured ? "" : "capability-tile--unconfigured"} ${open ? "is-expanded" : ""}`} key={item.id}>
          <h2 className="capability-tile-heading">
            <button className="capability-trigger" aria-label={item.label} aria-expanded={open}
              aria-controls={bodyId} onClick={() => setExpanded(current => ({ ...current, [item.id]: !current[item.id] }))}>
              <span className="capability-icon" aria-hidden="true"><Icon size={19} strokeWidth={1.7} /></span>
              <span className="capability-title">{item.label}</span>
              <span className={`capability-state ${item.configured ? "is-ready" : ""}`}>
                <i aria-hidden="true" />{item.configured ? "配置就绪" : "未配置"}
              </span>
              <ChevronDown className="capability-chevron" size={14} aria-hidden="true" />
            </button>
          </h2>
          <div id={bodyId} className="capability-body" role={open ? "region" : undefined}
            aria-label={open ? `${item.label}完整说明` : undefined} tabIndex={open ? 0 : undefined}>
            <p>{open ? item.detail : description?.text ?? item.detail}</p>
          </div>
        </article>;
      })}
  </div>;
}
