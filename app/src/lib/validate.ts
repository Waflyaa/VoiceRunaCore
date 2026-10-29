/**
 * Client-side validation for the settings editor.
 * It blocks saving obviously broken configs; the sidecar still validates for real.
 */
import type { AppEntry, AppsMap, Config, SettingsTab } from './types';

const PROVIDER_ID = /^[A-Za-z0-9][A-Za-z0-9._-]*$/;
const ENV_NAME = /^[A-Za-z_][A-Za-z0-9_]*$/;
const RATE = /^[+-]\d{1,3}%$/;
const URI = /^[A-Za-z][A-Za-z0-9+.-]*:/;
const APP_ID = /^[A-Za-z0-9][A-Za-z0-9_-]*$/;

export interface ValidationIssue {
  tab: SettingsTab;
  message: string;
}

export function validateAppEntry(id: string, entry: AppEntry): string[] {
  const issues: string[] = [];
  const trimmedId = id.trim();
  const name = entry.display_name?.trim() ? entry.display_name.trim() : trimmedId || 'без названия';
  if (!trimmedId) issues.push('У программы не задан id.');
  else if (!APP_ID.test(trimmedId)) {
    issues.push('id «' + trimmedId + '»: только латиница, цифры, «_» и «-».');
  }
  if (!entry.display_name?.trim()) issues.push('Программа «' + name + '»: не задано display_name.');
  const hasPath = Boolean(entry.path?.trim());
  const hasUri = Boolean(entry.uri?.trim());
  if (hasPath && hasUri) issues.push('Программа «' + name + '»: укажите только path или только uri.');
  if (!hasPath && !hasUri) issues.push('Программа «' + name + '»: нужен path или uri.');
  if (hasUri && entry.uri && !URI.test(entry.uri.trim())) {
    issues.push('Программа «' + name + '»: uri должен начинаться со схемы, например steam://…');
  }
  return issues;
}

function checkInt(
  issues: ValidationIssue[],
  label: string,
  value: number,
  min: number,
  max: number,
): void {
  if (!Number.isFinite(value) || !Number.isInteger(value) || value < min || value > max) {
    issues.push({
      tab: 'other',
      message: label + ': целое число от ' + String(min) + ' до ' + String(max) + '.',
    });
  }
}

export function validateSettings(config: Config, apps: AppsMap): ValidationIssue[] {
  const issues: ValidationIssue[] = [];
  const seenIds = new Set<string>();

  config.llm.providers.forEach((provider, index) => {
    const id = (provider.id ?? '').trim();
    const name = (provider.label ?? '').trim() || id || 'провайдер №' + String(index + 1);
    if (!id) issues.push({ tab: 'models', message: name + ': не задан id.' });
    else if (!PROVIDER_ID.test(id)) {
      issues.push({
        tab: 'models',
        message: name + ': id — латиница, цифры, точка, «_», «-» (например, openrouter).',
      });
    } else if (seenIds.has(id)) {
      issues.push({ tab: 'models', message: 'id «' + id + '» повторяется.' });
    } else {
      seenIds.add(id);
    }
    if (!/^https?:\/\//i.test((provider.base_url ?? '').trim())) {
      issues.push({ tab: 'models', message: name + ': base_url должен начинаться с http:// или https://.' });
    }
    if (!(provider.model ?? '').trim()) {
      issues.push({ tab: 'models', message: name + ': не выбрана модель.' });
    }
    if (!Number.isInteger(provider.priority) || provider.priority < 0) {
      issues.push({ tab: 'models', message: name + ': приоритет — целое число 0 и больше.' });
    }
    const envName = (provider.api_key_env ?? '').trim();
    if (envName && !ENV_NAME.test(envName)) {
      issues.push({
        tab: 'models',
        message: name + ': api_key_env — имя переменной .env, например OPENROUTER_API_KEY.',
      });
    }
  });

  if (!(config.tts.voice ?? '').trim()) {
    issues.push({ tab: 'voice', message: 'Не выбран голос синтеза.' });
  }
  if (!RATE.test((config.tts.rate ?? '').trim())) {
    issues.push({ tab: 'voice', message: 'Скорость должна быть в формате +10% или -5%.' });
  }

  for (const [id, entry] of Object.entries(apps)) {
    for (const message of validateAppEntry(id, entry)) issues.push({ tab: 'apps', message });
  }

  checkInt(issues, 'Тишина VAD (мс)', config.vad.silence_ms, 100, 5000);
  checkInt(issues, 'Максимум фразы (с)', config.vad.max_utterance_sec, 1, 60);
  checkInt(issues, 'Ожидание команды (с)', config.wakeword.listen_timeout_sec, 1, 60);
  checkInt(issues, 'Таймаут LLM (с)', config.llm.timeout_sec, 5, 180);
  if (!Number.isFinite(config.wakeword.threshold) || config.wakeword.threshold < 0 || config.wakeword.threshold > 1) {
    issues.push({ tab: 'other', message: 'Порог wake — число от 0 до 1 (например, 0.5).' });
  }
  const assistantName = (config.wakeword.phrase ?? '').trim();
  if (!assistantName) {
    issues.push({ tab: 'other', message: 'Имя ассистента: укажите слово, по которому он просыпается.' });
  } else if (/s/.test(assistantName) || assistantName.length < 2 || assistantName.length > 24) {
    issues.push({
      tab: 'other',
      message: 'Имя ассистента: одно слово из 2–24 символов, без пробелов.',
    });
  }
  if (!(config.weather.default_city ?? '').trim()) {
    issues.push({ tab: 'other', message: 'Не указан город по умолчанию.' });
  }

  // Audio devices are stored as "default" or "index:<n>" (backend contract).
  const audioInput = (config.audio?.input_device ?? '').trim();
  const audioOutput = (config.audio?.output_device ?? '').trim();
  if (!audioInput) {
    issues.push({ tab: 'audio', message: 'Микрофон: выберите устройство или «По умолчанию (система)».' });
  }
  if (!audioOutput) {
    issues.push({ tab: 'audio', message: 'Динамики: выберите устройство или «По умолчанию (система)».' });
  }
  if (audioInput.startsWith('index:') && !/^index:\d+$/.test(audioInput)) {
    issues.push({ tab: 'audio', message: 'Микрофон: формат устройства — «default» или «index:<номер>».' });
  }
  if (audioOutput.startsWith('index:') && !/^index:\d+$/.test(audioOutput)) {
    issues.push({ tab: 'audio', message: 'Динамики: формат устройства — «default» или «index:<номер>».' });
  }

  return issues;
}
