<script lang="ts">
  import { onMount } from 'svelte';
  import { describeError, requireApi } from '../lib/api';
  import { diffConfig, hasChanges } from '../lib/config-diff';
  import { closeSettings, pushToast, refreshApps, ui } from '../lib/store.svelte';
  import { validateSettings } from '../lib/validate';
  import type { AppsMap, Config, SettingsTab, VoiceInfo } from '../lib/types';
  import AppsTab from './tabs/AppsTab.svelte';
  import AudioTab from './tabs/AudioTab.svelte';
  import ModelsTab from './tabs/ModelsTab.svelte';
  import OtherTab from './tabs/OtherTab.svelte';
  import VoiceTab from './tabs/VoiceTab.svelte';
  import Icon from './Icon.svelte';

  const TABS: { id: SettingsTab; label: string }[] = [
    { id: 'models', label: 'Модели' },
    { id: 'voice', label: 'Голос' },
    { id: 'audio', label: 'Звук' },
    { id: 'apps', label: 'Программы' },
    { id: 'other', label: 'Прочее' },
  ];

  let tab = $state<SettingsTab>(ui.settingsTab);
  let loading = $state(true);
  let loadError = $state<string | null>(null);
  let saving = $state(false);
  let savedMessage = $state<string | null>(null);
  let config = $state<Config | null>(null);
  let originalConfig = $state<Config | null>(null);
  let apps = $state<AppsMap | null>(null);
  let originalApps = $state<AppsMap | null>(null);
  let voices = $state<VoiceInfo[]>([]);
  let voicesFromApi = $state(false);
  let dialogEl: HTMLDivElement | undefined = $state();

  const dirty = $derived.by(() => {
    if (!config || !originalConfig || !apps || !originalApps) return false;
    return (
      hasChanges(diffConfig(originalConfig, config)) ||
      JSON.stringify(apps) !== JSON.stringify(originalApps)
    );
  });

  onMount(() => {
    void load();
    queueMicrotask(() => dialogEl?.focus());
  });

  /** config.json from an older build may miss the audio branch entirely. */
  function withAudioDevices(cfg: Config): Config {
    const audio = cfg.audio;
    cfg.audio = {
      input_device: typeof audio?.input_device === 'string' ? audio.input_device : 'default',
      output_device: typeof audio?.output_device === 'string' ? audio.output_device : 'default',
    };
    return cfg;
  }

  async function load(): Promise<void> {
    loading = true;
    loadError = null;
    try {
      const client = requireApi();
      const [configResult, appsResult, voicesResult] = await Promise.allSettled([
        client.getConfig(),
        client.getApps(),
        client.voices(),
      ]);

      if (configResult.status !== 'fulfilled') throw configResult.reason;
      config = withAudioDevices(configResult.value);
      originalConfig = $state.snapshot(config) as unknown as Config;

      if (appsResult.status === 'fulfilled') {
        apps = appsResult.value;
        originalApps = $state.snapshot(apps) as unknown as AppsMap;
      } else {
        apps = {};
        originalApps = {};
        pushToast('Не удалось загрузить apps.json: ' + describeError(appsResult.reason), 'error');
      }

      if (voicesResult.status === 'fulfilled' && Array.isArray(voicesResult.value.voices)) {
        voices = voicesResult.value.voices;
        voicesFromApi = true;
      } else {
        voices = config.tts.voices ?? [];
        voicesFromApi = false;
      }
    } catch (error) {
      loadError = describeError(error);
    } finally {
      loading = false;
    }
  }

  async function save(): Promise<void> {
    if (saving || !config || !originalConfig || !apps || !originalApps) return;

    const issues = validateSettings(config, apps);
    if (issues.length > 0) {
      tab = issues[0].tab;
      savedMessage = null;
      pushToast(issues[0].message, 'error');
      return;
    }

    const patch = diffConfig(originalConfig, config);
    const appsChanged = JSON.stringify(apps) !== JSON.stringify(originalApps);
    if (!hasChanges(patch) && !appsChanged) {
      savedMessage = 'Нет изменений';
      return;
    }

    saving = true;
    savedMessage = null;
    try {
      const client = requireApi();
      if (hasChanges(patch)) {
        const updated = await client.putConfig(patch);
        if (updated && typeof updated === 'object') {
          config = withAudioDevices(updated);
          originalConfig = $state.snapshot(config) as unknown as Config;
        } else {
          originalConfig = $state.snapshot(config) as unknown as Config;
        }
      }
      if (appsChanged) {
        await client.putApps($state.snapshot(apps) as unknown as AppsMap);
        originalApps = $state.snapshot(apps) as unknown as AppsMap;
      }
      savedMessage = 'Сохранено';
      pushToast('Настройки сохранены', 'success');
      void refreshApps();
      window.setTimeout(() => {
        if (savedMessage === 'Сохранено') savedMessage = null;
      }, 4000);
    } catch (error) {
      savedMessage = null;
      pushToast(describeError(error), 'error');
    } finally {
      saving = false;
    }
  }

  function onDialogKeydown(event: KeyboardEvent): void {
    if (event.key !== 'Tab') return;
    const root = dialogEl;
    if (!root) return;
    const focusables = Array.from(
      root.querySelectorAll<HTMLElement>(
        'button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
      ),
    ).filter((element) => element.offsetParent !== null);
    if (focusables.length === 0) return;
    const first = focusables[0];
    const last = focusables[focusables.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }
</script>

<div class="overlay">
  <div
    class="dialog"
    bind:this={dialogEl}
    role="dialog"
    aria-modal="true"
    aria-labelledby="settings-title"
    tabindex="-1"
    onkeydown={onDialogKeydown}
  >
    <header class="dialog__head">
      <h2 class="dialog__title" id="settings-title">Настройки</h2>
      <button class="btn btn--icon btn--ghost" type="button" aria-label="Закрыть настройки" title="Закрыть (Esc)" onclick={closeSettings}>
        <Icon name="close" size={17} />
      </button>
    </header>

    <div class="tabs" role="tablist" aria-label="Разделы настроек">
      {#each TABS as item (item.id)}
        <button
          class="tab"
          class:tab--active={tab === item.id}
          type="button"
          role="tab"
          id={'tab-' + item.id}
          aria-controls={'panel-' + item.id}
          aria-selected={tab === item.id}
          onclick={() => (tab = item.id)}
        >
          {item.label}
        </button>
      {/each}
    </div>

    <div class="dialog__body">
      {#if loading}
        <div class="dialog__state">
          <span class="spinner" aria-hidden="true"></span>
          Загружаю конфиг…
        </div>
      {:else if loadError}
        <div class="dialog__state dialog__state--error">
          <p>{loadError}</p>
          <button class="btn" type="button" onclick={() => void load()}>Повторить</button>
        </div>
      {:else if config && apps}
        <div class="tabpanel" id={'panel-' + tab} role="tabpanel" aria-labelledby={'tab-' + tab}>
          {#if tab === 'models'}
            <ModelsTab providers={config.llm.providers} />
          {:else if tab === 'voice'}
            <VoiceTab tts={config.tts} {voices} {voicesFromApi} onOpenAudio={() => (tab = 'audio')} />
          {:else if tab === 'audio'}
            <AudioTab audio={config.audio} />
          {:else if tab === 'apps'}
            <AppsTab {apps} />
          {:else}
            <OtherTab {config} />
          {/if}
        </div>
      {/if}
    </div>

    <footer class="dialog__foot">
      <span class="dialog__status" aria-live="polite">
        {#if savedMessage}
          <span class="dialog__saved">{savedMessage}</span>
        {:else if dirty}
          <span class="dialog__dirty">есть несохранённые изменения</span>
        {/if}
      </span>
      <button class="btn btn--ghost" type="button" onclick={closeSettings}>Закрыть</button>
      <button
        class="btn btn--primary"
        type="button"
        onclick={() => void save()}
        disabled={saving || loading || Boolean(loadError)}
      >
        {saving ? 'Сохраняю…' : 'Сохранить'}
      </button>
    </footer>
  </div>
</div>

<style>
  .overlay {
    position: fixed;
    inset: 0;
    z-index: 50;
    display: flex;
    padding: 8px;
    background: rgba(7, 9, 14, 0.6);
    backdrop-filter: blur(2px);
  }

  .dialog {
    display: flex;
    flex-direction: column;
    width: 100%;
    height: 100%;
    min-height: 0;
    border-radius: 12px;
    border: 1px solid var(--border-strong);
    background: var(--bg-elev);
    box-shadow: var(--shadow);
    overflow: hidden;
  }

  .dialog:focus-visible {
    outline: none;
  }

  .dialog__head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    padding: 8px 10px 6px 12px;
  }

  .dialog__title {
    margin: 0;
    font-size: 14px;
    font-weight: 600;
  }

  .tabs {
    display: flex;
    gap: 2px;
    padding: 0 8px;
    border-bottom: 1px solid var(--border);
    overflow-x: auto;
    flex: 0 0 auto;
  }

  .tab {
    flex: 0 0 auto;
    padding: 7px 9px;
    border: none;
    border-bottom: 2px solid transparent;
    background: transparent;
    color: var(--text-muted);
    font-size: 12.5px;
    cursor: pointer;
    border-radius: 6px 6px 0 0;
  }

  .tab:hover {
    color: var(--text);
    background: rgba(255, 255, 255, 0.04);
  }

  .tab--active {
    color: var(--text);
    border-bottom-color: var(--accent);
    font-weight: 600;
  }

  .dialog__body {
    flex: 1 1 auto;
    min-height: 0;
    overflow-y: auto;
    overflow-x: hidden;
    padding: 12px;
  }

  .dialog__state {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 18px 4px;
    color: var(--text-muted);
    font-size: 12.5px;
  }

  .dialog__state--error {
    flex-direction: column;
    align-items: flex-start;
    color: #f4a3a3;
  }

  .dialog__state--error p {
    margin: 0;
    line-height: 1.45;
  }

  .dialog__foot {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 9px 12px;
    border-top: 1px solid var(--border);
    background: #14171f;
    flex: 0 0 auto;
  }

  .dialog__status {
    flex: 1 1 auto;
    min-width: 0;
    font-size: 11.5px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .dialog__saved {
    color: var(--ok);
  }

  .dialog__dirty {
    color: var(--warn);
  }
</style>
