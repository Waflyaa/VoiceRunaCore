/**
 * Bridge to the Tauri 2 shell.
 *
 * Rust injects window.__VOX__ = {port, token} before the page loads.
 * Outside Tauri (plain Vite in a browser) the port/token can be supplied with
 * ?port=&token= or persist in localStorage from a previous session.
 */
import type { VoxBridge } from './types';

const PORT_KEY = 'vox.port';
const TOKEN_KEY = 'vox.token';

interface TauriInternals {
  invoke?: (cmd: string, args?: Record<string, unknown>) => Promise<unknown>;
}

interface TauriWindow extends Window {
  __VOX__?: { port?: unknown; token?: unknown };
  __TAURI_INTERNALS__?: TauriInternals;
}

function asPort(value: unknown): number | null {
  const parsed = typeof value === 'number' ? value : Number.parseInt(String(value ?? ''), 10);
  if (!Number.isInteger(parsed) || parsed <= 0 || parsed > 65535) return null;
  return parsed;
}

function asToken(value: unknown): string | null {
  const token = typeof value === 'string' ? value.trim() : '';
  return token ? token : null;
}

function readInjected(): VoxBridge | null {
  const injected = (window as TauriWindow).__VOX__;
  if (!injected || typeof injected !== 'object') return null;
  const port = asPort(injected.port);
  const token = asToken(injected.token);
  if (port === null || token === null) return null;
  return { port, token };
}

function readQuery(): { port: number | null; token: string | null } {
  try {
    const params = new URLSearchParams(window.location.search);
    return { port: asPort(params.get('port')), token: asToken(params.get('token')) };
  } catch {
    return { port: null, token: null };
  }
}

function readStorage(): { port: number | null; token: string | null } {
  try {
    return {
      port: asPort(window.localStorage.getItem(PORT_KEY)),
      token: asToken(window.localStorage.getItem(TOKEN_KEY)),
    };
  } catch {
    return { port: null, token: null };
  }
}

/** Resolve connection details or null when the window has none yet. */
export function resolveBridge(): VoxBridge | null {
  const injected = readInjected();
  if (injected) {
    rememberBridge(injected);
    return injected;
  }
  const query = readQuery();
  const stored = readStorage();
  const port = query.port ?? stored.port;
  const token = query.token ?? stored.token;
  if (port === null || token === null) return null;
  const bridge = { port, token };
  rememberBridge(bridge);
  return bridge;
}

export function rememberBridge(bridge: VoxBridge): void {
  try {
    window.localStorage.setItem(PORT_KEY, String(bridge.port));
    window.localStorage.setItem(TOKEN_KEY, bridge.token);
  } catch {
    /* private mode / storage disabled: dev fallback only, ignore */
  }
}

export function forgetBridge(): void {
  try {
    window.localStorage.removeItem(PORT_KEY);
    window.localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* ignore */
  }
}

export function isTauriRuntime(): boolean {
  return typeof window !== 'undefined' && Boolean((window as TauriWindow).__TAURI_INTERNALS__);
}

/** Snapshot of the sidecar from Rust (commands.rs / sidecar.rs). */
export interface SidecarSnapshot {
  running?: boolean;
  healthy?: boolean;
  port?: number;
  restarts?: number;
  paused?: boolean;
  error?: string | null;
}

/** Call a Rust command; throws a Russian message when running outside Tauri. */
export async function invokeTauri<T>(command: string, args: Record<string, unknown> = {}): Promise<T> {
  const internals = (window as TauriWindow).__TAURI_INTERNALS__;
  if (internals && typeof internals.invoke === 'function') {
    return (await internals.invoke(command, args)) as T;
  }
  if (!isTauriRuntime()) {
    throw new Error('Действие доступно только в приложении VoiceRunaCore');
  }
  try {
    const core = await import('@tauri-apps/api/core');
    return await core.invoke<T>(command, args);
  } catch {
    throw new Error('Не удалось вызвать команду приложения: ' + command);
  }
}

/** Hide the window to tray (TZ §10.1). */
export async function hideWindow(): Promise<void> {
  await invokeTauri('hide_window');
}

/** Open %APPDATA%\VoiceRunaCore in Explorer (TZ §10.1). */
export async function openConfigFolder(): Promise<void> {
  await invokeTauri('open_config_folder');
}

/** Safety net when window.__VOX__ is stale (e.g. the sidecar restarted on another port). */
export async function getConnection(): Promise<VoxBridge | null> {
  if (!isTauriRuntime()) return null;
  try {
    const info = await invokeTauri<{ port?: unknown; token?: unknown }>('get_connection');
    // Rust reports port === 0 when the sidecar did not start: the UI then
    // shows the "сайдкар не отвечает" banner with invoke('restart_sidecar').
    const port = asPort(info?.port);
    const token = asToken(info?.token);
    if (port === null || token === null) return null;
    return { port, token };
  } catch {
    return null;
  }
}

/** Restart the sidecar from the error screen (TZ §3). Rejects with a Russian string. */
export async function restartSidecar(): Promise<void> {
  if (!isTauriRuntime()) {
    throw new Error('Перезапуск сайдкара доступен только в приложении VoiceRunaCore');
  }
  await invokeTauri('restart_sidecar');
}

export async function fetchSidecarStatus(): Promise<SidecarSnapshot | null> {
  if (!isTauriRuntime()) return null;
  try {
    return await invokeTauri<SidecarSnapshot>('sidecar_status');
  } catch {
    return null;
  }
}

/**
 * Subscribe to a Rust event ("vox-connection", "vox-paused", ...).
 * Returns an unsubscribe function; outside Tauri it is a no-op.
 */
export async function onTauriEvent<T>(
  name: string,
  handler: (payload: T) => void,
): Promise<() => void> {
  if (!isTauriRuntime()) return () => undefined;
  try {
    const events = await import('@tauri-apps/api/event');
    return await events.listen<T>(name, (event) => handler(event.payload));
  } catch {
    return () => undefined;
  }
}

/**
 * Resolve the sidecar bridge: window.__VOX__ -> query/localStorage -> get_connection.
 * Polls briefly because a host may inject the global a tick after the page starts.
 */
export async function resolveBridgeAsync(): Promise<VoxBridge | null> {
  const immediate = resolveBridge();
  if (immediate) return immediate;
  for (let attempt = 0; attempt < 6; attempt += 1) {
    await new Promise((resolve) => window.setTimeout(resolve, 250));
    const bridge = resolveBridge();
    if (bridge) return bridge;
  }
  const connection = await getConnection();
  if (connection) rememberBridge(connection);
  return connection;
}
