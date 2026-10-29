<script lang="ts">
  import { dismissToast, ui } from '../lib/store.svelte';
</script>

<div class="toasts" aria-live="polite" aria-atomic="false">
  {#each ui.toasts as toast (toast.id)}
    <div class="toast toast--{toast.kind}" role="status">
      <span class="toast__text">{toast.text}</span>
      <button class="toast__close" type="button" aria-label="Закрыть уведомление" onclick={() => dismissToast(toast.id)}>
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" aria-hidden="true">
          <path d="M6 6l12 12M18 6 6 18" />
        </svg>
      </button>
    </div>
  {/each}
</div>

<style>
  .toasts {
    position: fixed;
    left: 12px;
    right: 12px;
    bottom: 92px;
    display: flex;
    flex-direction: column;
    gap: 6px;
    z-index: 60;
    pointer-events: none;
  }

  .toast {
    pointer-events: auto;
    display: flex;
    align-items: flex-start;
    gap: 8px;
    padding: 8px 10px;
    border-radius: 9px;
    border: 1px solid var(--border-strong);
    background: #232838;
    color: var(--text);
    font-size: 12px;
    line-height: 1.4;
    box-shadow: 0 8px 22px rgba(0, 0, 0, 0.45);
    animation: toast-in 0.16s ease-out;
  }

  .toast--success {
    border-color: rgba(78, 201, 164, 0.4);
    background: #1d2a28;
  }

  .toast--error {
    border-color: rgba(240, 106, 106, 0.45);
    background: #2b1f22;
  }

  .toast__text {
    flex: 1 1 auto;
    overflow-wrap: anywhere;
  }

  .toast__close {
    flex: 0 0 auto;
    border: none;
    background: transparent;
    color: var(--text-muted);
    cursor: pointer;
    padding: 1px;
    display: inline-flex;
    border-radius: 4px;
  }

  .toast__close:hover {
    color: var(--text);
  }

  @keyframes toast-in {
    from {
      opacity: 0;
      transform: translateY(4px);
    }
    to {
      opacity: 1;
      transform: translateY(0);
    }
  }
</style>
