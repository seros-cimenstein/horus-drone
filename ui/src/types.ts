export interface Pose {
  north_m: number;
  east_m: number;
  up_m: number;
  yaw_deg?: number;
}

export interface DroneState {
  state?: string;
  operator_lock?: boolean;
  vehicle?: {
    pose?: Pose;
    altitude_m?: number;
    ground_range_m?: number;
    speed_m_s?: number;
    mode?: string;
    battery_pct?: number;
    link_ok?: boolean;
    pilot_override?: boolean;
    observation_age_s?: number;
    proximity?: Record<string, number>;
    proximity_sectors?: number[];
  };
  perception?: {
    camera?: string;
    last_frame_age_s?: number | null;
    reason?: string | null;
    vocabulary?: string[];
  };
  gimbal?: {
    pitch_deg?: number;
    yaw_deg?: number;
  };
}

export interface Candidate {
  id: number;
  label: string;
  confidence?: number;
  distance_m?: number;
  bearing_deg?: number;
}

export interface CameraFrame {
  image_base64: string;
  mime_type: string;
  frame_id?: number;
  candidates?: Candidate[];
  timestamp?: string;
}

export interface SceneInfo {
  name: string;
  title: string;
  subtitle: string;
  indoor: boolean;
  prompts: string[];
}

export interface AssetInfo {
  available: boolean;
  optional?: boolean;
  path: string | null;
  source: string;
  detail: string;
}

export interface AIStatus {
  configured: boolean;
  provider: string;
  effective_provider: string;
  model: string;
  detail: string;
}

export interface LinkShaperState {
  delay_ms: number;
  jitter_ms: number;
  link_cut: boolean;
}

export interface StudioStatus {
  lifecycle: "idle" | "bootstrapping" | "starting" | "running" | "stopping" | "error";
  mode: string;
  scene: string | null;
  target_label: string;
  run_id: string | null;
  mcp_url: string | null;
  spectator_url: string | null;
  error: string | null;
  rtf: number | null;
  assets: Record<string, AssetInfo>;
  assets_ready: boolean;
  ai_status: AIStatus;
  link_shaper: LinkShaperState;
  scenes: SceneInfo[];
}

export interface StudioSettingsData {
  ai_provider: string;
  model: string;
  base_url: string;
  reasoning_effort: string;
  claude_command: string;
  claude_effort: string;
  omp_command: string;
  omp_model: string;
  omp_thinking: string;
  default_scene: string;
  prefer_3d_view: boolean;
  theme: "dark" | "light";
  flight_speed_m_s: number;
  link_loss_rth_delay_s: number;
  default_gimbal_pitch_deg: number;
  camera_max_fps: number;
  open_vocab_hz: number;
  grounder_url: string;
  recent_hardware: Array<Record<string, unknown>>;
}

export interface StudioSettingsView {
  settings: StudioSettingsData;
  secrets_configured: Record<string, boolean>;
  secrets_hints: Record<string, string | null>;
  ai_status: AIStatus;
}

export interface CapabilityCheck {
  name: string;
  ok: boolean;
  optional?: boolean;
  detail: string;
  fix_hint: string;
}

export interface CapabilityItem {
  id: string;
  title: string;
  tier?: "core" | "optional";
  ready: boolean;
  summary: string;
  checks: CapabilityCheck[];
}

export interface McpClientConfig {
  id: string;
  name: string;
  description: string;
  snippet: string;
  cli: string;
}

export interface McpConfigsResponse {
  mcp_url: string;
  live_session: boolean;
  clients: McpClientConfig[];
}

export interface GrounderStatus {
  running: boolean;
  url: string;
  pid: number | null;
  backend?: string | null;
}

export interface PhoneStatus {
  local_url: string;
  lan_url?: string | null;
  lan_ip?: string | null;
  tls_enabled?: boolean;
  tailnet_url: string | null;
  phone_token_active: boolean;
  tailscale_installed: boolean;
  tailscale_dns: string | null;
  tailscale_certs: boolean;
  serve_active: boolean;
  output?: string;
}

export interface PreflightItem {
  id: string;
  label: string;
  status: "pass" | "warn" | "fail";
  detail: string;
}

export interface SerialPortInfo {
  device: string;
  description: string;
  hwid: string;
}

export interface RunLogEntry {
  name: string;
  bytes: number;
  modified_utc: string;
}

export interface TimelineEvent {
  id: number;
  kind: string;
  time?: string;
  source?: string;
  text?: string;
  tool?: string;
  args?: Record<string, unknown>;
  summary?: string;
  result?: unknown;
  error?: string;
  action?: string;
  status?: string;
  view_id?: string;
  bytes?: number;
  [key: string]: unknown;
}

export interface MapIndex {
  version: string;
  provider: string;
  attribution: string;
  levels: Array<{ level: number; size_m: number }>;
  bounds?: [number, number, number, number];
  tiles: Record<string, Array<[number, number, number]>>;
  bytes?: Record<string, number>;
}

export interface MapTileData {
  version: string;
  level: number;
  x: number;
  y: number;
  ways?: Array<{ id: string; kind: string; name?: string; points: Array<[number, number]> }>;
  walkable?: Array<{ id: string; kind: string; name?: string; outline: Array<[number, number]> }>;
  obstacles?: Array<{ id: string; kind: string; name?: string; outline: Array<[number, number]> }>;
  areas?: Array<{ id: string; kind: string; name?: string; outline: Array<[number, number]> }>;
  places?: Array<{ id: string; kind: string; name: string; north_m: number; east_m: number }>;
}

export interface LiveMap {
  pose?: Pose;
  heading_deg?: number;
  marks?: Array<{ name: string; north_m: number; east_m: number; note?: string }>;
  candidates?: Array<{ id: number; label: string; north_m: number; east_m: number }>;
}

export type MapSelection =
  | { kind: "point"; label: string; point: [number, number] }
  | { kind: "area"; label: string; points: Array<[number, number]> };
