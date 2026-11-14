"""Хук PermissionRequest: снимает запрос на удаление внутри ~/work.

Вход — JSON хука на stdin. Вывод — решение allow либо ничего; код всегда 0:
сбой хука не должен мешать показу обычного запроса. deny хук не возвращает.
Разбор консервативный: любое сомнение — без решения. Порядок — README плагина.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

# Режим, в котором хук работает: в остальных режимах запрос может быть вызван
# не удалением, и allow одобрил бы всю команду целиком.
ALLOWED_MODE = "bypassPermissions"
RM_COMMANDS = {
    "rm",
    "rmdir",
    "/bin/rm",
    "/bin/rmdir",
    "/usr/bin/rm",
    "/usr/bin/rmdir",
}
# Команды, меняющие каталог: после них относительные пути не вычисляются.
CD_COMMANDS = {"cd", "pushd", "popd", "chdir"}
# Команды, которые запускают другие команды или меняют состояние оболочки
# (HOME, псевдонимы, функции): их присутствие снимает решение.
BLOCKED_COMMANDS = {
    "sudo", "doas", "su", "env", "command", "builtin", "exec", "eval", "xargs",
    "find", "parallel", "bash", "sh", "zsh", "dash", "ksh", "fish", "python",
    "python3", "perl", "ruby", "node", "busybox", "nohup", "nice", "ionice",
    "time", "timeout", "stdbuf", "setsid", "watch", "chroot", "unshare",
    "nsenter", "flock", "script", "strace", "ssh", "source", ".", "alias",
    "unalias", "trap", "coproc", "function", "for", "case", "select",
    "export", "declare", "typeset", "readonly", "local", "unset", "set",
    "shopt", "enable", "hash", "command_not_found_handle", "rm()", "rmdir()",
}  # fmt: skip
# Зарезервированные слова, после которых снова ожидается команда.
KEYWORDS = {
    "if",
    "then",
    "else",
    "elif",
    "fi",
    "while",
    "until",
    "do",
    "done",
    "{",
    "}",
    "!",
}
SHORT_FLAGS = set("frRdvIi")
LONG_FLAGS = {
    "--force", "--recursive", "--verbose", "--dir", "--one-file-system",
    "--preserve-root", "--interactive",
}  # fmt: skip
GLOB_CHARS = "*?["
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\+?=")
RMISH = re.compile(r"(?<![\w.-])(?:rm|rmdir)(?![\w.-])")


class Bail(Exception):
    """Команду нельзя разобрать однозначно: решения нет."""


def lex(src: str) -> list[tuple[str, object]]:
    """Разбор на слова и операторы. Слово — список пар (символ, в кавычках)."""
    if "\x00" in src or "\r" in src:
        raise Bail
    tokens: list[tuple[str, object]] = []
    word: list[tuple[str, bool]] = []
    in_word = False
    i, n = 0, len(src)

    def flush() -> None:
        nonlocal word, in_word
        if in_word:
            tokens.append(("word", word))
        word, in_word = [], False

    while i < n:
        c = src[i]
        if c in " \t":
            flush()
            i += 1
        elif c == "\n":
            flush()
            tokens.append(("op", "\n"))
            i += 1
        elif c == "#" and not in_word:
            while i < n and src[i] != "\n":
                i += 1
        elif c == "\\":
            if i + 1 >= n or src[i + 1] == "\n":
                raise Bail
            word.append((src[i + 1], True))
            in_word = True
            i += 2
        elif c == "'":
            j = src.find("'", i + 1)
            if j < 0:
                raise Bail
            word.extend((ch, True) for ch in src[i + 1 : j])
            in_word = True
            i = j + 1
        elif c == '"':
            i += 1
            in_word = True
            while True:
                if i >= n:
                    raise Bail
                d = src[i]
                if d == '"':
                    i += 1
                    break
                if d in "$`":
                    raise Bail
                if d == "\\":
                    if i + 1 >= n:
                        raise Bail
                    e = src[i + 1]
                    if e == "\n":
                        raise Bail
                    if e in '$`"\\':
                        word.append((e, True))
                        i += 2
                        continue
                word.append((d, True))
                i += 1
        elif c in "$`":
            raise Bail
        elif c in "<>":
            two = src[i : i + 2]
            if two in ("<<", "<(", ">("):
                raise Bail
            if in_word and all(ch.isdigit() and not q for ch, q in word):
                word, in_word = [], False
            flush()
            j = i + 1
            follow = src[j : j + 1]
            if follow and follow in (">&|" if c == ">" else "&>"):
                j += 1
            tokens.append(("redir", src[i:j]))
            i = j
        elif c == "&" and src[i + 1 : i + 2] == ">":
            flush()
            j = i + 2
            if src[j : j + 1] == ">":
                j += 1
            tokens.append(("redir", src[i:j]))
            i = j
        elif c in ";&|()":
            flush()
            two = src[i : i + 2]
            if two == ";;" or two == ";&" or src[i : i + 3] == ";;&":
                raise Bail
            if two in ("&&", "||", "|&"):
                tokens.append(("op", two))
                i += 2
            else:
                tokens.append(("op", c))
                i += 1
        else:
            word.append((c, False))
            in_word = True
            i += 1
    flush()
    return tokens


def text_of(word: list[tuple[str, bool]]) -> str:
    return "".join(ch for ch, _ in word)


def check_word(word: list[tuple[str, bool]]) -> None:
    """Слова с раскрытием, которое нельзя вычислить статически."""
    for ch, q in word:
        if not q and ch in "{}":
            if len(word) == 1:
                continue
            raise Bail


def split_commands(
    tokens: list[tuple[str, object]],
) -> list[list[list[tuple[str, bool]]]]:
    """Простые команды: списки слов без слов-целей перенаправлений."""
    commands: list[list[list[tuple[str, bool]]]] = []
    current: list[list[tuple[str, bool]]] = []
    depth = 0
    after_close = False
    skip_redirect_target = False

    def end() -> None:
        nonlocal current, after_close
        if current:
            commands.append(current)
        current = []
        after_close = False

    for kind, value in tokens:
        if kind == "redir":
            if skip_redirect_target:
                raise Bail
            skip_redirect_target = True
            continue
        if kind == "word":
            word: list[tuple[str, bool]] = value  # type: ignore[assignment]
            check_word(word)
            if skip_redirect_target:
                skip_redirect_target = False
                continue
            if after_close:
                raise Bail
            text = text_of(word)
            if not current and not any(q for _, q in word) and text in KEYWORDS:
                continue
            current.append(word)
            continue
        if skip_redirect_target:
            raise Bail
        op = value
        if op == "(":
            if current:
                raise Bail
            depth += 1
        elif op == ")":
            if depth <= 0:
                raise Bail
            end()
            depth -= 1
            after_close = True
        else:
            end()
    if skip_redirect_target or depth != 0:
        raise Bail
    end()
    return commands


def inside_work(path: str, work: str, min_depth: int) -> bool:
    """Путь лежит в work на глубине не меньше min_depth компонентов."""
    if path != work and not path.startswith(work + os.sep):
        return False
    rel = path[len(work) :].strip(os.sep)
    parts = [p for p in rel.split(os.sep) if p]
    return len(parts) >= min_depth


def target_allowed(
    word: list[tuple[str, bool]], cwd: str, home: str, work: str, cd_seen: bool
) -> bool:
    text = text_of(word)
    if not text:
        return False
    # Тильда: только вначале слова без кавычек, только своя.
    if not word[0][1] and word[0][0] == "~":
        if text == "~":
            text, word = home, [(ch, True) for ch in home]
        elif text.startswith("~/"):
            tail = word[1:]
            word = [(ch, True) for ch in home] + tail
            text = home + text[1:]
        else:
            return False
    glob_at = next(
        (k for k, (ch, q) in enumerate(word) if not q and ch in GLOB_CHARS), None
    )
    if glob_at is None:
        const, depth = text, 2
    else:
        # Шаблон допустим только в последнем компоненте пути.
        if "/" in text[glob_at:]:
            return False
        const = text[:glob_at]
        const = const[: const.rfind("/") + 1] if "/" in const else ""
        if not const:
            const = "./"
        depth = 2
        # Каталог, внутри которого раскрывается шаблон.
    if not const.startswith("/"):
        if cd_seen:
            return False
        const = os.path.join(cwd, const)
    resolved = os.path.realpath(const)
    return inside_work(resolved, work, depth)


def rm_call_allowed(
    words: list[list[tuple[str, bool]]], cwd: str, home: str, work: str, cd_seen: bool
) -> bool:
    targets: list[list[tuple[str, bool]]] = []
    options_done = False
    for word in words[1:]:
        text = text_of(word)
        if not options_done and text == "--" and not any(q for _, q in word):
            options_done = True
            continue
        if not options_done and text.startswith("-") and len(text) > 1:
            if text.startswith("--"):
                if text not in LONG_FLAGS:
                    return False
            elif not set(text[1:]) <= SHORT_FLAGS:
                return False
            continue
        targets.append(word)
    if not targets:
        return False
    return all(target_allowed(w, cwd, home, work, cd_seen) for w in targets)


def decide(command: str, cwd: str, home: str) -> bool:
    """True, если все вызовы rm/rmdir в команде удовлетворяют условиям."""
    if not cwd.startswith("/") or not home.startswith("/"):
        return False
    work = os.path.realpath(os.path.join(home, "work"))
    try:
        commands = split_commands(lex(command))
    except Bail:
        return False
    cd_seen = False
    rm_calls = []
    for words in commands:
        first = text_of(words[0])
        if ASSIGNMENT.match(first):
            return False
        if first in CD_COMMANDS:
            cd_seen = True
        if first in RM_COMMANDS:
            rm_calls.append(words)
            continue
        if first in BLOCKED_COMMANDS:
            return False
        if any(RMISH.search(text_of(w)) for w in words):
            return False
    if not rm_calls:
        return False
    return all(rm_call_allowed(w, cwd, home, work, cd_seen) for w in rm_calls)


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "state"
    )
    return Path(base) / "rm-work-allow"


def write_log(command: str, cwd: str) -> bool:
    """Строка журнала: время, каталог и команда; True, если запись удалась."""
    try:
        directory = state_dir()
        directory.mkdir(parents=True, exist_ok=True)
        entry = {
            "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "cwd": cwd,
            "command": command,
        }
        with open(directory / "allow.log", "a", encoding="utf-8") as log:
            log.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return True
    except OSError:
        return False


def main() -> None:
    try:
        data = json.load(sys.stdin)
        if not isinstance(data, dict):
            return
        if data.get("hook_event_name") != "PermissionRequest":
            return
        if data.get("tool_name") != "Bash":
            return
        if data.get("permission_mode") != ALLOWED_MODE:
            return
        tool_input = data.get("tool_input")
        if not isinstance(tool_input, dict):
            return
        command = tool_input.get("command")
        cwd = data.get("cwd")
        if not isinstance(command, str) or not isinstance(cwd, str):
            return
        home = os.environ.get("HOME", "")
        if not decide(command, cwd, home):
            return
        if not write_log(command, cwd):
            return
        sys.stdout.write(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PermissionRequest",
                        "decision": {"behavior": "allow"},
                    }
                }
            )
        )
    except Exception:  # noqa: BLE001 - любой сбой: без решения
        return


if __name__ == "__main__":
    main()
