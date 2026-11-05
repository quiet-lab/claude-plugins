#!/usr/bin/env bash
# Передача работы между сессиями: снимок при старте и отметка при завершении.
#
# Режимы (первый аргумент):
#   start — хук SessionStart: отдаёт модели раздел «Текущая работа»
#           из todo.local.md, перечень отложенных вопросов из docs/open-questions/
#           и сообщение о прошлых сессиях, завершившихся без /handoff,
#           а пользователю показывает выжимку из того же;
#   end   — хук SessionEnd: запоминает, что сессия что-то изменила
#           в репозитории, а снимок за неё не обновлялся;
#   done  — вызывается навыком /handoff последним шагом: снимок обновлён.
#
# Вызвать навык при выходе нельзя: SessionEnd срабатывает, когда сессия уже
# закончилась, и модели в нём нет. Поэтому пропущенный /handoff не
# предотвращается, а обнаруживается при следующем старте.
#
# Файл поставляет плагин handoff (marketplace quiet-lab); других копий нет,
# скрипт не знает ничего проектного.
#
# Снимок лежит в корне репозитория в todo.local.md: файл этой машины,
# исключённый через .gitignore (строка *.local.md), у каждой машины свой.
# Проект, который ведёт снимок в репозитории (например, личные dotfiles),
# держит его в todo.md: он читается, когда местного файла нет. Нет
# ни одного — хук говорит об этом одной строкой и печатает остальное.
#
# Простой stdout хука SessionStart достаётся только модели, в терминале его
# нет. Пользователю текст показывает поле systemMessage ответа в JSON,
# поэтому ответ собирает jq. Без jq хук не молчит, а предлагает его
# установить. Запуск без идентификатора сессии считается ручным и печатает
# обычный текст: так хук проверяют навык /handoff и человек.

set -uo pipefail

mode=${1:-start}

# Сессия агента duty:worker ведёт одну задачу в своём workspace herdr:
# снимок todo.local.md и учёт пропущенного /handoff принадлежат главной сессии,
# а задача рабочего агента — в его каталоге. Режим done не затрагивается.
if [ "${DUTY_ROLE:-}" = 'worker' ] && [ "$mode" != 'done' ]; then
    [ "$mode" = 'start' ] &&
        printf 'Сессия роли worker: снимок todo.local.md и сверка родителей пропущены, задача — %s\n' "${DUTY_TASK:-не задана}"
    exit 0
fi

# Данные события приходят на stdin. При ручном запуске stdin — терминал или
# пустой поток; ждать там нечего, отсюда и срок.
input=''
[ -t 0 ] || input=$(timeout 2 cat 2>/dev/null || true)

have_jq=0
command -v jq > /dev/null 2>&1 && have_jq=1

# Разбор через sed оставлен на случай без jq: отметки о начале и конце сессии
# должны ставиться и тогда, иначе пропущенный /handoff останется незамеченным.
json_field() {
    if [ "$have_jq" -eq 1 ]; then
        printf '%s' "$input" | jq -r --arg key "$1" '.[$key] // empty' 2> /dev/null
        return 0
    fi
    printf '%s' "$input" | tr -d '\n' |
        sed -n 's/.*"'"$1"'"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p'
}

root=${CLAUDE_PROJECT_DIR:-}
[ -n "$root" ] || root=$(git rev-parse --show-toplevel 2>/dev/null) || root=$PWD

self=$(realpath "${BASH_SOURCE[0]}")

# Состояние хранится вне репозитория: это сведения о сессиях одной машины,
# в истории проекта им делать нечего.
state="$HOME/.claude/handoff-state/$(printf '%s' "$root" | sha1sum | cut -c1-16)"

# Отпечаток состояния репозитория: вершина и содержимое git status. Коммит
# в сабмодуле меняет status основного репозитория, поэтому виден и он.
fingerprint() {
    git -C "$root" rev-parse --is-inside-work-tree > /dev/null 2>&1 || return 1
    {
        git -C "$root" rev-parse HEAD 2> /dev/null
        git -C "$root" status --porcelain 2> /dev/null
    } | sha1sum | cut -c1-40
}

handoff_done_at() {
    cat "$state/handoff-done" 2> /dev/null || echo 0
}

# Раздел печатается целиком: обрезка прятала бы открытые вопросы, с которых
# сессия и должна начинаться. Порог задаёт только предупреждение: разросшийся
# снимок означает, что в нём копится сделанное или отложенное, и лечится это
# разбором раздела, а не сокращением вывода.
snapshot_missing='Местного снимка todo.local.md нет'

current_work() {
    local threshold=150
    local file="$root/todo.local.md" section lines

    [ -f "$file" ] || file="$root/todo.md"
    if [ ! -f "$file" ]; then
        printf '%s\n' "$snapshot_missing"
        return 0
    fi

    section=$(awk '
        /^## / {
            if (inside) { exit }
            if ($0 == "## Текущая работа" || $0 == "## Current work") {
                inside = 1; print; next
            }
        }
        inside { print }
    ' "$file")

    [ -n "$section" ] || return 0

    printf '%s\n' "$section"

    lines=$(printf '%s\n' "$section" | wc -l)
    if [ "$lines" -gt "$threshold" ]; then
        printf '\nРаздел «Текущая работа» занимает %s строк: снимок разросся,\n' "$lines"
        printf 'его надо разобрать при следующем /handoff.\n'
    fi
}

# Первый содержательный абзац раздела с заданным заголовком, сведённый
# в одну строку. Абзац берётся целиком, а не первой строкой: текст
# в документах перенесён по ширине, и одна строка обрывает фразу посередине.
section_text() {
    awk -v want="$2" '
        /^## / {
            if (collected) { exit }
            inside = ($0 == want)
            next
        }
        !inside { next }
        NF { text = text (collected ? " " : "") $0; collected = 1; next }
        collected { exit }
        END { if (collected) { print text } }
    ' "$1"
}

# Читаются сами файлы вопросов, а не перечень в README: перечень ведётся
# вручную и способен от них отстать.
open_questions() {
    local dir="$root/docs/open-questions"
    local file name question condition header=0

    [ -d "$dir" ] || return 0

    for file in "$dir"/*.md; do
        [ -f "$file" ] || continue
        name=$(basename "$file")
        [ "$name" = 'README.md' ] && continue

        question=$(section_text "$file" '## Вопрос')
        condition=$(section_text "$file" '## Условие пересмотра')
        [ -n "$question" ] || question='вопрос не записан'
        [ -n "$condition" ] || condition='условие пересмотра не записано'

        if [ "$header" -eq 0 ]; then
            printf 'Открытые вопросы (docs/open-questions/):\n'
            header=1
        fi
        printf '  %s — %s Пересмотр: %s\n' "$name" "$question" "$condition"
    done
}

# Сессии, которые что-то изменили и закончились без /handoff. Отметка снимается
# сама, как только /handoff выполнен позже её: снимок с тех пор переписан.
missed_handoffs() {
    local done_at file sid ended_at header=0

    [ -d "$state/ended" ] || return 0
    done_at=$(handoff_done_at)

    for file in "$state/ended"/*; do
        [ -f "$file" ] || continue
        sid=$(basename "$file")
        ended_at=$(sed -n '1p' "$file")
        if [ "${ended_at:-0}" -le "$done_at" ]; then
            rm -f "$file"
            continue
        fi

        if [ "$header" -eq 0 ]; then
            printf 'Без /handoff завершились сессии, менявшие репозиторий; снимок\n'
            printf '«Текущая работа» мог устареть:\n'
            header=1
        fi
        printf '  %s, завершена %s\n' "$sid" "$(date -d "@$ended_at" '+%Y-%m-%d %H:%M')"
    done

    [ "$header" -eq 1 ] || return 0
    printf 'Снимок восстанавливается по git log и состоянию дерева либо\n'
    printf 'в самой сессии: claude --resume <идентификатор>, затем /handoff.\n'
    printf 'Снять отметки без /handoff: bash %s done\n' "$self"
}

# Отпечаток на старте нужен, чтобы при завершении отличить сессию, которая
# что-то изменила, от разговора без последствий. При сжатии контекста сессия
# продолжается, и исходный отпечаток сохраняется.
remember_start() {
    local sid source print

    sid=$(json_field session_id)
    [ -n "$sid" ] || return 0
    source=$(json_field source)
    [ "$source" = 'compact' ] && [ -f "$state/started/$sid" ] && return 0

    print=$(fingerprint) || return 0
    mkdir -p "$state/started"
    printf '%s\n%s\n' "$print" "$(date +%s)" > "$state/started/$sid"

    find "$state/started" -type f -mtime +30 -delete 2> /dev/null
}

remember_end() {
    local sid print started_print started_at

    sid=$(json_field session_id)
    [ -n "$sid" ] && [ -f "$state/started/$sid" ] || return 0

    started_print=$(sed -n '1p' "$state/started/$sid")
    started_at=$(sed -n '2p' "$state/started/$sid")
    rm -f "$state/started/$sid"

    print=$(fingerprint) || return 0

    if [ "$print" != "$started_print" ] && [ "$(handoff_done_at)" -lt "${started_at:-0}" ]; then
        mkdir -p "$state/ended"
        date +%s > "$state/ended/$sid"
    else
        rm -f "$state/ended/$sid"
    fi
}

# Тело подраздела снимка, заголовок которого подходит под образец. Режим first
# обрывает тело на первом абзаце, а в нумерованном перечне — на первом пункте.
subsection() {
    printf '%s\n' "$1" | awk -v want="$2" -v first="${3:-}" '
        /^### / {
            if (inside) { exit }
            inside = ($0 ~ want)
            next
        }
        !inside { next }
        !NF && !started { next }
        first && started && (!NF || /^[0-9]+\. /) { exit }
        { print; started = 1 }
    '
}

# Выжимка для пользователя: цель, вопросы к нему и первый шаг. Снимок целиком
# занимает полтораста строк, и в терминале перед приглашением он вытеснил бы
# с экрана всё остальное; полный текст получает модель, а человеку он доступен
# в todo.local.md.
digest() {
    local work=$1 questions=$2 missed=$3
    local goal asks step steps deferred

    if [ "$work" = "$snapshot_missing" ]; then
        printf '%s\n' "$work"
    elif [ -n "$work" ]; then
        goal=$(subsection "$work" '^### (Цель|Goal)' first)
        asks=$(subsection "$work" '^### (Открытые вопросы|Open questions)')
        step=$(subsection "$work" '^### (Следующие шаги|Next steps)' first)
        steps=$(subsection "$work" '^### (Следующие шаги|Next steps)' | grep -c '^[0-9]\+\. ')

        printf 'Снимок незавершённой работы (todo.local.md, «Текущая работа»)\n'
        [ -z "$goal" ] || printf '\nЦель:\n%s\n' "$goal"
        [ -z "$asks" ] || printf '\nВопросы к пользователю:\n%s\n' "$asks"
        [ -z "$step" ] || printf '\nПервый шаг из %s:\n%s\n' "$steps" "$step"
    fi

    if [ -n "$questions" ]; then
        deferred=$(printf '%s\n' "$questions" | grep -c '^  ')
        printf '\nОтложенных вопросов в docs/open-questions/: %s\n' "$deferred"
    fi

    [ -z "$missed" ] || printf '\n%s\n' "$missed"
}

# Сообщение об отсутствии jq состоит из постоянного текста без кавычек
# и обратных косых черт, поэтому записано готовым JSON: экранировать
# произвольный текст снимка без jq нечем.
jq_missing_reply() {
    cat << 'JSON'
{"systemMessage":"Хук передачи работы не нашёл jq и не может показать выжимку снимка незавершённой работы. Установка: mise use -g jq либо пакет jq из дистрибутива (pacman -S jq, apt install jq, dnf install jq, brew install jq).","hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"Хук передачи работы не нашёл jq и снимок не передал. Прочитай раздел «Текущая работа» в todo.local.md в корне репозитория и файлы docs/open-questions/ либо запусти session-handoff.sh start вручную из каталога hooks плагина handoff: без данных сессии он печатает сводку обычным текстом. Пользователю предложение установить jq уже показано."}}
JSON
}

# Отметка конца сводки. Документация Claude Code не называет предела на объём
# вывода хука, но и не обещает, что его нет. Если в начале сессии этой строки
# не видно, вывод обрезала среда, и часть сводки до модели не дошла.
report_start() {
    local work questions missed part summary='' brief

    work=$(current_work)
    questions=$(open_questions)
    missed=$(missed_handoffs)

    for part in "$work" "$questions" "$missed"; do
        [ -n "$part" ] || continue
        summary=${summary:+$summary$'\n\n'}$part
    done
    [ -n "$summary" ] || return 0
    summary+=$'\n\nКонец сводки.'

    if [ -z "$(json_field session_id)" ]; then
        brief=$(digest "$work" "$questions" "$missed")
        printf '%s\n\nВыжимка, которую увидит пользователь:\n\n%s\n' "$summary" "$brief"
        return 0
    fi

    if [ "$have_jq" -eq 0 ]; then
        jq_missing_reply
        return 0
    fi

    # После сжатия контекста сводка нужна модели заново, а пользователь свою
    # выжимку уже видел в начале сессии.
    brief=''
    [ "$(json_field source)" = 'compact' ] || brief=$(digest "$work" "$questions" "$missed")

    jq -n --arg user "$brief" --arg model "$summary" '
        {hookSpecificOutput: {hookEventName: "SessionStart", additionalContext: $model}}
        + (if $user == "" then {} else {systemMessage: $user} end)
    '
}

case "$mode" in
    start)
        report_start
        remember_start
        ;;
    end)
        remember_end
        ;;
    done)
        mkdir -p "$state"
        date +%s > "$state/handoff-done"
        ;;
    *)
        printf 'session-handoff.sh: неизвестный режим %s\n' "$mode" >&2
        exit 2
        ;;
esac
