/**
 * WebSocket client for /ws?token=... with exponential reconnect (0.5 s -> 5 s).
 */
import type { VoxBridge, WsEvent } from './types';

export type WsStatus = 'connecting' | 'open' | 'closed';

export interface VoxSocketOptions {
  bridge: VoxBridge;
  onEvent: (event: WsEvent) => void;
  onStatus: (status: WsStatus) => void;
}

const MIN_DELAY_MS = 500;
const MAX_DELAY_MS = 5_000;

export class VoxSocket {
  readonly #options: VoxSocketOptions;
  #socket: WebSocket | null = null;
  #retryTimer: number | null = null;
  #attempt = 0;
  #disposed = false;

  constructor(options: VoxSocketOptions) {
    this.#options = options;
  }

  connect(): void {
    this.#disposed = false;
    this.#open();
  }

  close(): void {
    this.#disposed = true;
    this.#clearRetry();
    const socket = this.#socket;
    this.#socket = null;
    if (socket) {
      socket.onopen = null;
      socket.onmessage = null;
      socket.onerror = null;
      socket.onclose = null;
      try {
        socket.close();
      } catch {
        /* already closed */
      }
    }
    this.#options.onStatus('closed');
  }

  #open(): void {
    if (this.#disposed) return;
    this.#clearRetry();
    this.#options.onStatus('connecting');
    const { port, token } = this.#options.bridge;
    const url = 'ws://127.0.0.1:' + String(port) + '/ws?token=' + encodeURIComponent(token);
    let socket: WebSocket;
    try {
      socket = new WebSocket(url);
    } catch {
      this.#scheduleReconnect();
      return;
    }
    this.#socket = socket;
    socket.onopen = () => {
      this.#attempt = 0;
      this.#options.onStatus('open');
    };
    socket.onmessage = (message: MessageEvent<unknown>) => this.#handleMessage(message.data);
    socket.onerror = () => {
      /* onclose follows and schedules the reconnect */
    };
    socket.onclose = () => {
      if (this.#socket === socket) this.#socket = null;
      this.#options.onStatus('closed');
      this.#scheduleReconnect();
    };
  }

  #handleMessage(data: unknown): void {
    if (typeof data !== 'string') return;
    let parsed: unknown;
    try {
      parsed = JSON.parse(data);
    } catch {
      return;
    }
    if (!parsed || typeof parsed !== 'object') return;
    const event = parsed as { type?: unknown };
    if (typeof event.type !== 'string') return;
    this.#options.onEvent(parsed as WsEvent);
  }

  #scheduleReconnect(): void {
    if (this.#disposed || this.#retryTimer !== null) return;
    const base = Math.min(MAX_DELAY_MS, MIN_DELAY_MS * 2 ** this.#attempt);
    this.#attempt = Math.min(this.#attempt + 1, 8);
    const jitter = 0.8 + Math.random() * 0.4;
    const delay = Math.round(base * jitter);
    this.#retryTimer = window.setTimeout(() => {
      this.#retryTimer = null;
      this.#open();
    }, delay);
  }

  #clearRetry(): void {
    if (this.#retryTimer !== null) {
      window.clearTimeout(this.#retryTimer);
      this.#retryTimer = null;
    }
  }
}
