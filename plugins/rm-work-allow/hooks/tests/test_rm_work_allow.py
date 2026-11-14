"""Тесты хука rm-work-allow: таблица входов и запуск сценария как в Claude Code."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "rm-work-allow.py"
ALLOW = {
    "hookSpecificOutput": {
        "hookEventName": "PermissionRequest",
        "decision": {"behavior": "allow"},
    }
}

# (имя, команда, cwd, ожидается allow). {H} — подставной HOME, {O} — каталог
# вне HOME, {W} — {H}/work. Режим bypassPermissions, если не сказано иное.
CASES = [
    ("glob-deep", "rm -f {H}/work/a/b/*", "{W}/a", True),
    ("glob-deep-tilde", "rm -f ~/work/a/b/*", "{O}", True),
    ("explicit-paths", "rm {W}/a/b/c {W}/a/b/d", "{O}", True),
    ("rf-dir-deep", "rm -rf {W}/a/b", "{O}", True),
    ("rf-flags-split", "rm -r -f -v -- {W}/a/b/c", "{O}", True),
    ("flags-after-target", "rm {W}/a/b/c -rf", "{O}", True),
    ("rmdir-deep", "rmdir {W}/a/b", "{O}", True),
    ("glob-prefix-name", "rm -f {W}/a/b/c*", "{O}", True),
    ("glob-question", "rm -f {W}/a/b/?", "{O}", True),
    ("relative-from-cwd", "rm -rf b/*", "{W}/a", True),
    ("relative-dotdot-inside", "rm -rf ../a/b/*", "{W}/a", True),
    ("relative-glob-in-cwd", "rm -f *", "{W}/a/b", True),
    ("quoted-space-path", "rm -rf '{W}/sp ace/x'", "{O}", True),
    ("dquoted-path", 'rm -rf "{W}/a/b/c"', "{O}", True),
    ("compound-and", "rm -f {W}/a/b/c && rm -rf {W}/pets/x", "{O}", True),
    ("compound-semicolon-ls", "ls {W}/a; rm -f {W}/a/b/c", "{O}", True),
    ("pipeline-subshell", "(rm -f {W}/a/b/c) | cat", "{O}", True),
    ("redirect-stderr", "rm -f {W}/a/b/c 2>/dev/null", "{O}", True),
    ("newline-separated", "rm -f {W}/a/b/c\nrm -f {W}/pets/x", "{O}", True),
    ("bin-rm", "/bin/rm -f {W}/a/b/c", "{O}", True),
    ("backslash-rm", "\\rm -f {W}/a/b/c", "{O}", True),
    ("comment-tail", "rm -f {W}/a/b/c # $(x)", "{O}", True),
    ("or-chain", "false || rm -f {W}/a/b/c", "{O}", True),
    ("work-itself", "rm -rf {W}", "{O}", False),
    ("work-itself-tilde", "rm -rf ~/work", "{O}", False),
    ("work-glob", "rm -rf ~/work/*", "{O}", False),
    ("first-level", "rm -rf {W}/pets", "{O}", False),
    ("first-level-glob", "rm -rf {W}/pets/*", "{O}", False),
    ("first-level-glob-tilde", "rm -rf ~/work/pets/*", "{O}", False),
    ("first-level-prefix-glob", "rm -rf {W}/pe*", "{O}", False),
    ("glob-in-middle", "rm -rf {W}/a/*/b", "{O}", False),
    ("glob-in-first-level-component", "rm -rf {W}/*/b", "{O}", False),
    ("outside", "rm -f {O}/f", "{O}", False),
    ("outside-glob", "rm -f {O}/*", "{O}", False),
    ("home-glob", "rm -f ~/*", "{H}", False),
    ("mixed-targets", "rm -f {W}/a/b/c {O}/f", "{O}", False),
    ("mixed-calls", "rm -f {W}/a/b/c && rm -f {O}/f", "{O}", False),
    ("dotdot-escape", "rm -rf {W}/a/b/../../../other", "{O}", False),
    ("relative-dotdot-escape", "rm -rf ../../../other/*", "{W}/a/b", False),
    ("symlink-out-dir", "rm -rf {W}/a/b/link/x", "{O}", False),
    ("symlink-out-glob", "rm -f {W}/a/b/link/*", "{O}", False),
    ("symlink-out-final", "rm -f {W}/a/b/link", "{O}", False),
    ("variable", "rm -rf $HOME/work/a/b/c", "{O}", False),
    ("variable-braces", "rm -rf ${HOME}/work/a/b/c", "{O}", False),
    ("variable-in-dquote", 'rm -rf "{W}/a/$X"', "{O}", False),
    ("assignment-prefix", "X=1 rm -f {W}/a/b/c", "{O}", False),
    ("assignment-before", "HOME=/ ; rm -f ~/work/a/b/c", "{O}", False),
    ("export-home", "export HOME=/x; rm -f ~/work/a/b/c", "{O}", False),
    ("command-subst", "rm -rf $(echo {W}/a/b/c)", "{O}", False),
    ("backticks", "rm -rf `echo {W}/a/b/c`", "{O}", False),
    ("backticks-in-dquote", 'rm -rf "{W}/a/`x`"', "{O}", False),
    ("subst-in-other-command", "echo $(date); rm -f {W}/a/b/c", "{O}", False),
    ("brace-expansion", "rm -f {W}/a/b/{c,d}", "{O}", False),
    ("brace-range", "rm -f {W}/a/b/{1..3}", "{O}", False),
    ("ansi-c-quote", "rm -f $'{W}/a/b/c'", "{O}", False),
    ("sudo", "sudo rm -f {W}/a/b/c", "{O}", False),
    ("env-rm", "env rm -f {W}/a/b/c", "{O}", False),
    ("command-rm", "command rm -f {W}/a/b/c", "{O}", False),
    ("builtin-rm", "builtin rm -f {W}/a/b/c", "{O}", False),
    ("nohup-rm", "nohup rm -f {W}/a/b/c", "{O}", False),
    ("timeout-rm", "timeout 5 rm -f {W}/a/b/c", "{O}", False),
    ("eval", "eval 'rm -f {W}/a/b/c'", "{O}", False),
    ("bash-c", "bash -c 'rm -f {W}/a/b/c'", "{O}", False),
    ("xargs", "echo {W}/a/b/c | xargs rm", "{O}", False),
    ("xargs-first", "xargs rm -f < {W}/a/list", "{O}", False),
    ("find-delete", "find {W}/a/b -name x -delete", "{O}", False),
    ("find-exec", "find {W}/a/b -exec rm {} \\;", "{O}", False),
    ("git-rm", "git rm -f {W}/a/b/c", "{O}", False),
    ("rm-as-argument", "echo rm; ls", "{O}", False),
    ("no-rm", "ls {W}/a/b", "{O}", False),
    ("cd-then-relative", "cd {W}/a/b && rm -f c", "{O}", False),
    ("cd-then-glob", "cd {W}/a/b && rm -rf *", "{W}/a/b", False),
    ("cd-then-absolute", "cd /tmp && rm -f {W}/a/b/c", "{O}", True),
    ("relative-outside-cwd", "rm -f f", "{O}", False),
    ("no-targets", "rm -rf", "{O}", False),
    ("only-options", "rm -rf --", "{O}", False),
    ("empty-target", "rm -f ''", "{W}/a/b", False),
    ("unknown-option", "rm --no-preserve-root -rf {W}/a/b/c", "{O}", False),
    ("unknown-short-option", "rm -rfx {W}/a/b/c", "{O}", False),
    ("interactive-eq", "rm --interactive=never {W}/a/b/c", "{O}", False),
    ("tilde-user", "rm -f ~root/x/y/z", "{O}", False),
    ("tilde-quoted", "rm -f '~/work/a/b/c'", "{O}", False),
    ("heredoc", "rm -f {W}/a/b/c <<EOF\nx\nEOF", "{O}", False),
    ("process-subst", "rm -f {W}/a/b/c >(cat)", "{O}", False),
    ("unclosed-quote", "rm -f '{W}/a/b/c", "{O}", False),
    ("backslash-newline", "rm -f {W}/a/b/c \\\n{O}/f", "{O}", False),
    ("function-def", "rm() { :; }; rm -f {W}/a/b/c", "{O}", False),
    ("alias", "alias rm=ls; rm -f {W}/a/b/c", "{O}", False),
    ("for-loop", "for f in a b; do rm -f {W}/a/b/$f; done", "{O}", False),
    ("case-clause", "case x in x) rm -f {W}/a/b/c;; esac", "{O}", False),
    ("rm-relative-path-cmd", "./rm -f {W}/a/b/c", "{O}", False),
    ("word-after-paren", "(ls) rm -f {W}/a/b/c", "{O}", False),
    ("unbalanced-paren", "(rm -f {W}/a/b/c", "{O}", False),
    ("rm-then-outside-via-glob-escape", "rm -f {W}/a/b/[/]*", "{O}", False),
    ("after-dashdash-escape", "rm -rf -- {W}/a/b/c ../..", "{W}/a/b", False),
    ("dotdot-final", "rm -rf {W}/a/b/c/../../..", "{O}", False),
    ("home-itself", "rm -rf ~", "{O}", False),
    ("quoted-tilde-relative-in-work", "rm -rf '~'", "{W}/a/b", True),
    ("redirect-target-var", "rm -f {W}/a/b/c > $X", "{O}", False),
    ("nul-byte", "rm -f {W}/a/b/c\x00 {O}/f", "{O}", False),
    ("carriage-return", "rm -f {W}/a/b/c\r{O}/f", "{O}", False),
]


class RmWorkAllowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name).resolve()
        cls.home = base / "home"
        cls.outside = base / "outside"
        cls.state = base / "state"
        work = cls.home / "work"
        for sub in ("a/b", "pets/x", "telecom", "sp ace/x"):
            (work / sub).mkdir(parents=True)
        (cls.outside / "other").mkdir(parents=True)
        (cls.home / "other").mkdir()
        (work / "a" / "b" / "link").symlink_to(cls.home / "other")
        (work / "a" / "b" / "c").touch()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def sub(self, text):
        return (
            text.replace("{W}", f"{self.home}/work")
            .replace("{H}", str(self.home))
            .replace("{O}", str(self.outside))
        )

    def run_raw(self, stdin, state=None):
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(self.home),
            "XDG_STATE_HOME": str(state or self.state),
        }
        proc = subprocess.run(
            [sys.executable, "-I", str(SCRIPT)],
            check=False,
            input=stdin,
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def run_hook(
        self,
        command,
        cwd,
        mode="bypassPermissions",
        tool="Bash",
        event="PermissionRequest",
    ):
        data = {
            "hook_event_name": event,
            "tool_name": tool,
            "permission_mode": mode,
            "cwd": cwd,
            "tool_input": {"command": command},
        }
        return self.run_raw(json.dumps(data))

    def test_table(self):
        self.assertGreaterEqual(len(CASES), 30)
        for name, command, cwd, expected in CASES:
            with self.subTest(name):
                out = self.run_hook(self.sub(command), self.sub(cwd))
                if expected:
                    self.assertEqual(json.loads(out), ALLOW)
                else:
                    self.assertEqual(out, "")

    def test_never_deny(self):
        for _, command, cwd, _ in CASES:
            out = self.run_hook(self.sub(command), self.sub(cwd))
            self.assertNotIn("deny", out)

    def test_other_modes_no_decision(self):
        for mode in ("default", "acceptEdits", "plan", "auto", "dontAsk", None):
            with self.subTest(mode):
                out = self.run_hook(
                    "rm -f {W}/a/b/c".replace("{W}", f"{self.home}/work"),
                    str(self.outside),
                    mode=mode,
                )
                self.assertEqual(out, "")

    def test_not_bash(self):
        for tool in ("Write", "Edit", "Read", "mcp__x__y", None):
            with self.subTest(tool):
                out = self.run_hook(
                    f"rm -f {self.home}/work/a/b/c", str(self.outside), tool=tool
                )
                self.assertEqual(out, "")

    def test_other_event(self):
        out = self.run_hook(
            f"rm -f {self.home}/work/a/b/c", str(self.outside), event="PreToolUse"
        )
        self.assertEqual(out, "")

    def test_broken_input(self):
        for raw in (
            "",
            "not json",
            "[]",
            "null",
            "{}",
            '{"tool_name":"Bash"}',
            "\x00",
            '{"tool_name":',
        ):
            with self.subTest(raw):
                self.assertEqual(self.run_raw(raw), "")

    def test_wrong_field_types(self):
        base = {
            "hook_event_name": "PermissionRequest",
            "tool_name": "Bash",
            "permission_mode": "bypassPermissions",
        }
        for extra in (
            {"tool_input": "rm x", "cwd": "/"},
            {"tool_input": {"command": 5}, "cwd": str(self.outside)},
            {"tool_input": {"command": "rm -f a/b"}, "cwd": 5},
            {"tool_input": {"command": f"rm -f {self.home}/work/a/b/c"}},
            {
                "tool_input": {"command": f"rm -f {self.home}/work/a/b/c"},
                "cwd": "relative",
            },
        ):
            with self.subTest(extra):
                self.assertEqual(self.run_raw(json.dumps({**base, **extra})), "")

    def test_log_written_on_allow(self):
        state = Path(self.tmp.name) / "state-log"
        command = f"rm -f {self.home}/work/a/b/c"
        data = {
            "hook_event_name": "PermissionRequest",
            "tool_name": "Bash",
            "permission_mode": "bypassPermissions",
            "cwd": str(self.outside),
            "tool_input": {"command": command},
        }
        self.assertEqual(json.loads(self.run_raw(json.dumps(data), state)), ALLOW)
        lines = (state / "rm-work-allow" / "allow.log").read_text().splitlines()
        self.assertEqual(len(lines), 1)
        entry = json.loads(lines[0])
        self.assertEqual(entry["command"], command)
        self.assertEqual(entry["cwd"], str(self.outside))
        self.assertIn("time", entry)

    def test_no_log_without_allow(self):
        state = Path(self.tmp.name) / "state-nolog"
        self.assertEqual(
            self.run_hook(f"rm -f {self.outside}/f", str(self.outside)), ""
        )
        self.assertFalse((state / "rm-work-allow").exists())

    def test_log_failure_no_decision(self):
        blocker = Path(self.tmp.name) / "blocker"
        blocker.write_text("file")
        data = {
            "hook_event_name": "PermissionRequest",
            "tool_name": "Bash",
            "permission_mode": "bypassPermissions",
            "cwd": str(self.outside),
            "tool_input": {"command": f"rm -f {self.home}/work/a/b/c"},
        }
        self.assertEqual(self.run_raw(json.dumps(data), blocker), "")


if __name__ == "__main__":
    unittest.main()
