<script lang="ts">
  import { describeError, requireApi } from '../../lib/api';
  import { pushToast } from '../../lib/store.svelte';
  import type { TtsConfig, VoiceInfo } from '../../lib/types';
  import Icon from '../Icon.svelte';

  interface Props {
    tts: TtsConfig;
    voices: VoiceInfo[];
    voicesFromApi: boolean;
    /** Jump to the "Звук" tab when preview failed because playback is broken. */
    onOpenAudio?: () => void;
  }

  let { tts, voices, voicesFromApi, onOpenAudio }: Props = $props();

  let previewText = $state('Привет! Меня зовут Ассистент. Так я звучу.');
  let previewing = $state(false);
  let previewResult = $state<{ ok: boolean; message: string } | null>(null);
  let customEngine = $state('edge');
  let customName = $state('');
  let customLabel = $state('');
  let customError = $state<string | null>(null);

  const options = $derived.by(() => {
    const seen = new Set<string>();
    const result: VoiceInfo[] = [];
    const all = [...voices, ...(tts.voices ?? [])];
    for (const voice of all) {
      if (!voice || typeof voice.name !== 'string' || !voice.name.trim()) continue;
      const engine = voice.engine || 'edge';
      const key = engine + ':' + voice.name;
      if (seen.has(key)) continue;
      seen.add(key);
      result.push({ engine, name: voice.name, label: voice.label || voice.name });
    }
    return result;
  });

  const customVoices = $derived(
    (tts.voices ?? []).filter((voice) => (voice.engine || 'edge') !== 'edge'),
  );

  const rates = ['0%', '+10%', '+20%', '-10%'];

  const engineModes = [
    { value: 'auto', label: 'Авто (локальный голос, иначе онлайн edge-tts, иначе SAPI)' },
    { value: 'piper', label: 'Локальный нейроголос Piper (офлайн)' },
    { value: 'edge', label: 'Только онлайн edge-tts' },
    { value: 'sapi', label: 'Только офлайн SAPI' },
  ];

  let openingFolder = $state(false);

  async function openVoicesFolder(): Promise<void> {
    if (openingFolder) return;
    openingFolder = true;
    try {
      const response = await requireApi().openVoicesFolder();
      if (response?.ok) {
        pushToast('Папка голосов открыта: положите туда .onnx и .onnx.json', 'success');
      } else {
        pushToast(response?.message || 'не удалось открыть папку голосов', 'error');
      }
    } catch (error) {
      pushToast(describeError(error), 'error');
    } finally {
      openingFolder = false;
    }
  }

  async function preview(): Promise<void> {
    if (!(tts.voice ?? '').trim()) {
      pushToast('Сначала выберите голос', 'error');
      return;
    }
    const text = previewText.trim();
    if (!text) {
      pushToast('Введите текст для прослушивания', 'error');
      return;
    }
    previewing = true;
    previewResult = null;
    try {
      const response = await requireApi().ttsPreview(tts.voice, text);
      const ok = response?.ok === true;
      const message =
        typeof response?.message === 'string' && response.message.trim()
          ? response.message.trim()
          : ok
            ? 'озвучка отправлена на устройство вывода'
            : 'не удалось воспроизвести звук';
      previewResult = { ok, message };
      if (!ok) pushToast(message, 'error');
    } catch (error) {
      const message = describeError(error);
      previewResult = { ok: false, message };
      pushToast(message, 'error');
    } finally {
      previewing = false;
    }
  }

  function addCustom(): void {
    const name = customName.trim();
    const label = customLabel.trim();
    if (!name) {
      customError = 'Укажите имя голоса, например ru-RU-DmitryNeural.';
      return;
    }
    if (!label) {
      customError = 'Укажите подпись для списка.';
      return;
    }
    if ((tts.voices ?? []).some((voice) => voice.name === name && (voice.engine || 'edge') === customEngine)) {
      customError = 'Такой голос уже есть в конфиге.';
      return;
    }
    customError = null;
    tts.voices.push({ engine: customEngine, name, label });
    tts.voice = name;
    customName = '';
    customLabel = '';
    pushToast('Голос добавлен в конфиг', 'success');
  }

  function removeCustom(name: string): void {
    const index = tts.voices.findIndex((voice) => voice.name === name);
    if (index >= 0) tts.voices.splice(index, 1);
    if (tts.voice === name) tts.voice = options[0]?.name ?? '';
  }

  function voiceTitle(voice: VoiceInfo): string {
    return voice.label + ' · ' + voice.name;
  }
</script>

<div class="voice">
  <section class="group">
    <h3 class="section-title">Голос синтеза</h3>
    <p class="section-hint">
      {#if voicesFromApi}
        Список получен от edge-tts. Голос применяется сразу после сохранения.
      {:else}
        Список edge-tts не получен — показаны голоса из config.json.
      {/if}
    </p>

    <label class="field">
      <span class="field__label">Движок синтеза</span>
      <select class="field__input" bind:value={tts.engine}>
        {#each engineModes as mode (mode.value)}
          <option value={mode.value}>{mode.label}</option>
        {/each}
        {#if !engineModes.some((mode) => mode.value === tts.engine)}
          <option value={tts.engine}>{tts.engine || '— не выбрано —'}</option>
        {/if}
      </select>
      <span class="field__hint">Офлайн-голос SAPI зависит от установленных голосов Windows.</span>
    </label>

    <label class="field">
      <span class="field__label">Голос</span>
      <select class="field__input" bind:value={tts.voice}>
        {#each options as voice (voice.engine + ':' + voice.name)}
          <option value={voice.name}>{voiceTitle(voice)}</option>
        {/each}
        {#if !options.some((voice) => voice.name === tts.voice)}
          <option value={tts.voice}>{tts.voice || '— не выбран —'}</option>
        {/if}
      </select>
    </label>

    <div class="field">
      <span class="field__label">Скорость</span>
      <input class="field__input" bind:value={tts.rate} placeholder="+10%" spellcheck="false" />
      <div class="rates">
        {#each rates as rate (rate)}
          <button
            class="btn btn--sm"
            class:btn--active={tts.rate === rate}
            type="button"
            onclick={() => (tts.rate = rate)}
          >
            {rate}
          </button>
        {/each}
      </div>
      <span class="field__hint">Формат edge-tts: +10% или -5%.</span>
    </div>

    <label class="field">
      <span class="field__label">Текст для проверки</span>
      <textarea class="field__input field__input--area" bind:value={previewText} rows="2" spellcheck="false"></textarea>
    </label>

    <div class="voice__row">
      <button class="btn" type="button" disabled={previewing} onclick={() => void preview()}>
        <Icon name="play" size={13} />
        {previewing ? 'Озвучиваю…' : 'Прослушать'}
      </button>
      <button class="btn" type="button" disabled={openingFolder} onclick={() => void openVoicesFolder()}>
        {openingFolder ? 'Открываю…' : 'Открыть папку голосов'}
      </button>
      <span class="field__hint">{tts.voice || 'голос не выбран'}</span>
    </div>
    <p class="field__hint">
      Свой голос: положите файлы <code>имя.onnx</code> и <code>имя.onnx.json</code> в папку
      голосов, нажмите «Сохранить» и выберите «Piper: имя» в списке — пересборка не нужна.
    </p>
    {#if previewResult}
      <p
        class="preview-result"
        class:preview-result--ok={previewResult.ok}
        class:preview-result--fail={!previewResult.ok}
        role="status"
      >
        {previewResult.ok ? 'Прозвучало: ' + previewResult.message : previewResult.message}
      </p>
      {#if !previewResult.ok && onOpenAudio}
        <button class="btn btn--sm preview-result__action" type="button" onclick={onOpenAudio}>
          Проверить устройство вывода
        </button>
      {/if}
    {/if}
  </section>

  <section class="group">
    <h3 class="section-title">Свои голоса</h3>
    <p class="section-hint">Записи добавляются в tts.voices конфига и доступны в списке выше.</p>

    {#if customVoices.length === 0}
      <p class="empty-note">Пока только голоса edge-tts.</p>
    {:else}
      <ul class="voices">
        {#each customVoices as voice (voice.engine + ':' + voice.name)}
          <li class="voices__item">
            <span class="voices__text">
              <strong>{voice.label}</strong>
              <span class="voices__name">{voice.engine} · {voice.name}</span>
            </span>
            <button
              class="btn btn--icon btn--ghost"
              type="button"
              aria-label={'Удалить голос ' + voice.label}
              title="Удалить из конфига"
              onclick={() => removeCustom(voice.name)}
            >
              <Icon name="trash" size={14} />
            </button>
          </li>
        {/each}
      </ul>
    {/if}

    <div class="grid-2">
      <label class="field">
        <span class="field__label">Движок</span>
        <select class="field__input" bind:value={customEngine}>
          <option value="edge">edge</option>
          <option value="silero">silero</option>
        </select>
      </label>
      <label class="field">
        <span class="field__label">Имя</span>
        <input class="field__input" bind:value={customName} placeholder="ru-RU-DmitryNeural" spellcheck="false" />
      </label>
    </div>
    <label class="field">
      <span class="field__label">Подпись</span>
      <input class="field__input" bind:value={customLabel} placeholder="Дмитрий (мужской)" />
    </label>
    {#if customError}
      <p class="field__error">{customError}</p>
    {/if}
    <div class="voice__row">
      <button class="btn" type="button" onclick={addCustom}>
        <Icon name="plus" size={13} />
        Добавить голос
      </button>
    </div>
  </section>
</div>

<style>
  .voice {
    display: flex;
    flex-direction: column;
    gap: 10px;
  }

  .rates {
    display: flex;
    gap: 4px;
    flex-wrap: wrap;
    margin-top: 2px;
  }

  .btn--active {
    border-color: var(--accent);
    color: var(--accent);
    background: var(--accent-soft);
  }

  :global(.field__input--area) {
    resize: vertical;
    min-height: 48px;
    line-height: 1.4;
  }

  .voice__row {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
  }

  .preview-result {
    margin: 0;
    padding: 6px 8px;
    border-radius: 7px;
    font-size: 11.5px;
    line-height: 1.4;
    overflow-wrap: anywhere;
  }

  .preview-result--ok {
    color: var(--ok);
    background: rgba(78, 201, 164, 0.08);
    border: 1px solid rgba(78, 201, 164, 0.25);
  }

  .preview-result--fail {
    color: #f4a3a3;
    background: rgba(240, 106, 106, 0.08);
    border: 1px solid rgba(240, 106, 106, 0.28);
  }

  .preview-result__action {
    align-self: flex-start;
    margin-top: 4px;
  }

  .voices {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  .voices__item {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 5px 6px 5px 8px;
    border-radius: var(--radius-sm);
    border: 1px solid var(--border);
    background: var(--bg-elev-2);
  }

  .voices__text {
    display: flex;
    flex-direction: column;
    min-width: 0;
    flex: 1 1 auto;
    font-size: 12px;
  }

  .voices__name {
    font-size: 10.5px;
    color: var(--text-dim);
    overflow-wrap: anywhere;
  }
</style>
