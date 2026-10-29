<script lang="ts">
  import { openSettings } from '../lib/store.svelte';
  import type { ApiKeyIssue } from '../lib/types';

  interface Props {
    issue: ApiKeyIssue;
  }

  let { issue }: Props = $props();

  const title = $derived(issue.kind === 'auth' ? 'Ключ не принят провайдером' : 'API ключ не найден');
  const detail = $derived(extractDetail(issue.message));

  /** runtime.py sends "<phrase> (нет ключа: OPENROUTER_API_KEY (openrouter), ...)". */
  function extractDetail(message: string): string {
    const text = (message ?? '').trim();
    const names = text.match(/[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+/g);
    if (names && names.length > 0) {
      return Array.from(new Set(names)).join(', ');
    }
    return text;
  }
</script>

<div class="api-key" class:api-key--auth={issue.kind === 'auth'} role="alert">
  <div class="api-key__text">
    <strong>{title}</strong>
    {#if detail}
      <span class="api-key__detail">{detail}</span>
    {/if}
  </div>
  <button class="btn btn--sm" type="button" onclick={() => openSettings('models')}>
    Открыть настройки
  </button>
</div>

<style>
  .api-key {
    flex: 0 0 auto;
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 7px 12px;
    border-bottom: 1px solid rgba(233, 179, 79, 0.35);
    background: rgba(233, 179, 79, 0.1);
  }

  .api-key--auth {
    border-bottom-color: rgba(240, 106, 106, 0.35);
    background: rgba(240, 106, 106, 0.1);
  }

  .api-key__text {
    flex: 1 1 auto;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 1px;
    font-size: 11.5px;
    line-height: 1.35;
    color: #e8c98f;
    overflow-wrap: anywhere;
  }

  .api-key--auth .api-key__text {
    color: #f4b4b4;
  }

  .api-key__text strong {
    font-size: 12px;
    color: #f0d6a8;
  }

  .api-key--auth .api-key__text strong {
    color: #f7c6c6;
  }

  .api-key__detail {
    color: inherit;
    opacity: 0.85;
  }
</style>
