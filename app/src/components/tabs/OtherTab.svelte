<script lang="ts">
  import type { Config } from '../../lib/types';

  interface Props {
    config: Config;
  }

  let { config }: Props = $props();
</script>

<div class="other">
  <section class="group">
    <h3 class="section-title">Фразы ошибок</h3>
    <p class="section-hint">Ассистент произносит их голосом, когда что-то не получилось.</p>

    <label class="field">
      <span class="field__label">Не расслышал (STT)</span>
      <input class="field__input" bind:value={config.errors.stt_failed_phrase} placeholder="Не расслышал, повтори" />
    </label>
    <label class="field">
      <span class="field__label">Не понял команду</span>
      <input class="field__input" bind:value={config.errors.not_understood_phrase} placeholder="Не понял, что сделать" />
    </label>
    <label class="field">
      <span class="field__label">Отказ инструмента</span>
      <input class="field__input" bind:value={config.errors.tool_refused_phrase} placeholder="не могу это сделать" />
    </label>
    <label class="field">
      <span class="field__label">Нет связи</span>
      <input class="field__input" bind:value={config.errors.no_connection_phrase} placeholder="нет связи" />
    </label>
    <label class="field">
      <span class="field__label">Микрофон недоступен</span>
      <input class="field__input" bind:value={config.errors.mic_phrase} placeholder="микрофон недоступен" />
    </label>
  </section>

  <section class="group">
    <h3 class="section-title">Погода</h3>
    <label class="field">
      <span class="field__label">Город по умолчанию</span>
      <input class="field__input" bind:value={config.weather.default_city} placeholder="Сургут" />
      <span class="field__hint">Используется, если город не назван в команде.</span>
    </label>
  </section>

  <section class="group">
    <h3 class="section-title">Имя ассистента и wake word</h3>
    <p class="section-hint">Устройства ввода и вывода — на вкладке «Звук».</p>
    <label class="field">
      <span class="field__label">Имя ассистента</span>
      <input class="field__input" bind:value={config.wakeword.phrase} placeholder="вокс" />
      <span class="field__hint">По этому слову ассистент просыпается, оно же подставляется в системный промпт.</span>
    </label>
    <label class="field">
      <span class="field__label">Порог wake word (0…1)</span>
      <input class="field__input" type="number" min="0" max="1" step="0.01" bind:value={config.wakeword.threshold} />
      <span class="field__hint">Выше — меньше ложных срабатываний, но можно не проснуться.</span>
    </label>
    <label class="field">
      <span class="field__label">Ожидание команды после wake, с</span>
      <input class="field__input" type="number" min="1" max="60" step="1" bind:value={config.wakeword.listen_timeout_sec} />
    </label>
  </section>

  <section class="group">
    <h3 class="section-title">Таймауты</h3>
    <div class="grid-2">
      <label class="field">
        <span class="field__label">Тишина VAD, мс</span>
        <input class="field__input" type="number" min="100" max="5000" step="50" bind:value={config.vad.silence_ms} />
      </label>
      <label class="field">
        <span class="field__label">Максимум фразы, с</span>
        <input class="field__input" type="number" min="1" max="60" step="1" bind:value={config.vad.max_utterance_sec} />
      </label>
    </div>
    <label class="field">
      <span class="field__label">Таймаут LLM, с</span>
      <input class="field__input" type="number" min="5" max="180" step="1" bind:value={config.llm.timeout_sec} />
    </label>
  </section>
</div>

<style>
  .other {
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
</style>
