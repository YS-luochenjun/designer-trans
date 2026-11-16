export interface Asset {
  id: string;
  name: string;
  content_url: string;
  size_bytes: number;
  kind?: "video" | "image" | "face_source";
  image?: { display_size: number[]; stored_size: number[]; orientation: number };
}
export interface Artifact extends Omit<Asset, "kind"> {
  created_at?: string;
  kind: string;
  locale: string | null;
  node_id: string;
  sha256: string;
}
export interface RouteReport {
  owner: "route";
  source: "route_state_projection" | "route_plan";
  phase: string;
  summary: string;
  next_step: string;
  needs_user_decision: boolean;
  node_id: string | null;
}
export interface TaskNode {
  route_report?: RouteReport;
  route_state?: string;
  id: string;
  definition_id: string;
  label: string;
  stage: string;
  locale: string | null;
  asset_id: string;
  status: string;
  depends_on: string[];
  attempt: number;
  started_at: string | null;
  finished_at: string | null;
  error: string | null;
  output: Record<string, unknown>;
  artifact_ids: string[];
  skip_reason: string | null;
}
export interface Question {
  id: string;
  node_id: string;
  prompt: string;
  kind: string;
  options: string[];
  status: string;
  answer: string | null;
  context: { artifact_id?: string; sha256?: string; inventory?: Record<string, unknown>; notes?: string; media_kind?: string };
}
export interface Delivery {
  id: string;
  task_id: string;
  title: string;
  locale: string;
  asset_id: string;
  artifact_id: string;
  quality_status: string;
  status: string;
  created_at: string;
  content_url: string;
  notes: string;
  members?: { asset_id: string; artifact_id: string; sha256: string; archive_name: string }[];
}
export interface FaceFusionProfile {
  face_selector_mode: "reference" | "many" | "one";
  face_selector_order: "left-right" | "right-left" | "top-bottom" | "bottom-top"
    | "small-large" | "large-small" | "best-worst" | "worst-best";
  reference_frame_number: number;
  reference_face_position: number;
  reference_face_distance: number;
  face_mask_types: ("box" | "occlusion" | "region")[];
  face_mask_blur: number;
  output_video_quality: number;
  target_preview_id?: string | null;
}
export interface FacePreview {
  id: string;
  asset_id: string;
  source_sha256: string;
  frame_number: number;
  order: string;
  frame_url: string;
  faces: { position: number; bounds: number[]; content_url: string }[];
}
export interface ExternalExecution {
  id?: string;
  executor: string;
  state: string;
  artifact_id?: string | null;
  sha256?: string | null;
  quality_passed: boolean;
  error?: string | null;
}
export interface ProductionDecision {
  id: string;
  node_id: string;
  stage: string;
  status: string;
  created_at?: string;
  key?: string;
  result?: Record<string, unknown>;
  media?: unknown;
}
export interface ProductionSession {
  routing_stop?: { reason?: string };
  text_overrides?: Record<string, unknown>;
  id: string;
  node_id: string;
  turn: number;
  finished: boolean;
  accepted: Record<string, unknown>[];
  candidate?: Record<string, unknown> | null;
  evidence?: string[];
  history?: { turn: number; decision: Record<string, unknown>; tool_error?: string }[];
}
export interface AgentTrace {
  id: string;
  decision_id?: string;
  node_id: string;
  attempt: number;
  stage: string;
  status: string;
  created_at: string;
  finished_at?: string;
  error?: string | null;
  frame_times_ms: number[];
  requested_resources: string[];
  resources: { id: string; name: string; kind: string; version: string; sha256: string }[];
  result?: Record<string, unknown> | null;
}
export interface ImagegenRequest {
  id: string;
  node_id: string;
  source_id: string;
  status: string;
  lifecycle: string;
  created_at: string;
  time_ms: number;
  frame_index: number;
  prompt: string;
  execution_owner?: string;
  execution_state?: string;
  dispatch_blocked_reason?: string;
  error?: string;
  execution_error?: string;
  execution_error_detail?: string;
  tool_warning?: string;
  generated_output?: {
    original_path: string;
    task_path: string;
    dimensions: number[];
    original_frame_dimensions: number[];
    sha256: string;
  };
  original_frame_path: string;
  generated_image_path?: string;
  claim_receipt_path?: string;
  result_receipt_path?: string;
}
export interface TaskRouteEvent {
  id: string;
  node_id: string;
  trigger: string;
  status: string;
  reason?: string;
  plan?: { action: string; reason: string; instruction?: string };
  created_at: string;
  reconciliation?: {
    owner: string;
    active_count: number;
    retained_unknown_count: number;
    active: { id: string; kind: string; status: string; classification: string }[];
    retained_unknown: { id: string; kind: string; status: string; classification: string }[];
    basis_sha256: string;
  };
}
export interface Task {
  task_kind?: "video_translation" | "image_translation" | "face_swap";
  execution_owner?: "facefusion" | "route_worker";
  external_execution?: ExternalExecution;
  facefusion?: FaceFusionProfile;
  design_profile?: {
    sample_duration_seconds?: 3 | 5;
    face_mask?: "standard" | "tight";
    preserve_audio?: boolean;
    review_policy?: string;
  };
  execution_readiness?: "ready" | "draft_only" | "external_tool";
  design_workspace?: {
    task_kind: string;
    execution: string;
    source_count?: number;
    locale_count?: number;
    member_count?: number;
    members?: { id: string; source_id: string; locale: string; status: string; artifact_id: string | null }[];
    bundles?: { locale: string; expected_source_ids: string[]; node_id: string }[];
    next_step?: string;
    target_preview?: FacePreview | null;
    display_steps?: string[];
  };
  route_report?: RouteReport;
  task_route_events?: TaskRouteEvent[];
  output_goal?: "complete_candidate" | "quality_approved";
  imagegen_requests?: ImagegenRequest[];
  production_policy?: { mode: string; quality_passed: boolean };
  model_snapshot?: Record<string, unknown>;
  image_snapshot?: Record<string, unknown>;
  id: string;
  title: string;
  status: string;
  source: string;
  created_at: string;
  updated_at: string;
  target_locales: string[];
  asset_ids: string[];
  progress: { done: number; total: number };
  current_nodes: string[];
  delivery_count: number;
  error?: string;
  notes?: string;
  nodes?: TaskNode[];
  assets?: Asset[];
  artifacts?: Artifact[];
  questions?: Question[];
  deliveries?: Delivery[];
  operations?: Record<string, unknown>[];
  production_decisions?: ProductionDecision[];
  production_sessions?: ProductionSession[];
  agent_traces?: AgentTrace[];
  conversations?: NodeMessage[];
  control_paused?: boolean;
  execution_snapshot?: { budget_mode?: "bounded" | "testing_unlimited" };
  brief_revision?: number;
  visual_route?: VisualRoute;
  max_image_calls?: number | null;
  max_visual_steps?: number | null;
  max_video_credits?: number | null;
  max_kie_credits?: number | null;
  confirm_brief?: boolean;
  audio_strategy?: "preserve_background" | "replace_all_audio";
}
export interface NodeMessage {
  interaction?: "proposal" | "route";
  direction?: { status: string; revision: number };
  direction_error?: string;
  id: string;
  node_id: string;
  text: string;
  scope: "node" | "downstream";
  status: string;
  created_at: string;
  error?: string | null;
  reply?: { reply: string; question: string | null; proposed_instruction: string | null;
    action?: "inform" | "direct" | "clarify"; options?: string[] } | null;
  applied_revision?: number | null;
  continuation?: { status: "queued"; node_id: string; revision: number; at: string } | null;
}
export interface Page<T> {
  items: T[];
  total: number;
}
export interface Overview {
  total: number;
  running: number;
  waiting: number;
  completed: number;
  partial: number;
  failed: number;
  deliveries: number;
  fireplay: {
    configured: boolean;
    last_synced_at: string | null;
    complete: boolean;
    total: number;
  };
}
export interface Schedule {
  id: string;
  name: string;
  kind: "project_patrol" | "requirements_check" | "agent_message";
  trigger: "once" | "interval";
  enabled: boolean;
  prompt?: string;
  refresh_requirements?: boolean;
  last_reply?: string;
  last_error?: string;
  interval_seconds: number | null;
  run_at: string | null;
  timezone: string;
  next_run_at: string | null;
  last_result: string | null;
}
export type AgentEngine = "codex" | "harness" | "custom";
export type ModelRole = "transcription" | "tts" | "video";
export type VisualRoute = "agent_adaptive" | "solid_overlay" | "wan_video_edit";
export interface ModelChoice {
  version: string;
  transcription: string;
  tts: string;
  video: string;
  options: Partial<Record<ModelRole, Record<string, unknown>>>;
}
export interface ModelCatalogEntry {
  role: ModelRole;
  label: string;
  model: string;
  protocol: string;
  defaults: Record<string, unknown>;
  limitations: unknown;
  source: unknown;
  estimated_credits_per_second?: number | Record<string, number>;
  pricing_summary?: string;
  pricing_source?: string;
  pricing_verified_on?: string;
}
export interface ModelSettings {
  agent_engine: AgentEngine;
  selection: ModelChoice;
  catalog: { version: string; models: Record<string, ModelCatalogEntry> };
  resolved: Record<string, unknown>;
}
export interface Settings {
  capabilities: {
    id: string;
    label: string;
    configured: boolean;
    detail: string;
  }[];
  runtime: { max_concurrent_tasks: number; agent_mode: string };
  resources: Record<string, number>;
}
export interface Resource {
  id: string;
  version: string;
  name: string;
  kind: string;
  status: string;
  description: string;
  content: string;
}
export const labels: Record<string, string> = {
  draft: "任务草稿",
  workflow_step: "步骤",
  pending: "尚未开始",
  queued: "排队中",
  running: "执行中",
  waiting_external: "等待外部结果",
  waiting_user: "等待确认",
  succeeded: "已完成",
  failed: "失败",
  skipped: "已跳过",
  cancelled: "已取消",
  interrupted: "已中断",
  unknown: "结果待核对",
  blocked: "需要处理",
  partial_completed: "部分完成",
  completed: "全部完成",
  accepted: "已交付",
  open: "待处理",
  answered: "已回答",
  rejected: "已拒绝",
  resolved: "已解决",
  superseded: "已失效",
  human_approved: "人工已验收",
  awaiting_human: "等待视听验收",
};
export const time = (value?: string | null) =>
  value
    ? new Date(value).toLocaleString("zh-CN", {
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
      })
    : "—";
export const bytes = (value: number) =>
  value > 1048576
    ? `${(value / 1048576).toFixed(1)} MB`
    : `${Math.ceil(value / 1024)} KB`;
