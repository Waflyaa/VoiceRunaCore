/**
 * Shared types for the VoiceRunaCore window UI.
 * Mirrors docs/CONTRACTS.md ("HTTP/WS surface", "Events") and TZ v3.1 §10.1/§11/§12.
 */

export type VoxState =
  | 'idle'
  | 'listening'
  | 'thinking'
  | 'speaking'
  | 'paused'
  | 'downloading'
  | 'error';

export type ConnectionStatus = 'idle' | 'connecting' | 'online' | 'offline';

export type SettingsTab = 'models' | 'voice' | 'audio' | 'apps' | 'other';

/** Port + token injected by Tauri as window.__VOX__ (or read from the dev URL). */
export interface VoxBridge {
  port: number;
  token: string;
}

export interface HealthInfo {
  status: string;
  version: string;
  state: string;
}

export interface StateInfo {
  state: VoxState;
  paused: boolean;
}

/** One LLM provider entry (TZ §7.2). Never contains the key itself. */
export interface Provider {
  id: string;
  label: string;
  enabled: boolean;
  base_url: string;
  api_key_env: string;
  model: string;
  supports_tools: boolean;
  priority: number;
}

export interface VoiceInfo {
  engine: string;
  name: string;
  label: string;
}

/** One apps.json entry (TZ §9). */
export interface AppEntry {
  display_name: string;
  path?: string;
  uri?: string;
  aliases?: string[];
}

export type AppsMap = Record<string, AppEntry>;

export interface LlmConfig {
  providers: Provider[];
  timeout_sec: number;
  max_messages: number;
  reset_timeout_sec: number;
  system_prompt_extra: string;
}

export interface SttConfig {
  model: string;
  device: string;
  compute_type: string;
  unload_after_idle_sec: number;
  language: string;
}

export interface VadConfig {
  silence_ms: number;
  max_utterance_sec: number;
  pre_buffer_ms: number;
}

export interface WakewordConfig {
  engine: string;
  phrase: string;
  threshold: number;
  listen_timeout_sec: number;
  bargein_bump: number;
}

export interface TtsConfig {
  engine: string;
  voice: string;
  rate: string;
  max_chars: number;
  voices: VoiceInfo[];
  silero_speaker?: string;
}

export interface RouterConfig {
  fuzzy_threshold: number;
}

export interface WeatherConfig {
  default_city: string;
  cache_ttl_sec: number;
  language: string;
}

export interface ErrorPhrases {
  stt_failed_phrase: string;
  not_understood_phrase: string;
  tool_refused_phrase: string;
  no_connection_phrase: string;
  mic_phrase: string;
}

export interface AudioConfig {
  input_device: string;
  output_device: string;
}

/** One device from GET /audio/devices. */
export interface AudioDevice {
  index: number;
  name: string;
  channels: number;
  hostapi: string;
  default: boolean;
}

export interface AudioPlaybackInfo {
  ok: boolean;
  backend: string;
  message: string;
}

/** GET /audio/devices — what the machine can record from and play to. */
export interface AudioDevicesResponse {
  input: AudioDevice[];
  output: AudioDevice[];
  current: {
    input: string;
    output: string;
  };
  playback: AudioPlaybackInfo;
}

export type AudioTestKind = 'input' | 'output' | 'stt';

/** POST /audio/test — level is 0…1 for kind=input, null for output; text for kind=stt. */
export interface AudioTestResponse {
  ok: boolean;
  message: string;
  level: number | null;
  text?: string | null;
}

/** GET /diagnostics — read-only health snapshot for the settings window. */
export interface DiagnosticsProvider {
  id: string;
  enabled: boolean;
  model: string;
  api_key_env: string;
  key_present: boolean;
  supports_tools: boolean;
}

export interface DiagnosticsPaths {
  data_dir?: string;
  config?: string;
  env?: string;
  apps?: string;
  models?: string;
  logs?: string;
}

export interface DiagnosticsVoice {
  state?: string;
  mic?: {
    ok?: boolean;
    device?: string;
    error?: string | null;
    samplerate?: number;
    native_samplerate?: number;
  };
  vad?: { type?: string; ready?: boolean; error?: string | null };
  wake?: { engine?: string; ready?: boolean; error?: string | null; last_heard?: string };
  stt?: {
    ready?: boolean;
    model?: string;
    loaded?: boolean;
    error?: string | null;
    model_path?: string;
    model_size_mb?: number;
    last_confidence?: number | null;
    last_processing_s?: number | null;
  };
  tts?: { engine?: string; backend?: string; last?: Record<string, unknown> };
  debug?: { save_audio?: boolean; last_wav?: string; raw_wav?: string };
  voices?: { count?: number };
  models_dir?: string;
  error?: string;
}

/** POST /stt/record_test — record from the mic and run it through the real STT. */
export interface SttRecordTestResponse {
  ok: boolean;
  text: string;
  confidence: number | null;
  processing_s: number;
  wav_path: string;
  message: string;
  vad?: { start_s?: number; end_s?: number; speech_ms?: number };
  mic?: { device?: string; samplerate?: number };
}

export interface Diagnostics {
  version?: string;
  state?: string;
  paused?: boolean;
  paths?: DiagnosticsPaths;
  logging?: { enabled?: boolean };
  providers?: DiagnosticsProvider[];
  providers_disabled?: string[];
  voice?: DiagnosticsVoice;
}

/** Persistent banner reason from a WS error event. */
export interface ApiKeyIssue {
  kind: 'no_api_key' | 'auth';
  message: string;
}

export interface LoggingConfig {
  enabled: boolean;
  level: string;
  rotate_mb: number;
}

/** Full config.json shape (TZ §12). */
export interface Config {
  llm: LlmConfig;
  stt: SttConfig;
  vad: VadConfig;
  wakeword: WakewordConfig;
  hotkey: { activate: string };
  tts: TtsConfig;
  router: RouterConfig;
  apps: { allowed_uri_schemes: string[] };
  folders: { allowed: string[] };
  files: { allowed_extensions: string[]; lnk_allowed_folders: string[] };
  weather: WeatherConfig;
  errors: ErrorPhrases;
  logging: LoggingConfig;
  audio: AudioConfig;
}

/** PUT /config accepts a partial deep merge: arrays are replaced whole. */
export type DeepPartial<T> = {
  [K in keyof T]?: T[K] extends readonly unknown[]
    ? T[K]
    : T[K] extends object
      ? DeepPartial<T[K]>
      : T[K];
};

export type ConfigPatch = DeepPartial<Config>;

export interface ToolCallInfo {
  name: string;
  args?: Record<string, unknown>;
  ok: boolean;
}

export interface ChatMessage {
  id: number;
  role: 'user' | 'assistant' | 'tool' | 'error';
  text: string;
  at: number;
  source?: 'voice' | 'window';
  toolOk?: boolean;
  tool?: ToolCallInfo;
}

export type ToastKind = 'info' | 'success' | 'error';

export interface Toast {
  id: number;
  kind: ToastKind;
  text: string;
}

export interface DownloadState {
  stage: string;
  percent: number;
  message: string;
}

/** WS events from docs/CONTRACTS.md ("Events"). */
export type WsEvent =
  | { type: 'state'; state: VoxState }
  | { type: 'transcript'; text: string; source: 'voice' | 'window' }
  | { type: 'answer'; text: string; replaced_transcript?: string | null }
  | { type: 'tool_call'; name: string; args?: Record<string, unknown>; ok: boolean }
  | { type: 'error'; message: string; kind?: string }
  | { type: 'download_progress'; stage?: string; percent?: number; message?: string };

export interface OkResponse {
  ok: boolean;
  [key: string]: unknown;
}

export interface ModelsResponse {
  models: string[];
}

export interface VoicesResponse {
  voices: VoiceInfo[];
}

export interface ProviderTestResponse {
  ok: boolean;
  message: string;
}

/** POST /tts/preview — ok=false when playback failed (no output device, edge-tts error). */
export interface PreviewResponse {
  ok: boolean;
  message: string;
}
