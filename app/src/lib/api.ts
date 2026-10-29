/**
 * Typed REST client for the Python sidecar.
 *
 * Contract: docs/CONTRACTS.md "HTTP/WS surface". Every call carries X-Vox-Token,
 * binds to 127.0.0.1 and fails with a Russian message ready for a toast.
 */
import type {
  AppsMap,
  AudioDevicesResponse,
  AudioTestKind,
  AudioTestResponse,
  Config,
  ConfigPatch,
  Diagnostics,
  HealthInfo,
  ModelsResponse,
  OkResponse,
  PreviewResponse,
  ProviderTestResponse,
  StateInfo,
  SttRecordTestResponse,
  VoxBridge,
  VoicesResponse,
} from './types';

const DEFAULT_TIMEOUT_MS = 20_000;
// Long questions spend a while inside the LLM; a 20 s cap aborted healthy
// /command requests with "сайдкар не ответил" (owner report 2026-10-28).
const LONG_TIMEOUT_MS = 120_000;
/** LLM/provider calls can legitimately take a while. */
const SLOW_TIMEOUT_MS = 45_000;

export class ApiError extends Error {
  readonly status: number | null;
  readonly path: string;

  constructor(message: string, status: number | null, path: string) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.path = path;
  }
}

function extractDetail(body: string): string {
  if (!body) return '';
  try {
    const parsed = JSON.parse(body) as unknown;
    if (typeof parsed === 'string') return parsed.trim();
    if (parsed && typeof parsed === 'object') {
      const record = parsed as Record<string, unknown>;
      for (const key of ['detail', 'message', 'error']) {
        const value = record[key];
        if (typeof value === 'string' && value.trim()) return value.trim();
        if (Array.isArray(value)) {
          const parts = value
            .map((item) => {
              if (typeof item === 'string') return item;
              if (item && typeof item === 'object') {
                const message = (item as Record<string, unknown>).msg;
                if (typeof message === 'string') return message;
              }
              return '';
            })
            .filter((item) => item.length > 0);
          if (parts.length > 0) return parts.join('; ');
        }
      }
    }
  } catch {
    /* body is not JSON: fall through to plain text */
  }
  return body.slice(0, 300);
}

function httpMessage(status: number, path: string, detail: string): string {
  const suffix = detail ? ': ' + detail : '';
  if (status === 401 || status === 403) {
    return 'Сайдкар отклонил токен доступа. Перезапустите приложение.';
  }
  if (status === 404) {
    if (path === '/env') return 'Сайдкар не поддерживает сохранение ключа (PUT /env).';
    return 'Сайдкар не знает запрос ' + path + '. Версии окна и сайдкара не совпадают.';
  }
  if (status === 405) return 'Сайдкар не разрешает этот метод для ' + path + '.';
  if (status === 422) return 'Сайдкар отклонил данные' + suffix;
  if (status === 400) return 'Сайдкар отклонил запрос' + suffix;
  if (status === 429) return 'Слишком много запросов — попробуйте позже.';
  if (status >= 500) return 'Сайдкар вернул ошибку ' + status + suffix;
  return 'Запрос ' + path + ' не выполнен (' + status + ')' + suffix;
}

async function readBody(response: Response): Promise<string> {
  try {
    return await response.text();
  } catch {
    return '';
  }
}

export class VoxApi {
  readonly bridge: VoxBridge;

  constructor(bridge: VoxBridge) {
    this.bridge = bridge;
  }

  get baseUrl(): string {
    return 'http://127.0.0.1:' + String(this.bridge.port);
  }

  private async request<T>(
    method: string,
    path: string,
    body?: unknown,
    timeoutMs: number = DEFAULT_TIMEOUT_MS,
  ): Promise<T> {
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), timeoutMs);
    let response: Response;
    try {
      response = await fetch(this.baseUrl + path, {
        method,
        headers: {
          'X-Vox-Token': this.bridge.token,
          ...(body === undefined ? {} : { 'Content-Type': 'application/json' }),
        },
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: controller.signal,
      });
    } catch {
      if (controller.signal.aborted) {
        throw new ApiError(
          'Сайдкар не ответил за ' + String(Math.round(timeoutMs / 1000)) + ' с (' + path + ').',
          null,
          path,
        );
      }
      throw new ApiError(
        'Нет связи с сайдкаром на 127.0.0.1:' + String(this.bridge.port) + '. Приложение запущено?',
        null,
        path,
      );
    } finally {
      window.clearTimeout(timer);
    }

    if (!response.ok) {
      const detail = extractDetail(await readBody(response));
      throw new ApiError(httpMessage(response.status, path, detail), response.status, path);
    }
    if (response.status === 204) return undefined as T;
    const text = await response.text();
    if (!text) return {} as T;
    try {
      return JSON.parse(text) as T;
    } catch {
      throw new ApiError('Сайдкар вернул некорректный JSON для ' + path + '.', response.status, path);
    }
  }

  health(): Promise<HealthInfo> {
    return this.request<HealthInfo>('GET', '/health');
  }

  state(): Promise<StateInfo> {
    return this.request<StateInfo>('GET', '/state');
  }

  command(text: string): Promise<{ answer?: string }> {
    return this.request<{ answer?: string }>('POST', '/command', { text }, LONG_TIMEOUT_MS);
  }

  listen(): Promise<OkResponse> {
    return this.request<OkResponse>('POST', '/listen');
  }

  repeat(): Promise<{ answer?: string }> {
    return this.request<{ answer?: string }>('POST', '/repeat');
  }

  stopSpeaking(): Promise<OkResponse> {
    return this.request<OkResponse>('POST', '/stop_speaking');
  }

  pause(): Promise<OkResponse> {
    return this.request<OkResponse>('POST', '/pause');
  }

  resume(): Promise<OkResponse> {
    return this.request<OkResponse>('POST', '/resume');
  }

  getConfig(): Promise<Config> {
    return this.request<Config>('GET', '/config');
  }

  putConfig(patch: ConfigPatch): Promise<Config> {
    return this.request<Config>('PUT', '/config', patch);
  }

  providerModels(id: string): Promise<ModelsResponse> {
    return this.request<ModelsResponse>(
      'GET',
      '/providers/' + encodeURIComponent(id) + '/models',
      undefined,
      SLOW_TIMEOUT_MS,
    );
  }

  providerTest(id: string): Promise<ProviderTestResponse> {
    return this.request<ProviderTestResponse>(
      'POST',
      '/providers/' + encodeURIComponent(id) + '/test',
      {},
      SLOW_TIMEOUT_MS,
    );
  }

  voices(): Promise<VoicesResponse> {
    return this.request<VoicesResponse>('GET', '/voices');
  }

  /** Open %APPDATA%\VoiceRunaCore\voices so a .onnx can be dropped in. */
  async openVoicesFolder(): Promise<{ ok: boolean; path: string; message: string }> {
    return this.request<{ ok: boolean; path: string; message: string }>(
      'POST',
      '/voices/open_folder',
      {},
      SLOW_TIMEOUT_MS,
    );
  }

  /** ok=false carries the reason: no output device, edge-tts error, ... */
  ttsPreview(voice: string, text: string): Promise<PreviewResponse> {
    return this.request<PreviewResponse>('POST', '/tts/preview', { voice, text }, SLOW_TIMEOUT_MS);
  }

  /** Measure the room noise and store speech thresholds (POST /audio/calibrate). */
  calibrateMic(seconds = 3): Promise<{ ok: boolean; message?: string; noise_floor_dbfs?: number; threshold_dbfs?: number }> {
    return this.request('POST', '/audio/calibrate', { seconds }, SLOW_TIMEOUT_MS);
  }

  /** Record N seconds from the mic and recognise them (POST /stt/record_test). */
  sttRecordTest(seconds = 3): Promise<SttRecordTestResponse> {
    return this.request<SttRecordTestResponse>(
      'POST',
      '/stt/record_test',
      { seconds },
      SLOW_TIMEOUT_MS,
    );
  }

  /** Read-only health snapshot: paths, providers, voice chain (GET /diagnostics). */
  getDiagnostics(): Promise<Diagnostics> {
    return this.request<Diagnostics>('GET', '/diagnostics', undefined, SLOW_TIMEOUT_MS);
  }

  /** Available microphones and outputs plus the current playback backend. */
  audioDevices(): Promise<AudioDevicesResponse> {
    return this.request<AudioDevicesResponse>('GET', '/audio/devices', undefined, SLOW_TIMEOUT_MS);
  }

  /**
   * Play a test sound (kind=output) or measure the microphone (kind=input).
   * device is "index:<n>" for a concrete device, null for the system default.
   */
  audioTest(kind: AudioTestKind, device: string | null): Promise<AudioTestResponse> {
    return this.request<AudioTestResponse>('POST', '/audio/test', { kind, device }, SLOW_TIMEOUT_MS);
  }

  getApps(): Promise<AppsMap> {
    return this.request<AppsMap>('GET', '/apps');
  }

  putApps(apps: AppsMap): Promise<AppsMap> {
    return this.request<AppsMap>('PUT', '/apps', apps);
  }

  /** Write one API key into %APPDATA%\VoiceRunaCore\.env (never config.json). */
  setEnv(name: string, value: string): Promise<OkResponse> {
    return this.request<OkResponse>('PUT', '/env', { name, value });
  }

  shutdown(): Promise<OkResponse> {
    return this.request<OkResponse>('POST', '/shutdown');
  }
}

let current: VoxApi | null = null;

export function configureApi(bridge: VoxBridge): VoxApi {
  current = new VoxApi(bridge);
  return current;
}

export function requireApi(): VoxApi {
  if (!current) throw new ApiError('Нет подключения к сайдкару.', null, '/');
  return current;
}

export function currentApi(): VoxApi | null {
  return current;
}

/** Turn any thrown value into a short Russian sentence for a toast. */
export function describeError(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof DOMException && error.name === 'AbortError') {
    return 'Запрос прерван по таймауту.';
  }
  if (error instanceof TypeError) {
    return 'Нет связи с сайдкаром. Проверьте, что приложение запущено.';
  }
  if (error instanceof Error) {
    return error.message || 'Неизвестная ошибка';
  }
  const text = String(error ?? '').trim();
  return text || 'Неизвестная ошибка';
}
