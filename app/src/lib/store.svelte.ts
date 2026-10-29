/**
 * Single source of truth for the window UI (Svelte 5 runes).
 *
 * Wires the REST client and the /ws stream together: events own the history,
 * REST calls are used for window actions (TZ §10.1) and settings (§11).
 */
import { configureApi, currentApi, describeError, requireApi } from './api';
import { toolLine } from './format';
import {
  fetchSidecarStatus,
  getConnection,
  hideWindow,
  isTauriRuntime,
  onTauriEvent,
  openConfigFolder,
  rememberBridge,
  resolveBridgeAsync,
  restartSidecar as invokeRestartSidecar,
} from './tauri';
import type { SidecarSnapshot } from './tauri';
import { VoxSocket, type WsStatus } from './ws';
import type {
  ApiKeyIssue,
  AppsMap,
  ChatMessage,
  ConnectionStatus,
  DownloadState,
  SettingsTab,
  Toast,
  ToastKind,
  VoxBridge,
  VoxState,
  WsEvent,
} from './types';

const MAX_MESSAGES = 400;
const HEALTH_POLL_MS = 5_000;

const KNOWN_STATES: VoxState[] = [
  'idle',
  'listening',
  'thinking',
  'speaking',
  'paused',
  'downloading',
  'error',
];

export const ui = $state({
  bridge: null as VoxBridge | null,
  /** Overall reachability of the sidecar (REST or WS). */
  connection: 'idle' as ConnectionStatus,
  wsConnected: false,
  state: 'idle' as VoxState,
  paused: false,
  download: null as DownloadState | null,
  messages: [] as ChatMessage[],
  toasts: [] as Toast[],
  busy: false,
  input: '',
  focusSeq: 0,
  apps: {} as AppsMap,
  settingsOpen: false,
  settingsTab: 'models' as SettingsTab,
  /** Stays on screen until a successful answer or a saved key (TZ §15). */
  apiKeyIssue: null as ApiKeyIssue | null,
  sidecar: null as SidecarSnapshot | null,
  restarting: false,
});

let nextId = 1;
let socket: VoxSocket | null = null;
let healthTimer: number | null = null;
let bootstrapped = false;
let pendingEcho: { text: string; at: number } | null = null;
let tauriSubscribed = false;
const unlisteners: Array<() => void> = [];

// ----------------------------------------------------------------- history

function addMessage(message: Omit<ChatMessage, 'id' | 'at'> & { at?: number }): ChatMessage {
  const { at, ...rest } = message;
  const item: ChatMessage = { id: nextId++, at: at ?? Date.now(), ...rest };
  ui.messages.push(item);
  if (ui.messages.length > MAX_MESSAGES) {
    ui.messages.splice(0, ui.messages.length - MAX_MESSAGES);
  }
  return item;
}

function markEcho(text: string): void {
  pendingEcho = { text, at: Date.now() };
}

function consumeEcho(text: string): boolean {
  if (!pendingEcho) return false;
  if (pendingEcho.text !== text) return false;
  if (Date.now() - pendingEcho.at > 10_000) return false;
  pendingEcho = null;
  return true;
}

/**
 * runtime.py emits answer(phrase) right after error(no_api_key/auth), so the
 * "answer" that repeats the banner phrase is not a successful answer.
 */
function isIssueEcho(text: string): boolean {
  const issue = ui.apiKeyIssue;
  if (!issue) return false;
  const answer = (text ?? '').trim();
  if (!answer) return false;
  const message = issue.message.trim();
  return answer === message || message.startsWith(answer);
}

// ------------------------------------------------------------------ state

function applyState(state: VoxState): void {
  if (!KNOWN_STATES.includes(state)) return;
  ui.state = state;
  if (state === 'downloading') {
    if (!ui.download) ui.download = { stage: '', percent: 0, message: '' };
    return;
  }
  ui.download = null;
  ui.paused = state === 'paused';
}

export function handleEvent(event: WsEvent): void {
  switch (event.type) {
    case 'state':
      applyState(event.state);
      return;
    case 'transcript': {
      const text = (event.text ?? '').trim();
      if (!text) return;
      if (consumeEcho(text)) return;
      addMessage({ role: 'user', text, source: event.source === 'window' ? 'window' : 'voice' });
      return;
    }
    case 'answer': {
      const text = (event.text ?? '').trim();
      if (!text) return;
      // A real answer proves the provider/key works again: drop the banner,
      // unless it is just the error phrase echoed back as the answer.
      if (!isIssueEcho(text)) ui.apiKeyIssue = null;
      addMessage({ role: 'assistant', text });
      return;
    }
    case 'tool_call':
      addMessage({
        role: 'tool',
        text: toolLine(event.name, event.args, event.ok, ui.apps),
        toolOk: event.ok,
        tool: { name: event.name, args: event.args, ok: event.ok },
      });
      return;
    case 'error': {
      const text = (event.message ?? '').trim() || 'Ошибка';
      addMessage({ role: 'error', text });
      // Missing/rejected key is not a 3-second toast: keep a banner with a way out.
      if (event.kind === 'no_api_key' || event.kind === 'auth') {
        ui.apiKeyIssue = { kind: event.kind, message: text };
      }
      return;
    }
    case 'download_progress': {
      const percent = typeof event.percent === 'number' && Number.isFinite(event.percent)
        ? event.percent
        : 0;
      ui.download = {
        stage: event.stage ?? '',
        percent,
        message: event.message ?? '',
      };
      ui.state = 'downloading';
      ui.paused = false;
      return;
    }
    default:
      return;
  }
}

// ------------------------------------------------------------ connection

async function pingHealth(): Promise<boolean> {
  if (!ui.bridge || !currentApi()) return false;
  try {
    await requireApi().health();
    if (!ui.wsConnected) ui.connection = 'online';
    return true;
  } catch {
    if (!ui.wsConnected) ui.connection = 'offline';
    return false;
  }
}

function startHealthPolling(): void {
  if (healthTimer !== null) return;
  healthTimer = window.setInterval(() => {
    void (async () => {
      const healthy = await pingHealth();
      if (healthy && !ui.wsConnected) await refreshState();
    })();
  }, HEALTH_POLL_MS);
}

function stopHealthPolling(): void {
  if (healthTimer === null) return;
  window.clearInterval(healthTimer);
  healthTimer = null;
}

function handleSocketStatus(status: WsStatus): void {
  if (status === 'open') {
    ui.wsConnected = true;
    ui.connection = 'online';
    stopHealthPolling();
    void refreshState();
    return;
  }
  if (status === 'connecting') {
    ui.wsConnected = false;
    if (ui.connection === 'idle') ui.connection = 'connecting';
    return;
  }
  ui.wsConnected = false;
  startHealthPolling();
  void pingHealth();
}

function connectSocket(): void {
  if (!ui.bridge) return;
  socket?.close();
  socket = new VoxSocket({
    bridge: ui.bridge,
    onEvent: handleEvent,
    onStatus: handleSocketStatus,
  });
  socket.connect();
}

/** Apply a (new) port/token, e.g. after the sidecar restarted on another port. */
function applyBridge(next: VoxBridge | null | undefined): boolean {
  if (!next) return false;
  const port = Number(next.port);
  const token = typeof next.token === 'string' ? next.token.trim() : '';
  if (!Number.isInteger(port) || port <= 0 || port > 65535 || !token) return false;
  const bridge: VoxBridge = { port, token };
  if (ui.bridge && ui.bridge.port === bridge.port && ui.bridge.token === bridge.token) {
    return false;
  }
  ui.bridge = bridge;
  rememberBridge(bridge);
  configureApi(bridge);
  ui.connection = 'connecting';
  connectSocket();
  return true;
}

function setPaused(paused: boolean): void {
  ui.paused = paused;
  if (paused) {
    ui.state = 'paused';
    return;
  }
  if (ui.state === 'paused') ui.state = 'idle';
}

export async function refreshSidecarStatus(): Promise<void> {
  if (!isTauriRuntime()) return;
  const status = await fetchSidecarStatus();
  if (!status) return;
  ui.sidecar = status;
  if (typeof status.paused === 'boolean') setPaused(status.paused);
}

async function addListener<T>(name: string, handler: (payload: T) => void): Promise<void> {
  const off = await onTauriEvent<T>(name, handler);
  unlisteners.push(off);
}

/** Rust -> UI events (packaging/README.md §5.3). No-op outside Tauri. */
async function subscribeTauriEvents(): Promise<void> {
  if (tauriSubscribed || !isTauriRuntime()) return;
  tauriSubscribed = true;

  await addListener<VoxBridge>('vox-connection', (payload) => {
    if (applyBridge(payload)) {
      void pingHealth();
      void refreshState();
      void refreshApps();
    }
  });

  await addListener<SidecarSnapshot>('vox-sidecar', (payload) => {
    ui.sidecar = payload;
    if (typeof payload.paused === 'boolean') setPaused(payload.paused);
  });

  await addListener<{ message?: string }>('vox-error', (payload) => {
    if (payload?.message) pushToast(payload.message, 'error');
  });

  await addListener<{ paused?: boolean }>('vox-paused', (payload) => {
    if (typeof payload?.paused === 'boolean') setPaused(payload.paused);
  });

  await addListener<null>('open-settings', () => openSettings());

  await addListener<{ message?: string }>('vox-hotkey-error', (payload) => {
    if (payload?.message) pushToast(payload.message, 'error');
  });

  void refreshSidecarStatus();
}

export async function bootstrap(): Promise<void> {
  if (bootstrapped) return;
  bootstrapped = true;

  // Subscribe first: Rust may report a sidecar start/restart right after launch.
  void subscribeTauriEvents();

  const bridge = await resolveBridgeAsync();
  if (!bridge) {
    ui.connection = 'offline';
    void subscribeTauriEvents(); // retry: internals may have appeared late
    void refreshSidecarStatus();
    return;
  }
  applyBridge(bridge);
  void subscribeTauriEvents(); // retry: internals may have appeared late
  const healthy = await pingHealth();
  if (healthy) await refreshState();
  await refreshApps();
  void refreshSidecarStatus();
}

/** Error screen action: "перезапустить сайдкар" (TZ §3). */
export async function restartSidecar(): Promise<void> {
  if (ui.restarting) return;
  if (!isTauriRuntime()) {
    pushToast('Перезапуск сайдкара доступен только в приложении VoiceRunaCore', 'info');
    return;
  }
  ui.restarting = true;
  try {
    await invokeRestartSidecar();
    pushToast('Сайдкар перезапущен', 'success');
    const connection = await getConnection();
    if (applyBridge(connection)) {
      void pingHealth();
      void refreshState();
      void refreshApps();
    }
    await refreshSidecarStatus();
  } catch (error) {
    pushToast('Не удалось перезапустить сайдкар: ' + describeError(error), 'error');
    await refreshSidecarStatus();
  } finally {
    ui.restarting = false;
  }
}

export async function refreshState(): Promise<void> {
  if (!ui.bridge || !currentApi()) return;
  try {
    const info = await requireApi().state();
    const paused = Boolean(info.paused) || info.state === 'paused';
    applyState(paused ? 'paused' : info.state);
    ui.paused = paused;
  } catch {
    /* the connection indicator is the source of truth for outages */
  }
}

export async function refreshApps(): Promise<void> {
  if (!ui.bridge || !currentApi()) return;
  try {
    ui.apps = await requireApi().getApps();
  } catch {
    /* tool lines fall back to raw ids */
  }
}

// -------------------------------------------------------------- actions

export async function sendText(raw: string): Promise<void> {
  const text = raw.trim();
  if (!text || ui.busy) return;
  if (!ui.bridge || !currentApi()) {
    pushToast('Нет подключения к сайдкару', 'error');
    return;
  }
  addMessage({ role: 'user', text, source: 'window' });
  markEcho(text);
  ui.input = '';
  ui.busy = true;
  try {
    const result = await requireApi().command(text);
    const answer = typeof result.answer === 'string' ? result.answer.trim() : '';
    if (answer && !ui.wsConnected) addMessage({ role: 'assistant', text: answer });
    if (answer && !isIssueEcho(answer)) ui.apiKeyIssue = null;
  } catch (error) {
    const message = describeError(error);
    addMessage({ role: 'error', text: message });
    pushToast(message, 'error');
  } finally {
    ui.busy = false;
  }
}

export async function listenNow(): Promise<void> {
  if (ui.busy) return;
  if (!ui.bridge || !currentApi()) {
    pushToast('Нет подключения к сайдкару', 'error');
    return;
  }
  ui.busy = true;
  try {
    await requireApi().listen();
  } catch (error) {
    const message = describeError(error);
    pushToast(message, 'error');
  } finally {
    ui.busy = false;
  }
}

export async function repeatLast(): Promise<void> {
  if (ui.busy) return;
  if (!ui.bridge || !currentApi()) {
    pushToast('Нет подключения к сайдкару', 'error');
    return;
  }
  ui.busy = true;
  try {
    const result = await requireApi().repeat();
    const answer = typeof result.answer === 'string' ? result.answer.trim() : '';
    if (answer && !ui.wsConnected) addMessage({ role: 'assistant', text: answer });
    if (answer && !isIssueEcho(answer)) ui.apiKeyIssue = null;
  } catch (error) {
    const message = describeError(error);
    pushToast(message, 'error');
  } finally {
    ui.busy = false;
  }
}

export async function stopSpeaking(): Promise<void> {
  if (!ui.bridge || !currentApi()) return;
  try {
    await requireApi().stopSpeaking();
  } catch (error) {
    pushToast(describeError(error), 'error');
  }
}

/** TZ §2.4: put the last recognised phrase back into the input for repair. */
export function editMessage(text: string): void {
  ui.input = text;
  ui.focusSeq += 1;
}

export function openSettings(tab: SettingsTab = 'models'): void {
  ui.settingsTab = tab;
  ui.settingsOpen = true;
}

/** Called after PUT /env succeeded: the banner is no longer relevant. */
export function clearApiKeyIssue(): void {
  ui.apiKeyIssue = null;
}

export function closeSettings(): void {
  ui.settingsOpen = false;
}

export async function hideToTray(): Promise<void> {
  if (!isTauriRuntime()) {
    pushToast('Скрытие окна доступно только в приложении VoiceRunaCore', 'info');
    return;
  }
  try {
    await hideWindow();
  } catch (error) {
    pushToast(describeError(error), 'error');
  }
}

export async function openConfig(): Promise<void> {
  if (!isTauriRuntime()) {
    pushToast('Папку конфига можно открыть только в приложении VoiceRunaCore', 'info');
    return;
  }
  try {
    await openConfigFolder();
  } catch (error) {
    pushToast(describeError(error), 'error');
  }
}

// --------------------------------------------------------------- toasts

export function pushToast(text: string, kind: ToastKind = 'info', ttlMs = 4500): void {
  const item: Toast = { id: nextId++, kind, text };
  ui.toasts.push(item);
  if (ui.toasts.length > 4) ui.toasts.splice(0, ui.toasts.length - 4);
  const ttl = kind === 'error' ? Math.max(ttlMs, 7000) : ttlMs;
  window.setTimeout(() => dismissToast(item.id), ttl);
}

export function dismissToast(id: number): void {
  const index = ui.toasts.findIndex((toast) => toast.id === id);
  if (index >= 0) ui.toasts.splice(index, 1);
}
