<script lang="ts">
  import { rememberBridge } from '../lib/tauri';

  let port = $state('8000');
  let token = $state('');
  let error = $state<string | null>(null);

  function connect(event: SubmitEvent): void {
    event.preventDefault();
    const parsed = Number.parseInt(port.trim(), 10);
    if (!Number.isInteger(parsed) || parsed <= 0 || parsed > 65535) {
      error = 'Порт — целое число от 1 до 65535.';
      return;
    }
    const value = token.trim();
    if (!value) {
      error = 'Нужен токен сайдкара.';
      return;
    }
    error = null;
    rememberBridge({ port: parsed, token: value });
    window.location.reload();
  }
</script>

<div class="gate">
  <form class="gate__card" onsubmit={connect}>
    <h1 class="gate__title">Подключение к сайдкару</h1>
    <p class="gate__text">
      Окно открыто вне Tauri: Rust не передал порт и токен. Введите их, чтобы работать с запущенным сайдкаром.
    </p>
    <label class="field">
      <span class="field__label">Порт</span>
      <input class="field__input" bind:value={port} inputmode="numeric" placeholder="8000" />
    </label>
    <label class="field">
      <span class="field__label">Токен</span>
      <input class="field__input" bind:value={token} placeholder="VOX_TOKEN" autocomplete="off" spellcheck="false" />
    </label>
    {#if error}
      <p class="field__error">{error}</p>
    {/if}
    <button class="btn btn--primary" type="submit">Подключиться</button>
    <p class="field__hint">То же можно передать в адресе страницы: ?port=8000&amp;token=…</p>
  </form>
</div>

<style>
  .gate {
    position: fixed;
    inset: 0;
    z-index: 70;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 16px;
    background: var(--bg);
  }

  .gate__card {
    width: 100%;
    max-width: 340px;
    display: flex;
    flex-direction: column;
    gap: 10px;
    padding: 16px;
    border-radius: 12px;
    border: 1px solid var(--border);
    background: var(--bg-elev);
    box-shadow: var(--shadow);
  }

  .gate__title {
    margin: 0;
    font-size: 16px;
  }

  .gate__text {
    margin: 0 0 2px;
    font-size: 12px;
    line-height: 1.45;
    color: var(--text-muted);
  }
</style>
