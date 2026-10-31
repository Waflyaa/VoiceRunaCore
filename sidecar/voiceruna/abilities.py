"""Abilities.md generator: a human-readable feature list in the data folder.

The files are rewritten on every sidecar start so that they always match the
running version. The UI card opens Abilities_RU.md; the English twin exists for
issue reports and for anyone who prefers English.
"""

from __future__ import annotations

from pathlib import Path

from . import __version__

__all__ = ["RU_TEMPLATE", "EN_TEMPLATE", "abilities_paths", "ensure_files"]

RU_TEMPLATE = "# Возможности VoiceRunaCore\n\n_Версия {version}. Файл обновляется автоматически при запуске ассистента._\n\n## Как меня разбудить\n\n- Скажи имя ассистента (по умолчанию «вокс») и сразу команду: «вокс, открой блокнот».\n- Или нажми **Ctrl+Alt+V** — окно откроется и ассистент начнёт слушать. Повторное нажатие сворачивает окно в трей.\n- После ответа есть **5 секунд**, чтобы продолжить диалог без имени — просто говори дальше.\n\n## Программы и папки\n\n- Запуск программ из белого списка: «открой блокнот», «запусти калькулятор», «открой доту».\n- Открытие папок: «открой папку Загрузки», «открой Документы», «открой Рабочий стол».\n- Открытие файлов из известных папок: «открой файл отчёт.pdf в документах».\n- Список программ настраивается в «Настройки → Программы» (файл apps.json).\n\n## Громкость\n\n- «громче», «тише» — на 10%.\n- «выключи звук», «включи звук».\n- «громкость 50%» — точный уровень.\n\n## Время и погода\n\n- «который час», «сколько времени».\n- «какая погода», «погода в Москве». Город по умолчанию задаётся в настройках.\n\n## Файлы в песочнице\n\n- «создай файл заметка.txt», «создай файл идеи.md с текстом купить хлеб».\n- Файлы создаются **только** в папке `%USERPROFILE%\\Documents\\VoiceRunaCore` и только в форматах .txt и .md. За пределами песочницы ассистент откажет.\n\n## Диалог и история\n\n- Ассистент отвечает голосом и показывает историю в окне.\n- «повтори» — повторит последнюю фразу.\n- Кнопка микрофона — начать слушать без имени; Esc — свернуть окно.\n- Пауза микрофона и выход — в меню иконки в трее.\n- Любой вопрос (не команда) уходит в LLM: знания, объяснения, болтовня.\n\n## Настройки\n\n- **Звук** — устройства ввода/вывода, проверка, калибровка микрофона, диагностика.\n- **Голос** — движок синтеза (Piper/edge/SAPI), голос, скорость, папка своих голосов.\n- **Модели** — провайдеры LLM, ключи API, выбор модели.\n- **Программы** — белый список приложений.\n- **Прочее** — имя ассистента и слово-будильник, хоткей, follow-up окно, фразы ошибок, город, логи.\n\n## Перезапуск и журналы\n\n- «Перезапустить сайдкар» — на экране ошибки.\n- Логи: `%APPDATA%\\VoiceRunaCore\\logs\\sidecar.log`.\n- Полный список команд — в этом файле; он всегда соответствует текущей версии.\n\n_Работает локально: микрофон, распознавание, синтез и инструменты — на этом компьютере. В сеть уходят только запросы к LLM и погоде._\n"
EN_TEMPLATE = "# VoiceRunaCore abilities\n\n_Version {version}. This file is refreshed automatically when the assistant starts._\n\n## Waking me up\n\n- Say the assistant name (default \"вокс\") followed by a command: \"вокс, open notepad\".\n- Or press **Ctrl+Alt+V**: the window opens and listening starts. Press again to hide to tray.\n- After an answer you have **5 seconds** to continue the dialogue without the name.\n\n## Apps and folders\n\n- Launch whitelisted apps: \"open notepad\", \"launch calculator\", \"open dota\".\n- Open known folders: \"open Downloads\", \"open Documents\", \"open Desktop\".\n- Open files inside known folders: \"open file report.pdf in documents\".\n- The app list is edited in Settings → Programs (apps.json).\n\n## Volume\n\n- \"louder\", \"quieter\" — by 10%.\n- \"mute\", \"unmute\".\n- \"volume 50%\" — exact level.\n\n## Time and weather\n\n- \"what time is it\".\n- \"what's the weather\", \"weather in Moscow\". The default city is set in Settings.\n\n## Files in the sandbox\n\n- \"create file notes.txt\", \"create file ideas.md with text buy bread\".\n- Files are created **only** in `%USERPROFILE%\\Documents\\VoiceRunaCore` and only as .txt or .md. Anything outside the sandbox is refused.\n\n## Dialogue and history\n\n- The assistant speaks the answer and shows the dialogue history in the window.\n- \"repeat\" re-runs the last phrase.\n- The microphone button starts listening without the name; Esc hides the window.\n- Microphone pause and exit live in the tray menu.\n- Any question (not a command) goes to the LLM: knowledge, explanations, small talk.\n\n## Settings\n\n- **Audio** — input/output devices, tests, microphone calibration, diagnostics.\n- **Voice** — TTS engine (Piper/edge/SAPI), voice, rate, custom voices folder.\n- **Models** — LLM providers, API keys, model choice.\n- **Programs** — application whitelist.\n- **Other** — assistant name and wake word, hotkey, follow-up window, error phrases, city, logs.\n\n## Restart and logs\n\n- \"Restart sidecar\" on the error screen.\n- Logs: `%APPDATA%\\VoiceRunaCore\\logs\\sidecar.log`.\n- This file always matches the current version.\n\n_Runs locally: microphone, speech recognition, synthesis and tools stay on this computer. Only LLM and weather requests go to the network._\n"


def abilities_paths(base: Path) -> tuple[Path, Path]:
    """Return (Russian, English) abilities file paths under *base*."""
    return base / "Abilities_RU.md", base / "Abilities_ENG.md"


def ensure_files(base: Path | None = None) -> tuple[Path, Path]:
    """Write both abilities files (UTF-8) and return their paths."""
    from . import paths

    folder = base if base is not None else paths.data_dir()
    folder.mkdir(parents=True, exist_ok=True)
    ru_path, en_path = abilities_paths(folder)
    ru_path.write_text(RU_TEMPLATE.format(version=__version__), encoding="utf-8")
    en_path.write_text(EN_TEMPLATE.format(version=__version__), encoding="utf-8")
    return ru_path, en_path
