# Плагин handoff

Передача незавершённой работы между сессиями Claude Code.

## Состав

- Навык `/handoff:handoff` — в конце сессии и после каждой задачи
  пересматривает сделанное, переписывает раздел «Текущая работа» файла
  `todo.local.md` в корне репозитория (снимок состояния, не журнал) и
  разбирает открытые вопросы в `docs/open-questions/`.
- Хук `hooks/session-handoff.sh`: `start` (SessionStart) отдаёт модели
  снимок и перечень вопросов, пользователю — выжимку; `end` (SessionEnd)
  отмечает сессию, которая меняла репозиторий без `/handoff`; `done`
  вызывает навык последним шагом.

Хук не знает ничего проектного. Выжимка собирается через `jq`; без него
хук предлагает его установить. Состояние сессий — в
`~/.claude/handoff-state/`, вне репозиториев.

## Установка

```sh
claude plugin marketplace add quiet-lab/claude-plugins
claude plugin install handoff@quiet-lab
```

Либо записями `extraKnownMarketplaces` и `enabledPlugins`
(`handoff@quiet-lab`) в `settings.json`.

## Проверка

```sh
bash hooks/session-handoff.sh start
```

Сводка заканчивается строкой «Конец сводки.».

## Источник правды

Плагин — единственный источник хука и навыка: копии в `~/.claude/` и в
`.claude/` проектов не заводятся.
