"""Хук Stop: вопрос пользователю в прозе без AskUserQuestion возвращается агенту.

Вход — JSON хука на stdin. Вывод — решение block либо ничего; код всегда 0:
сбой хука не должен мешать завершению хода. Порядок — README плагина.
"""

import json
import os
import re
import sys

REASON = (
    "Вопрос пользователю задаётся через AskUserQuestion: два-четыре "
    "взаимоисключающих варианта с кратким следствием, рекомендуемый — первым "
    "с пометкой. Проза со знаком вопроса в конце хода не принимается. Варианты "
    "неизвестны — спросить, какие варианты возможны, тоже карточкой. Пересказ "
    "чужого вопроса — абзацем с началом «Вопрос не к вам»."
)

# Строка записи о сообщении; остальные записи (тела запросов к API и служебные)
# занимают основную часть файла и не разбираются.
RECORD_TYPE = re.compile(rb'"type"\s*:\s*"(?:user|assistant)"')
FENCE = re.compile(r"\s*(```|~~~)")
INLINE_CODE = re.compile(r"`[^`\n]*`")
LIST_ITEM = re.compile(r"\s*(?:[-*+]|\d+[.)])\s")
# Разметка перед началом абзаца: маркер списка, выделение.
LEADING_MARKUP = re.compile(r"(?:\s|[*_]|(?:[-+]|\d+[.)])\s)*")
EXEMPT = ("риторически:", "вопрос не к вам")
QUESTION_END = re.compile(r"\?[!?]*[\s*_)\"»”’'\]]*$")
PHRASES = re.compile(
    r"что думаете|подтвердите|как поступить|оставить как есть"
    r"|\bили\b[^.!?]*\?",
    re.IGNORECASE,
)


def is_user_message(record: dict) -> bool:
    """Начинает ли запись новый ход: tool_result и тела навыков не начинают."""
    if record.get("type") != "user" or record.get("isSidechain"):
        return False
    # Сообщение другой сессии — isMeta с origin; тело навыка — isMeta без него.
    if record.get("isMeta") and not (
        record.get("origin") or record.get("turnPosition")
    ):
        return False
    content = (record.get("message") or {}).get("content")
    if isinstance(content, str):
        return True
    return isinstance(content, list) and any(
        isinstance(block, dict) and block.get("type") == "text" for block in content
    )


def turn_tail(records) -> tuple[str | None, set[str]]:
    """Текст конца хода и имена инструментов хода.

    Текст, после которого агент вызвал инструмент, концом хода не считается.
    """
    text: str | None = None
    tools: set[str] = set()
    for record in records:
        if is_user_message(record):
            text, tools = None, set()
        elif record.get("type") == "assistant" and not record.get("isSidechain"):
            content = (record.get("message") or {}).get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and (block.get("text") or "").strip():
                    text = block["text"]
                elif block.get("type") == "tool_use":
                    tools.add(str(block.get("name")))
                    text = None
    return text, tools


def read_records(path: str):
    """Записи о сообщениях; повреждённые строки пропускаются."""
    with open(path, "rb") as stream:
        for line in stream:
            if not RECORD_TYPE.search(line):
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                yield record


def without_quotes_and_code(text: str) -> str:
    """Цитаты, таблицы и блоки кода заменяются пустыми строками, код в строке — словом."""
    lines = []
    fence = None
    for line in text.splitlines():
        match = FENCE.match(line)
        if fence:
            if match and match.group(1) == fence:
                fence = None
            lines.append("")
        elif match:
            fence = match.group(1)
            lines.append("")
        elif line.lstrip().startswith((">", "|")):
            lines.append("")
        else:
            lines.append(INLINE_CODE.sub("код", line))
    return "\n".join(lines)


def question_paragraph(text: str) -> str | None:
    """Первый абзац с вопросом пользователю или None.

    Пункт списка — свой абзац, пометка исключения действует на весь блок до
    пустой строки: пересказ вопросов списком помечается один раз.
    """
    for block in re.split(r"\n\s*\n", without_quotes_and_code(text)):
        start = LEADING_MARKUP.sub("", block, count=1).lower()
        if start.startswith(EXEMPT):
            continue
        items: list[list[str]] = []
        for line in block.splitlines():
            if not line.strip():
                continue
            if LIST_ITEM.match(line) or not items:
                items.append([line.strip()])
            else:
                items[-1].append(line.strip())
        for item in items:
            paragraph = " ".join(item)
            if QUESTION_END.search(paragraph) or PHRASES.search(paragraph):
                return paragraph
    return None


def decide(hook_input: dict) -> str | None:
    """Причина отказа или None, если ход можно завершить."""
    if hook_input.get("stop_hook_active") or os.environ.get("DUTY_ROLE") == "worker":
        return None
    text, tools = turn_tail(read_records(hook_input["transcript_path"]))
    # На момент Stop последнее сообщение агента в транскрипт ещё не записано.
    last_message = hook_input.get("last_assistant_message")
    if isinstance(last_message, str):
        text = last_message
    if not text or "AskUserQuestion" in tools or not question_paragraph(text):
        return None
    return REASON


def main() -> int:
    try:
        reason = decide(json.load(sys.stdin))
    except Exception:  # noqa: BLE001 — сбой хука не должен мешать завершению хода
        return 0
    if reason:
        print(json.dumps({"decision": "block", "reason": reason}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
