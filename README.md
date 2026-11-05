# Marketplace quiet-lab

Плагины Claude Code.

| Плагин | Что делает | Источник |
|---|---|---|
| `handoff` | передача работы между сессиями: навык `/handoff`, хук `session-handoff.sh` | `plugins/handoff/` |
| `duty` | дежурство по проекту: инспекция, workspace herdr, рутина субагентами | GitHub `quiet-lab/duty` (зеркало) |

## Подключение

```sh
claude plugin marketplace add quiet-lab/claude-plugins
claude plugin install handoff@quiet-lab
claude plugin install duty@quiet-lab
```

## Зеркало duty

Плагин `duty` ведётся в Bitbucket `QM/duty`, `quiet-lab/duty` — его
зеркало, ветвь `main`. После слияния pull request'а в `QM/duty` навык
`/duty:after-merge` обновляет зеркало:

```sh
git -C claude-plugins/duty fetch origin
git -C claude-plugins/duty push github origin/master:main
```

Remote `github` — `https://github.com/quiet-lab/duty.git`.
