<script lang="ts">
  import { onMount } from 'svelte';
  import ApiKeyBanner from './components/ApiKeyBanner.svelte';
  import ConnectionGate from './components/ConnectionGate.svelte';
  import History from './components/History.svelte';
  import Icon from './components/Icon.svelte';
  import InputBar from './components/InputBar.svelte';
  import SettingsDialog from './components/SettingsDialog.svelte';
  import SidecarBanner from './components/SidecarBanner.svelte';
  import StatusPill from './components/StatusPill.svelte';
  import ToastHost from './components/ToastHost.svelte';
  import { isTauriRuntime } from './lib/tauri';
  import { bootstrap, closeSettings, editMessage, hideToTray, openConfig, openSettings, ui } from './lib/store.svelte';

  const sidecarNotice = $derived.by(() => {
    if (ui.connection !== 'offline') return null;
    if (!ui.bridge) {
      return isTauriRuntime() ? ui.sidecar?.error || 'Сайдкар не запущен' : null;
    }
    return ui.sidecar?.error || 'Нет ответа на 127.0.0.1:' + String(ui.bridge.port);
  });

  onMount(() => {
    void bootstrap();

    const onKeydown = (event: KeyboardEvent): void => {
      if (event.key !== 'Escape') return;
      if (ui.settingsOpen) {
        event.preventDefault();
        closeSettings();
        return;
      }
      event.preventDefault();
      void hideToTray();
    };

    window.addEventListener('keydown', onKeydown);
    return () => window.removeEventListener('keydown', onKeydown);
  });
</script>

<div class="app">
  <header class="app__head">
    <div class="app__bar">
      <div class="brand">
        <span class="brand__mark" aria-hidden="true">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
            <path d="M4 10v4" />
            <path d="M8 7v10" />
            <path d="M12 4v16" />
            <path d="M16 8v8" />
            <path d="M20 10v4" />
          </svg>
        </span>
        <span class="brand__name">VoiceRunaCore</span>
      </div>
      <div class="app__actions">
        <button class="btn btn--icon btn--ghost" type="button" title="Настройки" aria-label="Настройки" onclick={() => openSettings()}>
          <Icon name="settings" size={17} />
        </button>
        <button
          class="btn btn--icon btn--ghost"
          type="button"
          title="Открыть папку конфига"
          aria-label="Открыть папку конфига"
          onclick={() => void openConfig()}
        >
          <Icon name="folder" size={17} />
        </button>
        <button
          class="btn btn--icon btn--ghost"
          type="button"
          title="Свернуть в трей (Esc)"
          aria-label="Свернуть в трей"
          onclick={() => void hideToTray()}
        >
          <Icon name="tray" size={17} />
        </button>
      </div>
    </div>

    <StatusPill
      state={ui.state}
      paused={ui.paused}
      download={ui.download}
      connection={ui.connection}
      wsConnected={ui.wsConnected}
    />
  </header>

  {#if sidecarNotice}
    <SidecarBanner message={sidecarNotice} />
  {/if}

  {#if ui.apiKeyIssue}
    <ApiKeyBanner issue={ui.apiKeyIssue} />
  {/if}

  <History messages={ui.messages} apps={ui.apps} onEdit={editMessage} />

  <InputBar />

  <ToastHost />
</div>

{#if ui.settingsOpen}
  <SettingsDialog />
{/if}

{#if !ui.bridge && ui.connection === 'offline' && !isTauriRuntime()}
  <ConnectionGate />
{/if}

<style>
  .app {
    display: flex;
    flex-direction: column;
    height: 100%;
    min-height: 0;
    overflow: hidden;
  }

  .app__head {
    flex: 0 0 auto;
    padding: 9px 12px 10px;
    border-bottom: 1px solid var(--border);
    background: linear-gradient(180deg, #191d28 0%, #141720 100%);
  }

  .app__bar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
  }

  .brand {
    display: flex;
    align-items: center;
    gap: 7px;
    min-width: 0;
  }

  .brand__mark {
    display: inline-flex;
    color: var(--accent);
  }

  .brand__name {
    font-weight: 600;
    font-size: 14px;
    letter-spacing: 0.2px;
  }

  .app__actions {
    display: flex;
    align-items: center;
    gap: 1px;
  }
</style>
