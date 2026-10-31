<script lang="ts">
  import { onMount } from 'svelte';
  import { describeError, requireApi } from '../../lib/api';
  import { pushToast } from '../../lib/store.svelte';
  import type { PluginInfo } from '../../lib/types';

  let plugins = $state<PluginInfo[]>([]);
  let loading = $state(true);
  let error = $state<string | null>(null);
  let busy = $state<string | null>(null);

  onMount(() => {
    void load();
  });

  async function load(): Promise<void> {
    loading = true;
    error = null;
    try {
      const response = await requireApi().getPlugins();
      plugins = response.plugins;
    } catch (issue) {
      error = describeError(issue);
    } finally {
      loading = false;
    }
  }

  async function toggle(plugin: PluginInfo): Promise<void> {
    if (busy) return;
    busy = plugin.name;
    try {
      const response = await requireApi().togglePlugin(plugin.name, !plugin.enabled);
      plugins = response.plugins;
      const now = plugins.find((item) => item.name === plugin.name);
      pushToast(
        (now?.title ?? plugin.name) + (now?.enabled ? ' включён' : ' выключен'),
        'success',
      );
    } catch (issue) {
      pushToast(describeError(issue), 'error');
    } finally {
      busy = null;
    }
  }
</script>

<div class="plugins">
  <section class="group">
    <h3 class="section-title">Плагины</h3>
    <p class="section-hint">
      Как расширения в VS Code: выключенный плагин не регистрирует свои инструменты, и ассистент
      сразу перестаёт их понимать. Переключение применяется без перезапуска.
    </p>
  </section>

  {#if loading}
    <p class="plugins__state">Загружаю плагины…</p>
  {:else if error}
    <div class="plugins__state plugins__state--error">
      <p>{error}</p>
      <button class="btn" type="button" onclick={() => void load()}>Повторить</button>
    </div>
  {:else}
    <ul class="plugins__list">
      {#each plugins as plugin (plugin.name)}
        <li class="plugin" class:plugin--off={!plugin.enabled}>
          <div class="plugin__puzzle" aria-hidden="true">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">
              <path d="M10 3h4a1 1 0 0 1 1 1v2a2 2 0 1 0 0 4v2a1 1 0 0 1-1 1h-2a1 1 0 0 0-1 1v2a1 1 0 0 1-1 1H8a2 2 0 1 1 0-4 2 2 0 0 0 0-4H5a1 1 0 0 1-1-1V8a1 1 0 0 1 1-1h2a2 2 0 0 0 2-2z" />
            </svg>
          </div>
          <div class="plugin__body">
            <span class="plugin__title">{plugin.title}</span>
            <span class="plugin__description">{plugin.description || plugin.name}</span>
            {#if plugin.tools.length > 0}
              <span class="plugin__tools">{plugin.tools.join(' · ')}</span>
            {/if}
          </div>
          <button
            class="switch"
            class:switch--on={plugin.enabled}
            type="button"
            role="switch"
            aria-checked={plugin.enabled}
            aria-label={(plugin.enabled ? 'Выключить ' : 'Включить ') + plugin.title}
            disabled={busy === plugin.name}
            onclick={() => void toggle(plugin)}
          >
            <span class="switch__knob"></span>
          </button>
        </li>
      {/each}
    </ul>
  {/if}
</div>

<style>
  .plugins {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }

  .group {
    display: flex;
    flex-direction: column;
    gap: 5px;
  }

  .section-title {
    margin: 0;
    font-size: 13px;
    font-weight: 650;
    color: var(--text);
  }

  .section-hint {
    margin: 0;
    font-size: 11.5px;
    line-height: 1.45;
    color: var(--text-muted);
  }

  .plugins__state {
    margin: 0;
    font-size: 12px;
    color: var(--text-muted);
  }

  .plugins__state--error {
    display: flex;
    flex-direction: column;
    gap: 8px;
    color: var(--state-error);
  }

  .plugins__list {
    display: flex;
    flex-direction: column;
    gap: 8px;
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .plugin {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 11px 12px;
    border: 1px solid var(--border);
    border-radius: 10px;
    background: var(--bg-elev);
    transition: opacity 120ms ease;
  }

  .plugin--off {
    opacity: 0.62;
  }

  .plugin__puzzle {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 32px;
    height: 32px;
    flex: 0 0 32px;
    border-radius: 8px;
    color: var(--accent);
    background: var(--accent-soft);
  }

  .plugin__body {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
  }

  .plugin__title {
    font-size: 12.5px;
    font-weight: 650;
    color: var(--text);
  }

  .plugin__description {
    font-size: 11.5px;
    color: var(--text-muted);
  }

  .plugin__tools {
    font-size: 10.5px;
    color: var(--text-dim);
    overflow-wrap: anywhere;
  }

  .switch {
    margin-left: auto;
    width: 36px;
    height: 20px;
    flex: 0 0 36px;
    padding: 2px;
    border: 1px solid var(--border-strong);
    border-radius: 999px;
    background: var(--bg);
    cursor: pointer;
    transition: background 120ms ease, border-color 120ms ease;
  }

  .switch__knob {
    display: block;
    width: 14px;
    height: 14px;
    border-radius: 50%;
    background: var(--text-muted);
    transition: transform 120ms ease, background 120ms ease;
  }

  .switch--on {
    background: var(--accent);
    border-color: var(--accent);
  }

  .switch--on .switch__knob {
    transform: translateX(16px);
    background: #fff;
  }

  .switch:disabled {
    cursor: progress;
    opacity: 0.7;
  }

  .switch:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 2px;
  }
</style>
