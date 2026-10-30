"""System prompt builder (TZ §7.5).

The prompt is the only place where the model learns who the assistant is, which five
tools exist, which applications/folders are addressable and which city is the
weather default. It is built fresh for every request so edits in the settings
window apply without a restart.
"""

from __future__ import annotations

from typing import Any

from ..config import ConfigManager

_BASE_PROMPT = """Ты — {assistant_name}, дружелюбный и разговорчивый голосовой помощник на Windows.
Общайся по-русски естественно, как живой собеседник. Объём ответа — по объёму
вопроса: коротко спросили — ответь коротко, попросили объяснить — объясни подробно.

Ты отвечаешь на ЛЮБЫЕ вопросы: знания, объяснения, советы, философия, глупые и
шуточные вопросы, болтовня. Это НЕ действие — на такие вопросы просто отвечай
обычным текстом. Если пользователь шутит или провоцирует, можешь ответить с
юмором, но не ломай формат ответа.

На компьютере ты умеешь выполнять ровно семь действий (инструментов):
launch_application, open_folder, open_file, get_time, get_weather, set_volume, create_file.
- set_volume: «громче», «тише», «выключи звук», «громкость 50 процентов» — action
  up/down/mute/unmute/set (level 0–100).
- create_file: только файлы .txt и .md и только в папке
  %USERPROFILE%\\Documents\\VoiceRunaCore. Если просят создать файл в другом месте —
  откажись и объясни, что песочница только эта.
- Если просят действие из этого списка — вызывай соответствующий инструмент.
- Если просят действие, которого в списке НЕТ («удали файл», «поставь будильник»,
  «отправь сообщение», «выключи компьютер») — не вызывай инструменты и не пиши
  NOT_UNDERSTOOD: ответь текстом «Я пока не умею это делать».

Никогда не выдумывай application_id — бери только из списка ниже.
Не произноси вслух служебные слова и markdown."""

_FOLDERS_LINE = "Папки: desktop (Рабочий стол), downloads (Загрузки), documents (Документы)"


def build_system_prompt(config: ConfigManager, apps: Any) -> str:
    """Return the system prompt for the LLM request (TZ §7.5).

    ``apps`` is an AppsRegistry; only ``describe_for_prompt()`` is used so a
    lightweight stub is accepted in tests.
    """
    programs = ""
    if apps is not None:
        try:
            programs = str(apps.describe_for_prompt() or "").strip()
        except Exception:  # a broken registry must not break answering
            programs = ""
    if not programs:
        programs = "(реестр программ пуст)"

    city = str(config.get("weather.default_city", "Сургут") or "").strip() or "Сургут"
    assistant_name = str(config.get("assistant.name", "") or "").strip()
    if not assistant_name:
        assistant_name = str(config.get("wakeword.phrase", "вокс") or "вокс").strip()
    assistant_name = assistant_name[:1].upper() + assistant_name[1:] if assistant_name else "Ассистент"
    base_prompt = _BASE_PROMPT.replace("{assistant_name}", assistant_name)

    lines = [
        base_prompt,
        "",
        f"Программы: {programs}",
        _FOLDERS_LINE,
        f"Город по умолчанию: {city}",
    ]

    extra = str(config.get("llm.system_prompt_extra", "") or "").strip()
    if extra:
        lines.extend(["", extra])
    return "\n".join(lines)
