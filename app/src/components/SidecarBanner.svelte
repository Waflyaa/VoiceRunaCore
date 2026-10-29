<script lang="ts">
  import { restartSidecar, ui } from '../lib/store.svelte';

  interface Props {
    message: string;
  }

  let { message }: Props = $props();
</script>

<div class="sidecar-error" role="alert">
  <div class="sidecar-error__text">
    <strong>Сайдкар не отвечает</strong>
    <span>{message}</span>
  </div>
  <button class="btn btn--sm" type="button" disabled={ui.restarting} onclick={() => void restartSidecar()}>
    {ui.restarting ? 'Перезапускаю…' : 'Перезапустить сайдкар'}
  </button>
</div>

<style>
  .sidecar-error {
    flex: 0 0 auto;
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 7px 12px;
    border-bottom: 1px solid rgba(240, 106, 106, 0.3);
    background: rgba(240, 106, 106, 0.1);
  }

  .sidecar-error__text {
    flex: 1 1 auto;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 1px;
    font-size: 11.5px;
    line-height: 1.35;
    color: #f4b4b4;
    overflow-wrap: anywhere;
  }

  .sidecar-error__text strong {
    color: #f7c6c6;
    font-size: 12px;
  }
</style>
