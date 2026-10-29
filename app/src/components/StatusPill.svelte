<script lang="ts">
  import { formatPercent, stateMeta } from '../lib/format';
  import type { ConnectionStatus, DownloadState, VoxState } from '../lib/types';

  interface Props {
    state: VoxState;
    paused: boolean;
    download: DownloadState | null;
    connection: ConnectionStatus;
    wsConnected: boolean;
  }

  let { state, paused, download, connection, wsConnected }: Props = $props();

  const meta = $derived(stateMeta(state, paused));
  const downloading = $derived(state === 'downloading');
  const percent = $derived(download ? download.percent : 0);
  const label = $derived(paused && state !== 'downloading' ? 'Пауза' : meta.label);

  const connectionText = $derived(
    connection === 'online'
      ? wsConnected
        ? 'на связи'
        : 'связь без событий'
      : connection === 'connecting'
        ? 'подключаюсь…'
        : connection === 'offline'
          ? 'нет связи'
          : 'запуск…',
  );

</script>

<div class="status" class:status--down={connection === 'offline'}>
  <div class="status__main">
    <span class="status__dot" style:background={meta.color} aria-hidden="true"></span>
    <span class="status__label" style:color={meta.color}>{label}</span>
    {#if downloading}
      <span class="status__percent">{formatPercent(percent)}</span>
    {/if}
    <span class="status__spacer"></span>
    <span
      class="status__conn"
      class:status__conn--ok={connection === 'online' && wsConnected}
      class:status__conn--wait={connection === 'online' && !wsConnected}
      class:status__conn--bad={connection === 'offline'}
      title={wsConnected ? 'Поток событий подключён' : 'Поток событий переподключается'}
    >
      <span class="status__conn-dot" aria-hidden="true"></span>
      {connectionText}
    </span>
  </div>

  {#if downloading}
    <div
      class="progress"
      role="progressbar"
      aria-label="Загрузка модели"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={Math.round(percent)}
      title={download?.message || 'Скачиваю модель'}
    >
      <div class="progress__bar" style:width={formatPercent(percent)}></div>
    </div>
  {/if}
</div>

<style>
  .status {
    display: flex;
    flex-direction: column;
    gap: 5px;
    margin-top: 7px;
  }

  .status__main {
    display: flex;
    align-items: center;
    gap: 7px;
    min-width: 0;
    font-size: 12.5px;
  }

  .status__dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    flex: 0 0 auto;
    box-shadow: 0 0 0 3px color-mix(in srgb, currentColor 8%, transparent);
  }

  .status__label {
    font-weight: 600;
    white-space: nowrap;
  }

  .status__percent {
    color: var(--text-muted);
    font-variant-numeric: tabular-nums;
  }

  .status__spacer {
    flex: 1 1 auto;
  }

  .status__conn {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    color: var(--text-dim);
    font-size: 11.5px;
    white-space: nowrap;
  }

  .status__conn-dot {
    width: 5px;
    height: 5px;
    border-radius: 50%;
    background: var(--text-dim);
  }

  .status__conn--ok .status__conn-dot {
    background: var(--ok);
  }

  .status__conn--wait .status__conn-dot {
    background: var(--warn);
  }

  .status__conn--bad {
    color: var(--err);
  }

  .status__conn--bad .status__conn-dot {
    background: var(--err);
  }

  .progress {
    height: 4px;
    border-radius: 999px;
    background: rgba(255, 255, 255, 0.07);
    overflow: hidden;
  }

  .progress__bar {
    height: 100%;
    border-radius: 999px;
    background: linear-gradient(90deg, #5d7dff, #7ea3ff);
    transition: width 0.25s ease;
  }
</style>
