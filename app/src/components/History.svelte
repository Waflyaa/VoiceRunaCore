<script lang="ts">
  import { formatTime, toolLine } from '../lib/format';
  import { openAbilities } from '../lib/tauri';
  import type { AppsMap, ChatMessage } from '../lib/types';

  interface Props {
    messages: ChatMessage[];
    apps: AppsMap;
    onEdit: (text: string) => void;
  }

  let { messages, apps, onEdit }: Props = $props();
  let listEl: HTMLDivElement | undefined = $state();

  // Autoscroll whenever the history grows (TZ §10.1).
  $effect(() => {
    const count = messages.length;
    if (!listEl) return;
    void count;
    listEl.scrollTop = listEl.scrollHeight;
  });

  const lastUserIndex = $derived.by(() => {
    for (let index = messages.length - 1; index >= 0; index -= 1) {
      if (messages[index].role === 'user') return index;
    }
    return -1;
  });

  function toolText(message: ChatMessage): string {
    if (message.tool) {
      return toolLine(message.tool.name, message.tool.args, message.tool.ok, apps);
    }
    return message.text;
  }
</script>

<div class="history" bind:this={listEl} aria-live="polite" aria-label="История диалога">
  {#if messages.length === 0}
    <div class="empty">
      <div class="empty__mark">
        <svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" aria-hidden="true">
          <path d="M4 10v4" />
          <path d="M8 6v12" />
          <path d="M12 3v18" />
          <path d="M16 7v10" />
          <path d="M20 10v4" />
        </svg>
      </div>
      <p class="empty__title">Пока пусто</p>
      <p class="empty__text">Скажите команду или напишите её ниже.</p>
    </div>
  {:else}
    {#each messages as message, index (message.id)}
      {#if message.role === 'user'}
        <article class="msg msg--user">
          <div class="msg__meta">
            <span class="msg__author">Вы</span>
            {#if message.source}
              <span class="chip" class:chip--voice={message.source === 'voice'}>
                {message.source === 'voice' ? 'голос' : 'окно'}
              </span>
            {/if}
            <time class="msg__time" datetime={new Date(message.at).toISOString()}>{formatTime(message.at)}</time>
            {#if index === lastUserIndex}
              <button
                class="msg__edit"
                type="button"
                title="Исправить фразу и отправить заново"
                onclick={() => onEdit(message.text)}
              >
                править
              </button>
            {/if}
          </div>
          <p class="msg__text">{message.text}</p>
        </article>
      {:else if message.role === 'assistant'}
        <article class="msg msg--assistant">
          <div class="msg__meta">
            <span class="msg__author msg__author--vox">Ассистент</span>
            <time class="msg__time" datetime={new Date(message.at).toISOString()}>{formatTime(message.at)}</time>
          </div>
          {#if message.action === 'open_abilities'}
            <button class="abilities" type="button" onclick={() => void openAbilities()}>
              <span class="abilities__icon" aria-hidden="true">
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M6 3h9l4 4v14H6z" />
                  <path d="M15 3v4h4" />
                  <path d="M9 12h7M9 16h7" />
                </svg>
              </span>
              <span class="abilities__body">
                <span class="abilities__title">📄 Список моих возможностей</span>
                <span class="abilities__hint">Нажми, чтобы открыть файл Abilities.md</span>
              </span>
              <span class="abilities__arrow" aria-hidden="true">→</span>
            </button>
            <p class="abilities__text">{message.text}</p>
          {:else}
            <p class="msg__text">{message.text}</p>
          {/if}
        </article>
      {:else if message.role === 'tool'}
        <div class="tool" class:tool--fail={message.tool ? !message.tool.ok : message.toolOk === false}>
          <span class="tool__line">{toolText(message)}</span>
          <time class="tool__time" datetime={new Date(message.at).toISOString()}>{formatTime(message.at)}</time>
        </div>
      {:else if message.role === 'error'}
        <div class="error" role="alert">
          <span class="error__icon">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true">
              <path d="M12 8v5" />
              <path d="M12 17h.01" />
              <circle cx="12" cy="12" r="9" />
            </svg>
          </span>
          <span class="error__text">{message.text}</span>
        </div>
      {/if}
    {/each}
  {/if}
</div>

<style>
  .history {
    flex: 1 1 auto;
    min-height: 0;
    overflow-y: auto;
    overflow-x: hidden;
    padding: 12px 12px 6px;
    display: flex;
    flex-direction: column;
    gap: 10px;
    scroll-behavior: smooth;
  }

  .empty {
    margin: auto;
    text-align: center;
    padding: 16px;
    color: var(--text-dim);
    max-width: 260px;
  }

  .empty__mark {
    display: inline-flex;
    color: var(--accent);
    opacity: 0.85;
    margin-bottom: 6px;
  }

  .empty__title {
    margin: 0 0 4px;
    color: var(--text-muted);
    font-weight: 600;
    font-size: 13px;
  }

  .empty__text {
    margin: 0;
    font-size: 12px;
    line-height: 1.45;
  }

  .msg {
    max-width: 92%;
    border-radius: 10px;
    padding: 7px 9px 8px;
    border: 1px solid var(--border);
    background: var(--bg-elev);
    overflow-wrap: anywhere;
  }

  .msg--user {
    align-self: flex-end;
    background: var(--accent-soft);
    border-color: rgba(111, 140, 255, 0.32);
  }

  .msg--assistant {
    align-self: flex-start;
    background: var(--bg-elev);
  }

  .msg__meta {
    display: flex;
    align-items: baseline;
    gap: 6px;
    font-size: 11px;
    color: var(--text-dim);
    margin-bottom: 3px;
  }

  .msg__author {
    font-weight: 600;
    color: var(--text-muted);
    font-size: 11.5px;
  }

  .msg__author--vox {
    color: var(--state-speaking);
  }

  .msg__time {
    font-variant-numeric: tabular-nums;
    margin-left: auto;
  }

  .msg__edit {
    border: none;
    background: transparent;
    color: var(--accent);
    font-size: 11px;
    padding: 0 2px;
    cursor: pointer;
    border-radius: 4px;
  }

  .msg__edit:hover {
    text-decoration: underline;
  }

  .msg__text {
    margin: 0;
    font-size: 13px;
    line-height: 1.45;
    color: var(--text);
    white-space: pre-wrap;
  }

  /* Interactive "what can you do" card: primary-button look, whole row clickable. */
  .abilities {
    display: flex;
    align-items: center;
    gap: 10px;
    width: 100%;
    margin-top: 2px;
    padding: 11px 12px;
    border: 1px solid transparent;
    border-radius: 10px;
    background: var(--accent);
    color: #fff;
    font: inherit;
    text-align: left;
    cursor: pointer;
    transition: filter 120ms ease, transform 120ms ease;
  }

  .abilities:hover {
    filter: brightness(1.07);
  }

  .abilities:active {
    transform: translateY(1px);
  }

  .abilities:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 2px;
  }

  .abilities__icon {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 34px;
    height: 34px;
    flex: 0 0 34px;
    border-radius: 9px;
    background: rgba(255, 255, 255, 0.16);
  }

  .abilities__body {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
  }

  .abilities__title {
    font-size: 13px;
    font-weight: 650;
    letter-spacing: 0.01em;
  }

  .abilities__hint {
    font-size: 11.5px;
    color: rgba(255, 255, 255, 0.82);
  }

  .abilities__arrow {
    margin-left: auto;
    font-size: 16px;
    opacity: 0.85;
  }

  .abilities__text {
    margin: 6px 0 0;
    font-size: 12px;
    color: var(--text-muted);
  }

  .chip {
    border: 1px solid var(--border-strong);
    border-radius: 999px;
    padding: 0 6px;
    font-size: 10px;
    color: var(--text-muted);
    line-height: 15px;
  }

  .chip--voice {
    border-color: rgba(76, 194, 255, 0.35);
    color: var(--state-listening);
  }

  .tool {
    display: flex;
    align-items: center;
    gap: 8px;
    align-self: flex-start;
    max-width: 100%;
    padding: 3px 8px;
    border-radius: 999px;
    background: rgba(78, 201, 164, 0.09);
    border: 1px solid rgba(78, 201, 164, 0.22);
    color: var(--ok);
    font-size: 12px;
  }

  .tool--fail {
    background: rgba(240, 106, 106, 0.09);
    border-color: rgba(240, 106, 106, 0.25);
    color: var(--err);
  }

  .tool__line {
    overflow-wrap: anywhere;
  }

  .tool__time {
    color: var(--text-dim);
    font-size: 10.5px;
    font-variant-numeric: tabular-nums;
  }

  .error {
    display: flex;
    align-items: flex-start;
    gap: 7px;
    padding: 6px 8px;
    border-radius: 8px;
    border: 1px solid rgba(240, 106, 106, 0.25);
    background: rgba(240, 106, 106, 0.08);
    color: #f4a3a3;
    font-size: 12px;
    line-height: 1.4;
  }

  .error__icon {
    display: inline-flex;
    margin-top: 1px;
    color: var(--err);
  }

  .error__text {
    overflow-wrap: anywhere;
  }
</style>
