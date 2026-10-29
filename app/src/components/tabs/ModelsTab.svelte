<script lang="ts">
  import { onMount } from 'svelte';
  import { describeError, requireApi } from '../../lib/api';
  import { clearApiKeyIssue, pushToast } from '../../lib/store.svelte';
  import type { Diagnostics, Provider } from '../../lib/types';
  import Icon from '../Icon.svelte';

  interface Props {
    providers: Provider[];
  }

  interface TestResult {
    ok: boolean;
    message: string;
  }

  let { providers }: Props = $props();

  let expanded = $state<Record<string, boolean>>({});
  let tests = $state<Record<string, TestResult | 'loading'>>({});
  let modelList = $state<Record<string, string[]>>({});
  let modelLoading = $state<Record<string, boolean>>({});
  let keys = $state<Record<string, string>>({});
  let keyVisible = $state<Record<string, boolean>>({});
  let keySaving = $state<Record<string, boolean>>({});
  let keySaved = $state<Record<string, boolean>>({});
  let diagnostics = $state<Diagnostics | null>(null);

  onMount(() => {
    void loadDiagnostics();
  });

  /** /diagnostics is optional: an older sidecar must not break the editor. */
  async function loadDiagnostics(): Promise<void> {
    try {
      diagnostics = await requireApi().getDiagnostics();
    } catch {
      diagnostics = null;
    }
  }

  /** Whether .env already has the key for this provider (from /diagnostics). */
  function keyState(provider: Provider): 'present' | 'missing' | null {
    const list = diagnostics?.providers;
    if (!Array.isArray(list)) return null;
    const id = (provider.id ?? '').trim();
    if (!id) return null;
    const info = list.find((item) => item && item.id === id);
    if (!info || typeof info.key_present !== 'boolean') return null;
    return info.key_present ? 'present' : 'missing';
  }

  const duplicates = $derived.by(() => {
    const seen = new Set<string>();
    const repeated = new Set<string>();
    for (const provider of providers) {
      const id = (provider.id ?? '').trim();
      if (!id) continue;
      if (seen.has(id)) repeated.add(id);
      seen.add(id);
    }
    return repeated;
  });

  function slot(provider: Provider, index: number): string {
    const id = (provider.id ?? '').trim();
    return (id || 'new') + '#' + String(index);
  }

  function isOpen(provider: Provider, index: number): boolean {
    return expanded[slot(provider, index)] ?? true;
  }

  function toggle(provider: Provider, index: number): void {
    const key = slot(provider, index);
    expanded[key] = !(expanded[key] ?? true);
  }

  function addProvider(): void {
    providers.push({
      id: '',
      label: 'Новый провайдер',
      enabled: true,
      base_url: 'https://',
      api_key_env: '',
      model: '',
      supports_tools: true,
      priority: providers.length + 1,
    });
  }

  function removeProvider(index: number): void {
    providers.splice(index, 1);
  }

  function envNameLabel(provider: Provider): string {
    const name = (provider.api_key_env ?? '').trim();
    return name || 'ключ не задан';
  }

  function testText(key: string): string {
    const value = tests[key];
    if (!value || value === 'loading') return '';
    return (value.ok ? '✓ ' : '✕ ') + value.message;
  }

  function testOk(key: string): boolean {
    const value = tests[key];
    return Boolean(value && value !== 'loading' && value.ok);
  }

  async function test(provider: Provider, key: string): Promise<void> {
    const id = (provider.id ?? '').trim();
    if (!id) {
      pushToast('Сначала задайте id провайдера', 'error');
      return;
    }
    tests[key] = 'loading';
    try {
      const result = await requireApi().providerTest(id);
      const message = (result.message ?? '').trim();
      tests[key] = {
        ok: Boolean(result.ok),
        message: message || (result.ok ? 'провайдер отвечает' : 'провайдер не ответил'),
      };
      pushToast(
        result.ok ? 'Провайдер ' + (provider.label.trim() || id) + ' отвечает' : message || 'Провайдер не ответил',
        result.ok ? 'success' : 'error',
      );
    } catch (error) {
      tests[key] = { ok: false, message: describeError(error) };
    }
  }

  async function loadModels(provider: Provider, key: string): Promise<void> {
    const id = (provider.id ?? '').trim();
    if (!id) {
      pushToast('Сначала задайте id провайдера', 'error');
      return;
    }
    modelLoading[key] = true;
    try {
      const response = await requireApi().providerModels(id);
      const list = Array.isArray(response.models) ? response.models : [];
      modelList[key] = list;
      if (list.length === 0) pushToast('Провайдер не вернул моделей', 'info');
      else pushToast('Загружено моделей: ' + String(list.length), 'success');
    } catch (error) {
      pushToast(describeError(error), 'error');
    } finally {
      modelLoading[key] = false;
    }
  }

  async function saveKey(provider: Provider, key: string): Promise<void> {
    const envName = (provider.api_key_env ?? '').trim();
    if (!envName) {
      pushToast('Сначала задайте api_key_env', 'error');
      return;
    }
    const value = (keys[key] ?? '').trim();
    if (!value) {
      pushToast('Введите ключ', 'error');
      return;
    }
    keySaving[key] = true;
    keySaved[key] = false;
    try {
      await requireApi().setEnv(envName, value);
      keys[key] = '';
      keySaved[key] = true;
      // The key is in .env now: refresh badges and drop the "no key" banner.
      clearApiKeyIssue();
      void loadDiagnostics();
      pushToast('Ключ сохранён в .env: ' + envName, 'success');
      window.setTimeout(() => {
        keySaved[key] = false;
      }, 6000);
    } catch (error) {
      pushToast(describeError(error), 'error');
    } finally {
      keySaving[key] = false;
    }
  }
</script>

<div class="models">
  <div class="section-head">
    <div>
      <h3 class="section-title">Провайдеры LLM</h3>
      <p class="section-hint">
        Каскад идёт по возрастанию приоритета. Ключи лежат в .env, в config.json их нет.
      </p>
    </div>
    <button class="btn" type="button" onclick={addProvider}>
      <Icon name="plus" size={14} />
      Добавить
    </button>
  </div>

  {#if providers.length === 0}
    <p class="empty-note">Провайдеров нет — добавьте хотя бы одного, иначе Ассистент не сможет отвечать.</p>
  {/if}

  {#each providers as provider, index (index)}
    {@const key = slot(provider, index)}
    <section class="provider" class:provider--off={!provider.enabled}>
      <div class="provider__head">
        <button
          class="provider__toggle"
          type="button"
          aria-expanded={isOpen(provider, index)}
          onclick={() => toggle(provider, index)}
        >
          <span class="provider__chevron" class:provider__chevron--open={isOpen(provider, index)}>
            <Icon name="chevron" size={14} />
          </span>
          <span class="provider__name">{(provider.label ?? '').trim() || (provider.id ?? '').trim() || 'Без названия'}</span>
          <span class="provider__id">{(provider.id ?? '').trim() || 'id не задан'}</span>
          {#if keyState(provider) === 'missing'}
            <span class="key-badge key-badge--missing" title={'В .env нет значения ' + envNameLabel(provider)}>
              ключ не найден
            </span>
          {:else if keyState(provider) === 'present'}
            <span class="key-badge key-badge--ok" title="Ключ найден в .env">ключ есть</span>
          {/if}
        </button>
        <label class="checkbox" title="Провайдер участвует в каскаде">
          <input type="checkbox" bind:checked={provider.enabled} />
          <span>вкл</span>
        </label>
      </div>

      {#if isOpen(provider, index)}
        <div class="provider__body">
          <div class="grid-2">
            <label class="field">
              <span class="field__label">id</span>
              <input class="field__input" bind:value={provider.id} placeholder="openrouter" spellcheck="false" />
            </label>
            <label class="field">
              <span class="field__label">Название</span>
              <input class="field__input" bind:value={provider.label} placeholder="OpenRouter (free)" />
            </label>
          </div>
          {#if duplicates.has((provider.id ?? '').trim())}
            <p class="field__error">id повторяется у нескольких провайдеров.</p>
          {/if}

          <label class="field">
            <span class="field__label">base_url</span>
            <input class="field__input" bind:value={provider.base_url} placeholder="https://openrouter.ai/api/v1" spellcheck="false" />
          </label>

          <div class="grid-2">
            <label class="field">
              <span class="field__label">api_key_env</span>
              <input class="field__input" bind:value={provider.api_key_env} placeholder="OPENROUTER_API_KEY" spellcheck="false" />
            </label>
            <label class="field">
              <span class="field__label">Приоритет</span>
              <input class="field__input" type="number" min="0" step="1" bind:value={provider.priority} />
            </label>
          </div>

          <label class="field">
            <span class="field__label">Модель</span>
            <input
              class="field__input"
              bind:value={provider.model}
              placeholder="mistralai/mistral-small-3.1-24b-instruct:free"
              spellcheck="false"
            />
          </label>

          <label class="checkbox">
            <input type="checkbox" bind:checked={provider.supports_tools} />
            <span>Поддерживает инструменты (tools)</span>
          </label>

          <div class="provider__key">
            <span class="field__label">Ключ для {envNameLabel(provider)}</span>
            <div class="provider__key-row">
              <input
                class="field__input"
                type={keyVisible[key] ? 'text' : 'password'}
                bind:value={keys[key]}
                aria-label={'Ключ API для ' + envNameLabel(provider)}
                placeholder="вставьте ключ и сохраните"
                autocomplete="off"
                spellcheck="false"
                disabled={!(provider.api_key_env ?? '').trim()}
              />
              <button
                class="btn btn--icon"
                type="button"
                title={keyVisible[key] ? 'Скрыть ключ' : 'Показать ключ'}
                aria-label={keyVisible[key] ? 'Скрыть ключ' : 'Показать ключ'}
                onclick={() => {
                  keyVisible[key] = !keyVisible[key];
                }}
              >
                {keyVisible[key] ? '••' : '•••'}
              </button>
              <button
                class="btn"
                type="button"
                disabled={keySaving[key] || !(provider.api_key_env ?? '').trim()}
                onclick={() => void saveKey(provider, key)}
              >
                {keySaving[key] ? 'Сохраняю…' : 'Сохранить'}
              </button>
            </div>
            <span class="field__hint">Ключ хранится в %APPDATA%\VoiceRunaCore\.env, в config.json он не попадает.</span>
            {#if keySaved[key]}
              <span class="field__hint field__hint--ok">Ключ сохранён в .env</span>
            {/if}
          </div>

          <div class="provider__actions">
            <button
              class="btn btn--sm"
              type="button"
              disabled={tests[key] === 'loading'}
              onclick={() => void test(provider, key)}
            >
              <Icon name="check" size={13} />
              {tests[key] === 'loading' ? 'Проверяю…' : 'Проверить'}
            </button>
            <button
              class="btn btn--sm"
              type="button"
              disabled={modelLoading[key]}
              onclick={() => void loadModels(provider, key)}
            >
              <Icon name="refresh" size={13} />
              {modelLoading[key] ? 'Загружаю…' : 'Загрузить модели'}
            </button>
            <button class="btn btn--sm btn--danger provider__remove" type="button" onclick={() => removeProvider(index)}>
              <Icon name="trash" size={13} />
              Удалить
            </button>
          </div>

          {#if testText(key)}
            <p class="test-result" class:test-result--ok={testOk(key)} class:test-result--fail={!testOk(key)}>
              {testText(key)}
            </p>
          {/if}

          {#if modelList[key] && modelList[key].length > 0}
            <label class="field">
              <span class="field__label">Модели провайдера ({modelList[key].length})</span>
              <select
                class="field__input"
                value={modelList[key].includes(provider.model) ? provider.model : ''}
                onchange={(event) => {
                  provider.model = event.currentTarget.value;
                }}
              >
                <option value="">— выбрать модель —</option>
                {#each modelList[key] as model (model)}
                  <option value={model}>{model}</option>
                {/each}
              </select>
            </label>
          {:else if modelList[key]}
            <p class="field__hint">Провайдер не вернул моделей.</p>
          {/if}
        </div>
      {/if}
    </section>
  {/each}
</div>

<style>
  .models {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  .provider {
    border: 1px solid var(--border);
    border-radius: var(--radius);
    background: var(--bg-elev);
    overflow: hidden;
  }

  .provider--off {
    opacity: 0.72;
  }

  .provider__head {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 4px 8px 4px 4px;
    background: #1a1e29;
  }

  .provider__toggle {
    flex: 1 1 auto;
    min-width: 0;
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 5px 4px;
    border: none;
    background: transparent;
    color: var(--text);
    cursor: pointer;
    text-align: left;
  }

  .provider__chevron {
    display: inline-flex;
    color: var(--text-dim);
    transform: rotate(-90deg);
    transition: transform 0.15s ease;
  }

  .provider__chevron--open {
    transform: rotate(0deg);
  }

  .provider__name {
    font-size: 12.5px;
    font-weight: 600;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .provider__id {
    margin-left: auto;
    font-size: 10.5px;
    color: var(--text-dim);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .key-badge {
    flex: 0 0 auto;
    font-size: 9.5px;
    line-height: 15px;
    padding: 0 5px;
    border-radius: 999px;
    border: 1px solid transparent;
    white-space: nowrap;
  }

  .key-badge--missing {
    color: var(--warn);
    border-color: rgba(233, 179, 79, 0.4);
    background: rgba(233, 179, 79, 0.1);
  }

  .key-badge--ok {
    color: var(--ok);
    border-color: rgba(78, 201, 164, 0.35);
    background: rgba(78, 201, 164, 0.08);
  }

  .provider__body {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 10px;
    border-top: 1px solid var(--border);
  }

  .provider__key {
    display: flex;
    flex-direction: column;
    gap: 4px;
    padding: 8px;
    border-radius: var(--radius-sm);
    background: rgba(255, 255, 255, 0.025);
    border: 1px dashed var(--border);
  }

  .provider__key-row {
    display: flex;
    align-items: center;
    gap: 6px;
    min-width: 0;
  }

  .provider__key-row .field__input {
    flex: 1 1 auto;
  }

  .provider__actions {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 6px;
  }

  .provider__remove {
    margin-left: auto;
  }

  .test-result {
    margin: 0;
    font-size: 11.5px;
    line-height: 1.4;
    padding: 5px 7px;
    border-radius: 6px;
    overflow-wrap: anywhere;
  }

  .test-result--ok {
    color: var(--ok);
    background: rgba(78, 201, 164, 0.08);
  }

  .test-result--fail {
    color: var(--err);
    background: rgba(240, 106, 106, 0.08);
  }
</style>
