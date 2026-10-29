<script lang="ts">
  import { onMount } from 'svelte';
  import { listenNow, repeatLast, sendText, stopSpeaking, ui } from '../lib/store.svelte';
  import Icon from './Icon.svelte';

  let inputEl: HTMLInputElement | undefined = $state();
  let lastFocusSeq = -1;

  // Autofocus on open and after "править" (store bumps focusSeq).
  $effect(() => {
    const seq = ui.focusSeq;
    if (seq === lastFocusSeq) return;
    lastFocusSeq = seq;
    const target = inputEl;
    if (!target) return;
    target.focus();
    target.select();
  });

  onMount(() => {
    const onWindowFocus = () => {
      const active = document.activeElement;
      if (!active || active === document.body || active === inputEl) inputEl?.focus();
    };
    window.addEventListener('focus', onWindowFocus);
    return () => window.removeEventListener('focus', onWindowFocus);
  });

  function submit(): void {
    const text = ui.input.trim();
    if (!text || ui.busy) return;
    void sendText(text);
  }

  function onKeydown(event: KeyboardEvent): void {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      submit();
    }
  }
</script>

<div class="composer">
  <div class="composer__row">
    <input
      bind:this={inputEl}
      class="composer__input"
      bind:value={ui.input}
      onkeydown={onKeydown}
      placeholder="Введите команду и нажмите Enter"
      aria-label="Команда"
      autocomplete="off"
      spellcheck="false"
      aria-busy={ui.busy}
    />
    <button
      class="composer__send"
      type="button"
      onclick={submit}
      disabled={ui.busy || ui.input.trim().length === 0}
      title="Отправить (Enter)"
      aria-label="Отправить"
    >
      {#if ui.busy}
        <span class="spinner" aria-hidden="true"></span>
      {:else}
        <Icon name="send" size={17} />
      {/if}
    </button>
    <button
      class="composer__mic"
      type="button"
      onclick={() => void listenNow()}
      disabled={ui.busy}
      title="Слушать сейчас"
      aria-label="Слушать сейчас"
    >
      <Icon name="mic" size={17} />
    </button>
  </div>

  <div class="composer__row composer__row--actions">
    <button
      class="btn btn--ghost btn--sm"
      type="button"
      onclick={() => void repeatLast()}
      disabled={ui.busy}
      title="Перезаписать последнюю реплику"
    >
      <Icon name="repeat" size={14} />
      Повторить
    </button>
    {#if ui.state === 'speaking'}
      <button class="btn btn--ghost btn--sm" type="button" onclick={() => void stopSpeaking()} title="Остановить озвучку">
        <Icon name="stop" size={13} />
        Стоп
      </button>
    {/if}
    <span class="composer__hint">Enter — отправить</span>
  </div>
</div>

<style>
  .composer {
    display: flex;
    flex-direction: column;
    gap: 6px;
    padding: 9px 12px 11px;
    border-top: 1px solid var(--border);
    background: var(--bg-elev);
  }

  .composer__row {
    display: flex;
    align-items: center;
    gap: 6px;
    min-width: 0;
  }

  .composer__row--actions {
    gap: 4px;
  }

  .composer__input {
    flex: 1 1 auto;
    min-width: 0;
    height: 36px;
    padding: 0 10px;
    border-radius: var(--radius-sm);
    border: 1px solid var(--border-strong);
    background: var(--bg-input);
    color: var(--text);
    font-size: 13px;
  }

  .composer__input::placeholder {
    color: var(--text-dim);
  }

  .composer__input:focus-visible {
    outline: none;
    border-color: var(--accent);
    box-shadow: 0 0 0 2px var(--accent-soft);
  }

  .composer__send,
  .composer__mic {
    width: 36px;
    height: 36px;
    flex: 0 0 auto;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    border-radius: var(--radius-sm);
    border: 1px solid var(--border-strong);
    background: var(--bg-elev-2);
    color: var(--text);
    cursor: pointer;
    padding: 0;
  }

  .composer__send {
    background: var(--accent);
    border-color: var(--accent);
    color: #0d1020;
  }

  .composer__send:hover:not(:disabled) {
    background: #8aa2ff;
    border-color: #8aa2ff;
  }

  .composer__mic:hover:not(:disabled) {
    border-color: var(--accent);
    color: var(--accent);
  }

  .composer__send:disabled,
  .composer__mic:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .composer__hint {
    margin-left: auto;
    font-size: 10.5px;
    color: var(--text-dim);
    white-space: nowrap;
  }
</style>
