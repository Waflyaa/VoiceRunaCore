/**
 * Human-facing formatting helpers: state labels, clock, tool-call lines.
 */
import type { AppsMap, VoxState } from './types';

export interface StateMeta {
  label: string;
  color: string;
  hint: string;
}

/** TZ §3.2 state names shown as text + colour. */
export const STATE_META: Record<VoxState, StateMeta> = {
  idle: { label: 'Ожидание', color: 'var(--state-idle)', hint: 'Скажите имя ассистента или введите команду' },
  listening: { label: 'Слушаю', color: 'var(--state-listening)', hint: 'Говорите — записываю' },
  thinking: { label: 'Думаю', color: 'var(--state-thinking)', hint: 'Разбираю команду' },
  speaking: { label: 'Говорю', color: 'var(--state-speaking)', hint: 'Озвучиваю ответ' },
  paused: { label: 'Пауза', color: 'var(--state-paused)', hint: 'Микрофон выключен' },
  error: { label: 'Ошибка', color: 'var(--state-error)', hint: 'Что-то пошло не так' },
  downloading: { label: 'Скачиваю модель', color: 'var(--state-downloading)', hint: 'Нужна сеть — первый запуск' },
};

export function stateMeta(state: VoxState, paused: boolean): StateMeta {
  if (paused && state !== 'downloading' && state !== 'error') return STATE_META.paused;
  return STATE_META[state] ?? STATE_META.idle;
}

function pad(value: number): string {
  return value < 10 ? '0' + value : String(value);
}

export function formatTime(at: number): string {
  const date = new Date(at);
  if (Number.isNaN(date.getTime())) return '—';
  return pad(date.getHours()) + ':' + pad(date.getMinutes());
}

export function formatPercent(percent: number): string {
  if (!Number.isFinite(percent)) return '0%';
  const clamped = Math.max(0, Math.min(100, Math.round(percent)));
  return String(clamped) + '%';
}

export function clampNumber(value: number, min: number, max: number): number {
  if (!Number.isFinite(value)) return min;
  return Math.max(min, Math.min(max, value));
}

export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return '0 Б';
  const units = ['Б', 'КБ', 'МБ', 'ГБ'];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  const digits = unit === 0 ? 0 : value >= 10 ? 0 : 1;
  return value.toFixed(digits) + ' ' + units[unit];
}

const FOLDER_LABELS: Record<string, string> = {
  desktop: 'Рабочий стол',
  downloads: 'Загрузки',
  documents: 'Документы',
};

function str(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

/** Short Russian title for a tool call shown in the history. */
export function toolTitle(name: string, args: Record<string, unknown> | undefined, apps: AppsMap): string {
  const values = args ?? {};
  switch (name) {
    case 'launch_application': {
      const id = str(values.application_id);
      const entry = apps[id];
      return (entry && entry.display_name) || id || 'программу';
    }
    case 'open_folder': {
      const folder = str(values.folder_id);
      return FOLDER_LABELS[folder] || folder || 'папку';
    }
    case 'open_file': {
      const filename = str(values.filename);
      return filename || 'файл';
    }
    case 'get_weather': {
      const city = str(values.city);
      return city ? 'погода · ' + city : 'погода';
    }
    case 'get_time':
      return 'время';
    default:
      return name || 'инструмент';
  }
}

/** Compact one-liner: "▶ Dota 2" or "✕ Dota 2 — отказ". */
export function toolLine(
  name: string,
  args: Record<string, unknown> | undefined,
  ok: boolean,
  apps: AppsMap,
): string {
  const title = toolTitle(name, args, apps);
  return ok ? '▶ ' + title : '✕ ' + title + ' — отказ';
}

/** Trim + collapse whitespace, used for aliases entered as "a, b, c". */
export function normalizeAliases(raw: string): string[] {
  return raw
    .split(',')
    .map((item) => item.trim().toLowerCase())
    .filter((item, index, list) => item.length > 0 && list.indexOf(item) === index);
}

export function formatAliases(aliases: string[] | undefined): string {
  return (aliases ?? []).join(', ');
}
