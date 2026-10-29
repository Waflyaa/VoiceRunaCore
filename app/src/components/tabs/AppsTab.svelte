<script lang="ts">
  import { formatAliases, normalizeAliases } from '../../lib/format';
  import { pushToast } from '../../lib/store.svelte';
  import type { AppEntry, AppsMap } from '../../lib/types';
  import { validateAppEntry } from '../../lib/validate';
  import Icon from '../Icon.svelte';

  interface Props {
    apps: AppsMap;
  }

  let { apps }: Props = $props();

  let confirmId = $state<string | null>(null);
  let newId = $state('');
  let newDisplay = $state('');
  let newTarget = $state('');
  let addError = $state<string | null>(null);

  const entries = $derived(Object.entries(apps) as [string, AppEntry][]);

  function looksLikeUri(value: string): boolean {
    const match = value.match(/^([A-Za-z][A-Za-z0-9+.-]*):/);
    if (!match) return false;
    // A single letter is a Windows drive (C:\...), not a URI scheme.
    return match[1].length > 1;
  }

  function targetOf(entry: AppEntry): string {
    return (entry.uri ?? '').trim() || (entry.path ?? '');
  }

  function setTarget(entry: AppEntry, raw: string): void {
    const value = raw.trim();
    if (looksLikeUri(value)) {
      entry.uri = value;
      delete entry.path;
    } else {
      entry.path = value;
      delete entry.uri;
    }
  }

  function issuesFor(id: string, entry: AppEntry): string[] {
    return validateAppEntry(id, entry);
  }

  function rename(oldId: string, raw: string): void {
    const nextId = raw.trim();
    if (nextId === oldId) return;
    if (!nextId) {
      pushToast('id не может быть пустым', 'error');
      return;
    }
    if (apps[nextId]) {
      pushToast('id «' + nextId + '» уже занят', 'error');
      return;
    }
    const entry = apps[oldId];
    if (!entry) return;
    apps[nextId] = entry;
    delete apps[oldId];
  }

  function remove(id: string): void {
    delete apps[id];
    confirmId = null;
  }

  function addApp(): void {
    const id = newId.trim().toLowerCase();
    const display = newDisplay.trim();
    const target = newTarget.trim();
    if (!id) {
      addError = 'Укажите id латиницей, например discord.';
      return;
    }
    if (apps[id]) {
      addError = 'Программа с таким id уже есть.';
      return;
    }
    if (!display) {
      addError = 'Укажите название программы.';
      return;
    }
    if (!target) {
      addError = 'Укажите путь к exe или uri (например, steam://rungameid/570).';
      return;
    }
    const entry: AppEntry = looksLikeUri(target)
      ? { display_name: display, uri: target }
      : { display_name: display, path: target };
    const problems = validateAppEntry(id, entry);
    if (problems.length > 0) {
      addError = problems[0];
      return;
    }
    apps[id] = entry;
    newId = '';
    newDisplay = '';
    newTarget = '';
    addError = null;
    pushToast('Программа добавлена', 'success');
  }
</script>

<div class="apps">
  <div class="section-head">
    <div>
      <h3 class="section-title">Программы и папки</h3>
      <p class="section-hint">
        Это apps.json: по алиасам Ассистент узнаёт программу. Изменения сохраняются кнопкой «Сохранить».
      </p>
    </div>
  </div>

  {#if entries.length === 0}
    <p class="empty-note">Список пуст — добавьте программы, которые Ассистент должен открывать.</p>
  {/if}

  {#each entries as [id, entry] (id)}
    {@const issues = issuesFor(id, entry)}
    <section class="app-card">
      <div class="app-card__head">
        <input
          class="field__input app-card__id"
          value={id}
          aria-label="id программы"
          spellcheck="false"
          onchange={(event) => rename(id, event.currentTarget.value)}
        />
        <input
          class="field__input app-card__name"
          bind:value={entry.display_name}
          aria-label="Название программы"
          placeholder="Название"
        />
      </div>

      <label class="field">
        <span class="field__label">path или uri</span>
        <input
          class="field__input"
          value={targetOf(entry)}
          placeholder="C:\Program Files\... или steam://rungameid/570"
          spellcheck="false"
          oninput={(event) => setTarget(entry, event.currentTarget.value)}
        />
      </label>

      <label class="field">
        <span class="field__label">Алиасы через запятую</span>
        <input
          class="field__input"
          value={formatAliases(entry.aliases)}
          placeholder="дота, доту, dota"
          onchange={(event) => {
            entry.aliases = normalizeAliases(event.currentTarget.value);
          }}
        />
      </label>

      {#if issues.length > 0}
        <ul class="app-card__issues">
          {#each issues as issue (issue)}
            <li>{issue}</li>
          {/each}
        </ul>
      {/if}

      <div class="app-card__actions">
        {#if confirmId === id}
          <span class="app-card__confirm">Удалить «{entry.display_name || id}»?</span>
          <button class="btn btn--sm btn--danger" type="button" onclick={() => remove(id)}>Да, удалить</button>
          <button class="btn btn--sm btn--ghost" type="button" onclick={() => (confirmId = null)}>Отмена</button>
        {:else}
          <button class="btn btn--sm btn--ghost" type="button" onclick={() => (confirmId = id)}>
            <Icon name="trash" size={13} />
            Удалить
          </button>
        {/if}
      </div>
    </section>
  {/each}

  <section class="app-card app-card--new">
    <h4 class="app-card__title">Добавить программу</h4>
    <div class="grid-2">
      <label class="field">
        <span class="field__label">id</span>
        <input class="field__input" bind:value={newId} placeholder="discord" spellcheck="false" />
      </label>
      <label class="field">
        <span class="field__label">Название</span>
        <input class="field__input" bind:value={newDisplay} placeholder="Discord" />
      </label>
    </div>
    <label class="field">
      <span class="field__label">path или uri</span>
      <input class="field__input" bind:value={newTarget} placeholder="%LOCALAPPDATA%\Discord\Update.exe" spellcheck="false" />
    </label>
    {#if addError}
      <p class="field__error">{addError}</p>
    {/if}
    <div class="app-card__actions">
      <button class="btn btn--sm" type="button" onclick={addApp}>
        <Icon name="plus" size={13} />
        Добавить
      </button>
    </div>
  </section>
</div>

<style>
  .apps {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  .app-card {
    display: flex;
    flex-direction: column;
    gap: 7px;
    padding: 9px;
    border: 1px solid var(--border);
    border-radius: var(--radius);
    background: var(--bg-elev);
  }

  .app-card--new {
    border-style: dashed;
    border-color: var(--border-strong);
    background: transparent;
  }

  .app-card__title {
    margin: 0;
    font-size: 12.5px;
    font-weight: 600;
  }

  .app-card__head {
    display: flex;
    gap: 6px;
    min-width: 0;
  }

  .app-card__id {
    flex: 0 0 38%;
    font-size: 11.5px;
    color: var(--text-muted);
  }

  .app-card__name {
    flex: 1 1 auto;
    min-width: 0;
  }

  .app-card__issues {
    margin: 0;
    padding-left: 16px;
    color: var(--err);
    font-size: 10.5px;
    line-height: 1.4;
  }

  .app-card__actions {
    display: flex;
    align-items: center;
    gap: 6px;
    flex-wrap: wrap;
  }

  .app-card__confirm {
    flex: 1 1 auto;
    min-width: 0;
    font-size: 11.5px;
    color: var(--warn);
  }
</style>
