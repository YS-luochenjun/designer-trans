import type { Task } from "./types";

export type TaskKind = NonNullable<Task["task_kind"]>;
export const taskKinds: { id: TaskKind; label: string }[] = [
  { id: "video_translation", label: "视频翻译" },
  { id: "image_translation", label: "图片翻译" },
  { id: "face_swap", label: "视频换脸" },
];
export const kindLabel = (task: Task) =>
  taskKinds.find(kind => kind.id === (task.task_kind ?? "video_translation"))?.label ?? "视频翻译";
