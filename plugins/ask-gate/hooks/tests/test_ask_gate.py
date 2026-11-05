"""Тесты хука ask-gate: образцы транскриптов и запуск сценария как в Claude Code."""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "ask-gate.py"
_spec = importlib.util.spec_from_file_location("ask_gate", SCRIPT)
assert _spec and _spec.loader
ask_gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ask_gate)


def user(content):
    return {"type": "user", "message": {"role": "user", "content": content}}


def tool_result():
    return user([{"type": "tool_result", "tool_use_id": "t", "content": "готово?"}])


def text(value):
    return {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": value}]},
    }


def tool(name):
    block = {"type": "tool_use", "id": "t", "name": name, "input": {}}
    return {"type": "assistant", "message": {"content": [block]}}


def run_hook(
    records,
    *,
    env_role=None,
    raw_input=None,
    active=False,
    tail="",
    last_message=None,
    entrypoint=None,
):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "t.jsonl"
        lines = [json.dumps(r, ensure_ascii=False) for r in records]
        path.write_text("\n".join(lines) + "\n" + tail, encoding="utf-8")
        hook_input = {
            "transcript_path": str(path),
            "stop_hook_active": active,
            "session_id": "s",
        }
        if last_message is not None:
            hook_input["last_assistant_message"] = last_message
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("DUTY_ROLE", "CLAUDE_CODE_ENTRYPOINT")
        }
        if entrypoint:
            env["CLAUDE_CODE_ENTRYPOINT"] = entrypoint
        if env_role:
            env["DUTY_ROLE"] = env_role
        return subprocess.run(
            [sys.executable, "-I", str(SCRIPT)],
            input=raw_input if raw_input is not None else json.dumps(hook_input),
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            check=False,
        )


def blocked(records, **kwargs):
    done = run_hook(records, **kwargs)
    assert done.returncode == 0, done.stderr
    if not done.stdout:
        return False
    answer = json.loads(done.stdout)
    assert answer["decision"] == "block" and "AskUserQuestion" in answer["reason"]
    return True


class HookRun(unittest.TestCase):
    def test_prose_question_blocks(self):
        self.assertTrue(blocked([user("как дела"), text("Сделано.\n\nСливать ветвь?")]))

    def test_prose_after_successful_ask_user_question_blocks(self):
        records = [
            user("x"),
            tool("AskUserQuestion"),
            tool_result(),
            text("Какой вариант?"),
        ]
        self.assertTrue(blocked(records))

    def test_prose_after_failed_ask_user_question_blocks(self):
        failed = tool_result()
        failed["message"]["content"][0]["is_error"] = True
        records = [user("x"), tool("AskUserQuestion"), failed, text("Какой вариант?")]
        self.assertTrue(blocked(records))

    def test_statement_after_ask_user_question_passes(self):
        records = [user("x"), tool("AskUserQuestion"), tool_result(), text("Принято.")]
        self.assertFalse(blocked(records))

    def test_non_interactive_entrypoints_pass(self):
        records = [user("x"), text("Сливать ветвь?")]
        for entrypoint in ("sdk-cli", "sdk-py", "sdk-ts"):
            self.assertFalse(blocked(records, entrypoint=entrypoint), entrypoint)
        for entrypoint in ("cli", "claude-vscode"):
            self.assertTrue(blocked(records, entrypoint=entrypoint), entrypoint)

    def test_ask_user_question_of_previous_turn_does_not_count(self):
        records = [
            user("x"),
            tool("AskUserQuestion"),
            tool_result(),
            text("Ок."),
            user("y"),
            text("Сливать?"),
        ]
        self.assertTrue(blocked(records))

    def test_stop_hook_active_passes(self):
        self.assertFalse(blocked([user("x"), text("Сливать?")], active=True))

    def test_worker_role_passes(self):
        self.assertFalse(blocked([user("x"), text("Сливать?")], env_role="worker"))

    def test_other_role_blocks(self):
        self.assertTrue(blocked([user("x"), text("Сливать?")], env_role="main"))

    def test_bad_stdin_passes(self):
        for raw in ("", "не json", "[]", '{"stop_hook_active": false}'):
            done = run_hook([], raw_input=raw)
            self.assertEqual((done.returncode, done.stdout), (0, ""), raw)

    def test_missing_transcript_passes(self):
        done = run_hook([], raw_input='{"transcript_path": "/нет/такого.jsonl"}')
        self.assertEqual((done.returncode, done.stdout), (0, ""))

    def test_broken_lines_are_skipped(self):
        records = [user("x"), text("Сливать?")]
        self.assertTrue(blocked(records, tail='{"type":"assistant", обрыв'))


class LastMessageField(unittest.TestCase):
    """Последнее сообщение приходит полем входа: в транскрипт оно ещё не записано."""

    def test_question_only_in_field_blocks(self):
        self.assertTrue(blocked([user("x")], last_message="Сливать ветвь?"))

    def test_field_overrides_stale_text(self):
        self.assertFalse(
            blocked([user("x"), text("Проверить?")], last_message="Проверено.")
        )

    def test_empty_field_passes(self):
        self.assertFalse(blocked([user("x"), text("Проверить?")], last_message=""))

    def test_ask_user_question_in_transcript_does_not_exempt_field(self):
        records = [user("x"), tool("AskUserQuestion"), tool_result()]
        self.assertTrue(blocked(records, last_message="Какой вариант?"))


class TurnBoundary(unittest.TestCase):
    def tail(self, records):
        return ask_gate.turn_tail(records)

    def test_tool_result_is_not_user_message(self):
        records = [user("x"), tool("Bash"), tool_result(), text("итог")]
        self.assertEqual(self.tail(records), "итог")

    def test_user_text_blocks_start_turn(self):
        records = [
            user("x"),
            tool("Bash"),
            user([{"type": "text", "text": "y"}]),
            text("итог"),
        ]
        self.assertEqual(self.tail(records), "итог")

    def test_skill_body_does_not_start_turn(self):
        skill = user("Base directory for this skill: ...") | {"isMeta": True}
        records = [
            user("x"),
            tool("AskUserQuestion"),
            tool_result(),
            skill,
            text("итог"),
        ]
        self.assertEqual(self.tail(records), "итог")

    def test_peer_message_starts_turn(self):
        peer = user("Another Claude session sent a message") | {
            "isMeta": True,
            "origin": {"kind": "peer"},
        }
        records = [
            user("x"),
            tool("AskUserQuestion"),
            tool_result(),
            peer,
            text("итог"),
        ]
        self.assertEqual(self.tail(records), "итог")

    def test_compact_summary_does_not_start_turn(self):
        summary = user("Сводка беседы") | {
            "isCompactSummary": True,
            "turnPosition": {"turnIndex": 2},
        }
        records = [
            user("x"),
            tool("AskUserQuestion"),
            tool_result(),
            summary,
            text("итог"),
        ]
        self.assertEqual(self.tail(records), "итог")

    def test_sidechain_is_ignored(self):
        side = text("Вопрос субагента?") | {"isSidechain": True}
        self.assertEqual(self.tail([user("x"), text("итог"), side]), "итог")

    def test_text_before_tool_call_is_not_turn_end(self):
        self.assertEqual(
            self.tail([user("x"), text("Проверить?"), tool("Bash"), tool_result()]),
            None,
        )

    def test_only_last_text_counts(self):
        records = [
            user("x"),
            text("Проверить?"),
            tool("Bash"),
            tool_result(),
            text("Проверено."),
        ]
        self.assertFalse(blocked(records))


class Paragraphs(unittest.TestCase):
    def assert_question(self, value):
        self.assertIsNotNone(ask_gate.question_paragraph(value), value)

    def assert_no_question(self, value):
        self.assertIsNone(ask_gate.question_paragraph(value), value)

    def test_last_sentence_question(self):
        self.assert_question("Итог готов. Сливать?\n\nЖду.")
        self.assert_question("Сделано. **Какой вариант?**")
        self.assert_question("Сливать ветвь `ask-gate`?")
        self.assert_question("Сливать сейчас?!")

    def test_question_in_the_middle_of_paragraph_does_not_count(self):
        self.assert_no_question("Почему упало? Причина в таймауте.\n\nПоправлено.")

    def test_question_followed_by_text_in_last_paragraph_counts(self):
        for value in (
            "Сливать? Рекомендую да.",
            "Какой вариант? Варианты: первый, второй.",
            "Готово.\n\nСливать ветвь? Рекомендую слить.",
            "Выбор:\n- первый\n- Удалять? Лучше нет.",
        ):
            self.assert_question(value)

    def test_url_with_query_does_not_count(self):
        self.assert_no_question("Страница https://x.y/a?b=1 открыта.")

    def test_exempt_last_paragraph_with_followup_text(self):
        self.assert_no_question("Итог.\n\nВопрос не к вам: сливать? Решает владелец.")

    def test_headings_do_not_count(self):
        for value in (
            "## Почему упало?",
            "Итог.\n\n## Что дальше?\n\nЖдём результата.",
            "# Сливать?",
            "Итог готов.\n\n### Причина? Таймаут",
        ):
            self.assert_no_question(value)

    def test_phrases(self):
        for value in (
            "Вот предложение. Что думаете.",
            "Подтвердите слияние.",
            "Не знаю, как поступить с веткой.",
            "Можно отвечать «оставить как есть».",
            "Сливать сейчас или после ревью? Иначе подожду.",
        ):
            self.assert_question(value)

    def test_or_without_question_mark_does_not_count(self):
        self.assert_no_question(
            "Правило действует для главной сессии или координатора."
        )

    def test_question_before_list_counts(self):
        self.assert_question("Какой вариант выбрать?\n- первый\n- второй")

    def test_list_item_question_counts(self):
        self.assert_question("Осталось:\n- слить ветвь\n- удалить ветвь?\n- снимок")

    def test_quote_does_not_count(self):
        self.assert_no_question(
            "Пользователь спросил:\n\n> Почему упало?\n\nПричина найдена."
        )

    def test_quote_splits_paragraph(self):
        self.assert_no_question("> Почему упало?\nПричина найдена.")

    def test_fenced_code_does_not_count(self):
        self.assert_no_question(
            "Команда:\n\n```sh\ntest -f a || echo нет?\nчто думаете\n```\n\nГотово."
        )
        self.assert_no_question("Команда:\n```\nудалить?\n")

    def test_inline_code_does_not_count(self):
        self.assert_no_question("Регулярка `a?`")
        self.assert_no_question("Флаг `что думаете` снят.")

    def test_table_does_not_count(self):
        self.assert_no_question(
            "| задача | действие |\n|---|---|\n| a | оставить как есть |\n| b | снять? |"
        )

    def test_exempt_marks(self):
        self.assert_no_question("Риторически: кто это проверит?")
        self.assert_no_question("Вопрос не к вам: субагент спрашивает, сливать ли?")
        self.assert_no_question("**Вопрос не к вам.** Сливать?")
        self.assert_no_question(
            "Вопрос не к вам, субагент спрашивает:\n- сливать?\n- удалять?"
        )

    def test_exempt_mark_on_list_item(self):
        self.assert_no_question(
            "Итог:\n- сделано\n- Вопрос не к вам: субагент спрашивает, сливать?"
        )
        self.assert_question("Итог:\n- Вопрос не к вам: сливать?\n- удалять?")

    def test_exempt_mark_covers_only_its_paragraph(self):
        self.assert_question("Вопрос не к вам: сливать?\n\nА вам: удалять?")

    def test_mark_in_the_middle_does_not_exempt(self):
        self.assert_question("Субагент спросил. Вопрос не к вам, но сливать?")

    def test_plain_report_passes(self):
        self.assert_no_question(
            "Сделано: хук и тесты.\n\nОсталось слить ветвь.\n\nЖду отчёт."
        )


if __name__ == "__main__":
    unittest.main()
