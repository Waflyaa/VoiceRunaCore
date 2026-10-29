<script lang="ts">
  import { onMount } from 'svelte';
  import { ApiError, describeError, requireApi } from '../../lib/api';
  import { pushToast } from '../../lib/store.svelte';
  import type {
    AudioConfig,
    AudioDevice,
    AudioDevicesResponse,
    AudioTestResponse,
    Diagnostics,
    SttRecordTestResponse,
  } from '../../lib/types';
  import Icon from '../Icon.svelte';

  interface Props {
    audio: AudioConfig;
  }

  let { audio }: Props = $props();

  let devices = $state<AudioDevicesResponse | null>(null);
  let loading = $state(true);
  let loadError = $state<string | null>(null);
  let refreshError = $state<string | null>(null);
  let refreshing = $state(false);
  let testingInput = $state(false);
  let testingOutput = $state(false);
  let testingStt = $state(false);
  let inputResult = $state<AudioTestResponse | null>(null);
  let outputResult = $state<AudioTestResponse | null>(null);
  let sttResult = $state<AudioTestResponse | null>(null);
  let sttPhase = $state<'idle' | 'recording' | 'processing'>('idle');
  let sttRecord = $state<SttRecordTestResponse | null>(null);
  let sttRecordError = $state<string | null>(null);
  let sttUnavailable = $state(false);
  let sttTimer: number | null = null;
  let showDiagnostics = $state(false);
  let diagnostics = $state<Diagnostics | null>(null);
  let diagnosticsLoading = $state(false);
  let diagnosticsError = $state<string | null>(null);

  const inputDevices = $derived.by(() => (devices && Array.isArray(devices.input) ? devices.input : []));
  const outputDevices = $derived.by(() => (devices && Array.isArray(devices.output) ? devices.output : []));
  const playbackMessage = $derived((devices?.playback?.message ?? '').trim());
  const playbackBad = $derived(Boolean(devices && devices.playback && devices.playback.ok === false));

  onMount(() => {
    void load(true);
  });

  async function load(initial: boolean): Promise<void> {
    if (initial) loading = true;
    else refreshing = true;
    loadError = null;
    refreshError = null;
    if (initial) {
      inputResult = null;
      outputResult = null;
    }
    try {
      const response = await requireApi().audioDevices();
      devices = {
        input: Array.isArray(response?.input) ? response.input : [],
        output: Array.isArray(response?.output) ? response.output : [],
        current: {
          input: typeof response?.current?.input === 'string' ? response.current.input : '',
          output: typeof response?.current?.output === 'string' ? response.current.output : '',
        },
        playback: {
          ok: response?.playback?.ok !== false,
          backend: typeof response?.playback?.backend === 'string' ? response.playback.backend : '',
          message: typeof response?.playback?.message === 'string' ? response.playback.message : '',
        },
      };
      inputResult = null;
      outputResult = null;
    } catch (error) {
      const message = describeError(error);
      // A failed refresh must not wipe an already rendered device list.
      if (devices) {
        refreshError = message;
        pushToast(message, 'error');
      } else {
        loadError = message;
      }
    } finally {
      loading = false;
      refreshing = false;
    }
  }

  function deviceValue(device: AudioDevice): string {
    return 'index:' + String(device.index);
  }

  function deviceLabel(device: AudioDevice): string {
    return device.name + (device.default ? ' · по умолчанию' : '');
  }

  function hasValue(list: AudioDevice[], value: string): boolean {
    const trimmed = (value ?? '').trim();
    return trimmed === 'default' || list.some((device) => deviceValue(device) === trimmed);
  }

  function isMissing(list: AudioDevice[], value: string): boolean {
    const trimmed = (value ?? '').trim();
    if (!trimmed || trimmed === 'default') return false;
    return !list.some((device) => deviceValue(device) === trimmed);
  }

  function deviceDetails(device: AudioDevice): string {
    const parts: string[] = [];
    if (device.hostapi) parts.push(device.hostapi);
    if (Number.isFinite(device.channels)) parts.push(String(device.channels) + ' кан.');
    if (device.default) parts.push('по умолчанию в системе');
    return parts.join(' · ');
  }

  /** GET /audio/devices returns config values ("default"/"index:N") in current.*. */
  function resolveCurrentDevice(list: AudioDevice[], currentValue: string): AudioDevice | null {
    const value = (currentValue ?? '').trim();
    if (!value || value === 'default') return list.find((item) => item.default) ?? null;
    if (value.startsWith('index:')) {
      const numeric = Number.parseInt(value.slice(6), 10);
      return Number.isFinite(numeric) ? list.find((item) => item.index === numeric) ?? null : null;
    }
    return list.find((item) => item.name === value) ?? null;
  }

  function hintFor(list: AudioDevice[], value: string, currentValue: string): string {
    const trimmed = (value ?? '').trim();
    if (!trimmed || trimmed === 'default') {
      const current = resolveCurrentDevice(list, currentValue);
      const base = current
        ? 'Системное устройство по умолчанию — сейчас «' + current.name + '»'
        : 'Системное устройство по умолчанию';
      if (!current) return base;
      const details = deviceDetails(current);
      return details ? base + ' · ' + details : base;
    }
    const numeric = trimmed.startsWith('index:') ? Number.parseInt(trimmed.slice(6), 10) : NaN;
    const device = Number.isFinite(numeric)
      ? list.find((item) => item.index === numeric) ?? null
      : null;
    if (!device) return 'Устройство не найдено — выберите из списка или «По умолчанию»';
    return deviceDetails(device);
  }

  function levelPercent(level: number | null): number | null {
    if (level === null || !Number.isFinite(level)) return null;
    return Math.max(0, Math.min(100, Math.round(level * 100)));
  }

  function isQuiet(level: number | null): boolean {
    return level !== null && Number.isFinite(level) && level < 0.01;
  }

  /** "default" is sent as null so the backend picks the system default. */
  function deviceForTest(kind: 'input' | 'output'): string | null {
    const raw = kind === 'input' ? audio.input_device : audio.output_device;
    const trimmed = (raw ?? '').trim();
    if (!trimmed || trimmed === 'default') return null;
    return trimmed;
  }

  function normalizeResult(
    result: AudioTestResponse | null | undefined,
    fallback: string,
  ): AudioTestResponse {
    const ok = result?.ok === true;
    const message =
      typeof result?.message === 'string' && result.message.trim()
        ? result.message.trim()
        : ok
          ? fallback
          : 'Проверка не удалась';
    const level = typeof result?.level === 'number' && Number.isFinite(result.level) ? result.level : null;
    return { ok, message, level };
  }

  async function testOutput(): Promise<void> {
    if (testingOutput) return;
    testingOutput = true;
    outputResult = null;
    try {
      const result = await requireApi().audioTest('output', deviceForTest('output'));
      outputResult = normalizeResult(result, 'Звук воспроизведён');
      if (!outputResult.ok) pushToast(outputResult.message, 'error');
    } catch (error) {
      const message = describeError(error);
      outputResult = { ok: false, message, level: null };
      pushToast(message, 'error');
    } finally {
      testingOutput = false;
    }
  }

  async function testInput(): Promise<void> {
    if (testingInput) return;
    testingInput = true;
    inputResult = null;
    try {
      const result = await requireApi().audioTest('input', deviceForTest('input'));
      inputResult = normalizeResult(result, 'Микрофон открыт');
      if (!inputResult.ok) pushToast(inputResult.message, 'error');
    } catch (error) {
      const message = describeError(error);
      inputResult = { ok: false, message, level: null };
      pushToast(message, 'error');
    } finally {
      testingInput = false;
    }
  }

  let calibrating = $state(false);
  let calibration = $state<{ ok: boolean; message: string } | null>(null);

  /** Measure the room noise and store thresholds for the selected mic. */
  async function calibrateMic(): Promise<void> {
    if (calibrating) return;
    calibrating = true;
    calibration = null;
    try {
      const result = await requireApi().calibrateMic(3);
      const ok = result?.ok === true;
      calibration = {
        ok,
        message:
          (typeof result?.message === 'string' && result.message.trim()) ||
          (ok ? 'калибровка сохранена' : 'калибровка не удалась'),
      };
      if (!ok) pushToast(calibration.message, 'error');
      else void load(false);
    } catch (error) {
      const message = describeError(error);
      calibration = { ok: false, message };
      pushToast(message, 'error');
    } finally {
      calibrating = false;
    }
  }

  /** Record 3 s from the selected mic and recognise it (POST /stt/record_test). */
  async function recordAndRecognize(): Promise<void> {
    if (sttPhase !== 'idle') return;
    sttPhase = 'recording';
    sttRecord = null;
    sttRecordError = null;
    sttUnavailable = false;
    // The request is one call; flip the label once the recording part is over.
    sttTimer = window.setTimeout(() => {
      if (sttPhase === 'recording') sttPhase = 'processing';
    }, 3200);
    try {
      sttRecord = await requireApi().sttRecordTest(3);
    } catch (error) {
      if (error instanceof ApiError && (error.status === 404 || error.status === 405 || error.status === 501)) {
        sttUnavailable = true;
      } else {
        sttRecordError = describeError(error);
        pushToast(sttRecordError, 'error');
      }
    } finally {
      if (sttTimer !== null) {
        window.clearTimeout(sttTimer);
        sttTimer = null;
      }
      sttPhase = 'idle';
    }
  }

  const sttButtonText = $derived(
    sttPhase === 'recording'
      ? 'Записываю… 3 с'
      : sttPhase === 'processing'
        ? 'Распознаю…'
        : 'Записать 3 секунды и распознать',
  );

  function sttMetrics(record: SttRecordTestResponse): string {
    const parts: string[] = [];
    if (typeof record.confidence === 'number' && Number.isFinite(record.confidence)) {
      parts.push('Уверенность: ' + record.confidence.toFixed(2));
    }
    if (typeof record.processing_s === 'number' && Number.isFinite(record.processing_s)) {
      parts.push('Обработка: ' + record.processing_s.toFixed(1) + ' с');
    }
    const vad = record.vad;
    if (
      vad &&
      typeof vad.start_s === 'number' &&
      Number.isFinite(vad.start_s) &&
      typeof vad.end_s === 'number' &&
      Number.isFinite(vad.end_s)
    ) {
      parts.push('VAD: ' + vad.start_s.toFixed(1) + '–' + vad.end_s.toFixed(1) + ' с');
    }
    return parts.join(' · ');
  }

  function sampleRateLine(data: Diagnostics): string {
    const mic = data.voice?.mic;
    if (!mic) return '—';
    const actual = typeof mic.samplerate === 'number' && Number.isFinite(mic.samplerate) ? mic.samplerate : null;
    const native = typeof mic.native_samplerate === 'number' && Number.isFinite(mic.native_samplerate) ? mic.native_samplerate : null;
    if (actual === null && native === null) return '—';
    const parts: string[] = [];
    if (actual !== null) parts.push(String(actual) + ' Гц');
    if (native !== null && native !== actual) parts.push('родной ' + String(native) + ' Гц');
    return parts.join(' · ');
  }

  function wakeHeardLine(data: Diagnostics): string {
    const heard = data.voice?.wake?.last_heard;
    return typeof heard === 'string' && heard.trim() ? heard.trim() : '—';
  }

  function sttModelLine(data: Diagnostics): string {
    const stt = data.voice?.stt;
    if (!stt) return '—';
    const parts: string[] = [];
    if (stt.model) parts.push(stt.model);
    if (typeof stt.model_size_mb === 'number' && Number.isFinite(stt.model_size_mb)) {
      parts.push(Math.round(stt.model_size_mb) + ' МБ');
    }
    return parts.join(' · ') || '—';
  }

  function sttLastLine(data: Diagnostics): string {
    const stt = data.voice?.stt;
    if (!stt) return '—';
    const parts: string[] = [];
    if (typeof stt.last_confidence === 'number' && Number.isFinite(stt.last_confidence)) {
      parts.push('уверенность ' + stt.last_confidence.toFixed(2));
    }
    if (typeof stt.last_processing_s === 'number' && Number.isFinite(stt.last_processing_s)) {
      parts.push(stt.last_processing_s.toFixed(1) + ' с');
    }
    return parts.join(' · ') || '—';
  }

  function debugLine(data: Diagnostics): string {
    const debug = data.voice?.debug;
    if (!debug) return '—';
    if (typeof debug.last_wav === 'string' && debug.last_wav.trim()) return debug.last_wav.trim();
    return debug.save_audio ? 'сохранение включено, записи ещё нет' : 'сохранение выключено';
  }

  /** SAPI speaks a phrase, the same STT engine recognises it (POST /audio/test kind=stt). */
  async function testStt(): Promise<void> {
    if (testingStt) return;
    testingStt = true;
    sttResult = null;
    try {
      const result = await requireApi().audioTest('stt', null);
      const ok = result?.ok === true;
      const text = typeof result?.text === 'string' ? result.text.trim() : '';
      const message =
        typeof result?.message === 'string' && result.message.trim()
          ? result.message.trim()
          : ok
            ? 'распознано'
            : 'распознавание не удалось';
      sttResult = { ok, message, level: null, text: ok ? text : '' };
      if (!ok) pushToast(message, 'error');
    } catch (error) {
      const message = describeError(error);
      sttResult = { ok: false, message, level: null, text: '' };
      pushToast(message, 'error');
    } finally {
      testingStt = false;
    }
  }

  async function loadDiagnostics(): Promise<void> {
    diagnosticsLoading = true;
    diagnosticsError = null;
    try {
      diagnostics = await requireApi().getDiagnostics();
    } catch (error) {
      diagnosticsError = 'Не удалось получить диагностику: ' + describeError(error);
    } finally {
      diagnosticsLoading = false;
    }
  }

  async function toggleDiagnostics(): Promise<void> {
    showDiagnostics = !showDiagnostics;
    if (showDiagnostics) await loadDiagnostics();
  }

  function readyText(value: boolean | undefined): string {
    return value ? 'готов' : 'нет';
  }

  function micLine(data: Diagnostics): string {
    const mic = data.voice?.mic;
    if (!mic) return 'нет данных';
    const parts = [mic.ok ? 'ок' : 'ошибка'];
    if (mic.device) parts.push(mic.device);
    if (mic.error) parts.push(mic.error);
    return parts.join(' · ');
  }

  function vadLine(data: Diagnostics): string {
    const vad = data.voice?.vad;
    if (!vad) return 'нет данных';
    const parts = [vad.type || '?', readyText(vad.ready)];
    if (vad.error) parts.push(vad.error);
    return parts.join(' · ');
  }

  function wakeLine(data: Diagnostics): string {
    const wake = data.voice?.wake;
    if (!wake) return 'нет данных';
    const parts = [wake.engine || 'none', readyText(wake.ready)];
    if (wake.error) parts.push(wake.error);
    return parts.join(' · ');
  }

  function sttLine(data: Diagnostics): string {
    const stt = data.voice?.stt;
    if (!stt) return 'нет данных';
    const parts = [readyText(stt.ready)];
    if (stt.model) parts.push(stt.model);
    if (typeof stt.loaded === 'boolean') parts.push(stt.loaded ? 'в памяти' : 'не загружена');
    if (stt.error) parts.push(stt.error);
    return parts.join(' · ');
  }

  function ttsLastMessage(data: Diagnostics): string {
    const last = data.voice?.tts?.last;
    if (!last) return '';
    const message = last['message'];
    if (typeof message === 'string' && message.trim()) return message.trim();
    const error = last['error'];
    if (typeof error === 'string' && error.trim()) return error.trim();
    return '';
  }

  function ttsLine(data: Diagnostics): string {
    const tts = data.voice?.tts;
    if (!tts) return 'нет данных';
    const parts = [tts.engine || '?', tts.backend || 'none'];
    const last = ttsLastMessage(data);
    if (last) parts.push(last);
    return parts.join(' · ');
  }

  function providersLine(data: Diagnostics): string {
    const list = Array.isArray(data.providers) ? data.providers : [];
    if (list.length === 0) return 'нет';
    return list
      .map((provider) => (provider.id || '?') + ' — ' + (provider.key_present ? 'ключ есть' : 'ключа нет'))
      .join('; ');
  }

  function disabledLine(data: Diagnostics): string {
    const list = Array.isArray(data.providers_disabled) ? data.providers_disabled : [];
    return list.join(', ');
  }

  function pathsLine(data: Diagnostics): string {
    const paths = data.paths;
    if (!paths) return '—';
    const parts: string[] = [];
    if (paths.models) parts.push('models: ' + paths.models);
    if (paths.logs) parts.push('logs: ' + paths.logs);
    return parts.join(' · ') || '—';
  }

  function stateLine(data: Diagnostics): string {
    const parts = [data.state || '?'];
    if (data.paused) parts.push('пауза');
    return parts.join(' · ');
  }
</script>

<div class="audio">
  <div class="section-head">
    <div>
      <h3 class="section-title">Звук</h3>
      <p class="section-hint">
        Устройства применяются сразу после сохранения. Проверка — кнопками ниже, перезапуск не нужен.
      </p>
    </div>
    {#if !loading}
      <button class="btn btn--sm" type="button" disabled={refreshing} onclick={() => void load(false)}>
        <Icon name="refresh" size={13} />
        {refreshing ? 'Обновляю…' : 'Обновить список'}
      </button>
    {/if}
  </div>

  {#if loading}
    <div class="audio__state">
      <span class="spinner" aria-hidden="true"></span>
      Опрашиваю устройства…
    </div>
  {:else if loadError}
    <div class="audio__state audio__state--error">
      <p>Не удалось получить список устройств: {loadError}</p>
      <button class="btn btn--sm" type="button" onclick={() => void load(true)}>Повторить</button>
    </div>
  {:else}
    {#if refreshError}
      <p class="audio__error">Не удалось обновить список: {refreshError}</p>
    {/if}

    <section class="group">
      <h4 class="group__title">Устройство ввода (микрофон)</h4>
      <div class="audio__row">
        <select
          class="field__input audio__select"
          bind:value={audio.input_device}
          aria-label="Устройство ввода (микрофон)"
        >
          <option value="default">По умолчанию (система)</option>
          {#each inputDevices as device (device.index)}
            <option value={deviceValue(device)}>{deviceLabel(device)}</option>
          {/each}
          {#if !hasValue(inputDevices, audio.input_device ?? '')}
            <option value={audio.input_device}>{audio.input_device || '— не выбрано —'}</option>
          {/if}
        </select>
        <button
          class="btn btn--sm"
          type="button"
          disabled={refreshing}
          title="Обновить список устройств ввода"
          aria-label="Обновить список устройств ввода"
          onclick={() => void load(false)}
        >
          <Icon name="refresh" size={13} />
        </button>
      </div>
      <p class="audio__hint" class:audio__hint--warn={isMissing(inputDevices, audio.input_device ?? '')}>
        {hintFor(inputDevices, audio.input_device ?? '', devices?.current?.input ?? '')}
      </p>
      {#if inputDevices.length === 0}
        <p class="audio__warn">Микрофоны не найдены.</p>
      {/if}

      <div class="audio__test">
        <button class="btn btn--sm" type="button" disabled={testingInput} onclick={() => void testInput()}>
          <Icon name="mic" size={13} />
          {testingInput ? 'Проверяю…' : 'Проверить микрофон'}
        </button>
        <button class="btn btn--sm" type="button" disabled={calibrating} onclick={() => void calibrateMic()}>
          {calibrating ? 'Калибрую… (3 с тишины)' : 'Калибровка микрофона'}
        </button>
        {#if calibration}
          <div class="result" class:result--ok={calibration.ok} class:result--fail={!calibration.ok} role="status">
            <span class="result__text">{calibration.message}</span>
          </div>
        {/if}
        {#if inputResult}
          {@const percent = levelPercent(inputResult.level)}
          <div class="result" class:result--ok={inputResult.ok} class:result--fail={!inputResult.ok} role="status">
            <span class="result__text">{inputResult.message}</span>
            {#if percent !== null}
              <span class="result__level">Уровень: {percent}%</span>
            {/if}
            {#if isQuiet(inputResult.level) && !inputResult.message.toLowerCase().includes('тишина')}
              <span class="result__quiet">тишина</span>
            {/if}
          </div>
        {/if}
      </div>

    </section>

    <section class="group">
      <h4 class="group__title">Тест распознавания</h4>
      <p class="audio__hint">
        Записывает 3 секунды с выбранного микрофона и распознаёт тем же движком, что и в работе.
        Остальной интерфейс не блокируется.
      </p>
      <div class="audio__test">
        <button class="btn" type="button" disabled={sttPhase !== 'idle'} onclick={() => void recordAndRecognize()}>
          <Icon name="mic" size={13} />
          {sttButtonText}
        </button>
      </div>

      {#if sttRecord}
        <div class="stt-result" class:stt-result--fail={!sttRecord.ok} role="status">
          {#if sttRecord.ok}
            <p class="stt-result__text">
              {sttRecord.text.trim() ? 'Распознано: «' + sttRecord.text + '»' : 'ничего не распознано'}
            </p>
            {#if sttMetrics(sttRecord)}
              <p class="stt-result__metrics">{sttMetrics(sttRecord)}</p>
            {/if}
          {:else}
            <p class="stt-result__text">{sttRecord.message || 'не удалось распознать'}</p>
          {/if}
          {#if sttRecord.wav_path}
            <p class="stt-result__path">Запись сохранена: {sttRecord.wav_path}</p>
            <p class="stt-result__hint">Пришлите этот файл, если текст неверный</p>
          {/if}
        </div>
      {:else if sttRecordError}
        <p class="audio__error">Тест распознавания не удался: {sttRecordError}</p>
      {:else if sttUnavailable}
        <p class="audio__hint">Тест пока недоступен.</p>
      {/if}

      <div class="stt-secondary">
        <span class="audio__hint">Без микрофона: SAPI синтезирует фразу, STT её распознаёт (2–5 с).</span>
        <button class="btn btn--sm" type="button" disabled={testingStt} onclick={() => void testStt()}>
          <Icon name="check" size={13} />
          {testingStt ? 'Распознаю…' : 'Проверить распознавание (SAPI)'}
        </button>
        {#if sttResult}
          <div class="result" class:result--ok={sttResult.ok} class:result--fail={!sttResult.ok} role="status">
            <span class="result__text">
              {sttResult.ok
                ? sttResult.text
                  ? 'Распознано: «' + sttResult.text + '»'
                  : sttResult.message
                : sttResult.message}
            </span>
          </div>
        {/if}
      </div>
    </section>

    <section class="group">
      <h4 class="group__title">Устройство вывода (динамики/наушники)</h4>
      <div class="audio__row">
        <select
          class="field__input audio__select"
          bind:value={audio.output_device}
          aria-label="Устройство вывода (динамики, наушники)"
        >
          <option value="default">По умолчанию (система)</option>
          {#each outputDevices as device (device.index)}
            <option value={deviceValue(device)}>{deviceLabel(device)}</option>
          {/each}
          {#if !hasValue(outputDevices, audio.output_device ?? '')}
            <option value={audio.output_device}>{audio.output_device || '— не выбрано —'}</option>
          {/if}
        </select>
        <button
          class="btn btn--sm"
          type="button"
          disabled={refreshing}
          title="Обновить список устройств вывода"
          aria-label="Обновить список устройств вывода"
          onclick={() => void load(false)}
        >
          <Icon name="refresh" size={13} />
        </button>
      </div>
      <p class="audio__hint" class:audio__hint--warn={isMissing(outputDevices, audio.output_device ?? '')}>
        {hintFor(outputDevices, audio.output_device ?? '', devices?.current?.output ?? '')}
      </p>

      {#if outputDevices.length === 0}
        <p class="audio__error">
          Устройства вывода не найдены. Подключите наушники или динамики и нажмите «Обновить».
        </p>
        {#if playbackMessage}
          <p class="audio__hint">Вывод звука: {playbackMessage}</p>
        {/if}
      {:else if playbackBad && playbackMessage}
        <p class="audio__error">
          Вывод звука недоступен{(devices?.playback?.backend ?? '') ? ' (' + devices?.playback?.backend + ')' : ''}: {playbackMessage}
        </p>
      {/if}

      <div class="audio__test">
        <button class="btn btn--sm" type="button" disabled={testingOutput} onclick={() => void testOutput()}>
          <Icon name="play" size={13} />
          {testingOutput ? 'Проверяю…' : 'Проверить звук'}
        </button>
        {#if outputResult}
          <div class="result" class:result--ok={outputResult.ok} class:result--fail={!outputResult.ok} role="status">
            <span class="result__text">{outputResult.message}</span>
          </div>
        {/if}
      </div>
    </section>

    <section class="group">
      <div class="diag__head">
        <h4 class="group__title">Диагностика</h4>
        <button class="btn btn--sm" type="button" onclick={() => void toggleDiagnostics()}>
          {showDiagnostics ? 'Скрыть диагностику' : 'Показать диагностику'}
        </button>
      </div>
      {#if showDiagnostics}
        {#if diagnosticsLoading}
          <div class="audio__state">
            <span class="spinner" aria-hidden="true"></span>
            Читаю диагностику…
          </div>
        {:else if diagnosticsError}
          <p class="audio__error">{diagnosticsError}</p>
          <button class="btn btn--sm" type="button" onclick={() => void loadDiagnostics()}>Повторить</button>
        {:else if diagnostics}
          <dl class="diag">
            <dt>Состояние</dt>
            <dd>{stateLine(diagnostics)}</dd>
            <dt>Микрофон</dt>
            <dd>{micLine(diagnostics)}</dd>
            <dt>Sample rate</dt>
            <dd>{sampleRateLine(diagnostics)}</dd>
            <dt>VAD</dt>
            <dd>{vadLine(diagnostics)}</dd>
            <dt>Wake</dt>
            <dd>{wakeLine(diagnostics)}</dd>
            <dt>Wake слышал</dt>
            <dd>{wakeHeardLine(diagnostics)}</dd>
            <dt>STT</dt>
            <dd>{sttLine(diagnostics)}</dd>
            <dt>Модель STT</dt>
            <dd>{sttModelLine(diagnostics)}</dd>
            <dt>STT последний</dt>
            <dd>{sttLastLine(diagnostics)}</dd>
            <dt>TTS</dt>
            <dd>{ttsLine(diagnostics)}</dd>
            <dt>Провайдеры</dt>
            <dd>{providersLine(diagnostics)}</dd>
            {#if disabledLine(diagnostics)}
              <dt>Отключены</dt>
              <dd>{disabledLine(diagnostics)}</dd>
            {/if}
            <dt>Отладка</dt>
            <dd>{debugLine(diagnostics)}</dd>
            <dt>Пути</dt>
            <dd>{pathsLine(diagnostics)}</dd>
          </dl>
        {/if}
      {/if}
    </section>

    <p class="audio__hint">
      «По умолчанию» сохраняется как «default», конкретное устройство — как «index:&lt;номер&gt;».
      Если переткнуть наушники, нумерация может измениться — нажмите «Обновить список».
    </p>
  {/if}
</div>

<style>
  .audio {
    display: flex;
    flex-direction: column;
    gap: 10px;
  }

  .audio__row {
    display: flex;
    align-items: center;
    gap: 6px;
    min-width: 0;
  }

  .audio__select {
    flex: 1 1 auto;
    min-width: 0;
  }

  .audio__hint {
    margin: 0;
    font-size: 10.5px;
    line-height: 1.35;
    color: var(--text-dim);
    overflow-wrap: anywhere;
  }

  .audio__hint--warn {
    color: var(--warn);
  }

  .audio__warn {
    margin: 0;
    font-size: 11.5px;
    color: var(--warn);
    background: rgba(233, 179, 79, 0.08);
    border: 1px solid rgba(233, 179, 79, 0.3);
    border-radius: 7px;
    padding: 6px 8px;
  }

  .audio__error {
    margin: 0;
    font-size: 11.5px;
    line-height: 1.4;
    color: #f4a3a3;
    background: rgba(240, 106, 106, 0.08);
    border: 1px solid rgba(240, 106, 106, 0.3);
    border-radius: 7px;
    padding: 6px 8px;
  }

  .audio__state {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 14px 6px;
    color: var(--text-muted);
    font-size: 12.5px;
  }

  .audio__state--error {
    flex-direction: column;
    align-items: flex-start;
    color: #f4a3a3;
  }

  .audio__state--error p {
    margin: 0;
    line-height: 1.45;
  }

  .audio__test {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
  }

  .result {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 4px 8px;
    flex: 1 1 100%;
    padding: 6px 8px;
    border-radius: 7px;
    font-size: 11.5px;
    line-height: 1.4;
    overflow-wrap: anywhere;
  }

  .result--ok {
    color: var(--ok);
    background: rgba(78, 201, 164, 0.08);
    border: 1px solid rgba(78, 201, 164, 0.25);
  }

  .result--fail {
    color: #f4a3a3;
    background: rgba(240, 106, 106, 0.08);
    border: 1px solid rgba(240, 106, 106, 0.28);
  }

  .result__level {
    font-variant-numeric: tabular-nums;
    color: var(--text);
  }

  .result__quiet {
    color: var(--warn);
  }

  .group__title {
    margin: 0;
    font-size: 12.5px;
    font-weight: 600;
    color: var(--text);
  }

  .diag__head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
  }

  .stt-result {
    display: flex;
    flex-direction: column;
    gap: 3px;
    padding: 8px;
    border-radius: 8px;
    border: 1px solid rgba(78, 201, 164, 0.25);
    background: rgba(78, 201, 164, 0.07);
  }

  .stt-result--fail {
    border-color: rgba(240, 106, 106, 0.3);
    background: rgba(240, 106, 106, 0.08);
  }

  .stt-result__text {
    margin: 0;
    font-size: 13px;
    font-weight: 600;
    line-height: 1.4;
    color: var(--text);
    overflow-wrap: anywhere;
  }

  .stt-result--fail .stt-result__text {
    color: #f4a3a3;
  }

  .stt-result__metrics {
    margin: 0;
    font-size: 11px;
    line-height: 1.4;
    color: var(--text-muted);
  }

  .stt-result__path {
    margin: 0;
    font-size: 10.5px;
    line-height: 1.4;
    color: var(--text-dim);
    overflow-wrap: anywhere;
  }

  .stt-result__hint {
    margin: 0;
    font-size: 10.5px;
    line-height: 1.4;
    color: var(--text-muted);
  }

  .stt-secondary {
    display: flex;
    flex-direction: column;
    gap: 5px;
    padding-top: 6px;
    border-top: 1px dashed var(--border);
  }

  .diag {
    display: grid;
    grid-template-columns: 88px minmax(0, 1fr);
    gap: 4px 8px;
    margin: 0;
    font-size: 11px;
    line-height: 1.4;
  }

  .diag dt {
    color: var(--text-muted);
    white-space: nowrap;
  }

  .diag dd {
    margin: 0;
    color: var(--text);
    overflow-wrap: anywhere;
  }
</style>
