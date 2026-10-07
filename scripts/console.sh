#!/usr/bin/env bash
# htop처럼 터미널 전체를 쓰는 운영 관리 콘솔. 터미널에서 ./admin.sh 를 실행하면 열린다.
# 서비스·백업 상태는 10초마다 직접 확인하고, 공지 패널은 DB 조회용 일회성 컨테이너가 필요해
# 60초마다 백그라운드에서 새로 받아 키 입력이 멈추지 않게 한다.
# 메뉴 동작은 scripts/menu.sh를 그대로 쓰며, 실행하는 동안에는 일반 화면으로 돌아간다.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/operations.sh"
source "$ops_root/scripts/menu.sh"

ui_use_utf8_locale || true
require_operations

service_interval=10
notice_interval=60
min_cols=80
min_rows=24
# notices.dashboard --width가 허용하는 범위. 넓은 창에서도 이 폭으로 그려 받는다.
panel_min_width=40
panel_max_width=240
# 화면 고정 영역: 상단 바·서비스·백업·구분선 4줄 + 구분선·메뉴 제목·메뉴 5줄·설명 2줄·하단 바 10줄
fixed_rows=14

c_rev=$'\033[7m'
c_bold=$'\033[1m'
c_dim=$'\033[2m'
c_red=$'\033[31m'
c_green=$'\033[32m'
c_yellow=$'\033[33m'
c_reset=$'\033[0m'

selected=0
rows=24
cols=80
work_dir="$(mktemp -d "${TMPDIR:-/tmp}/seulseul-console-XXXXXX")"
notice_file="$work_dir/notice"
notice_pid=''
notice_width=0
notice_loaded_at=-$notice_interval
notice_state='공지 정보를 불러오는 중입니다…'
service_checked_at=-$service_interval
service_text=''
service_plain=''
backup_text=''
backup_plain=''
console_notice=''
console_notice_until=0
saved_stty="$(stty -g 2>/dev/null || true)"

enter_screen() {
    printf '\033[?1049h\033[?25l'
    stty -echo -icanon 2>/dev/null || true
}

leave_screen() {
    printf '\033[?25h\033[?1049l'
    if [[ -n "$saved_stty" ]]; then
        stty "$saved_stty" 2>/dev/null || true
    fi
}

finish() {
    if [[ -n "$notice_pid" ]]; then
        kill "$notice_pid" 2>/dev/null || true
    fi
    leave_screen
    rm -rf -- "$work_dir"
}
trap finish EXIT
trap 'exit 130' INT TERM

read_size() {
    local size
    size="$(stty size 2>/dev/null || true)"
    if [[ "$size" =~ ^([0-9]+)\ ([0-9]+)$ ]]; then
        rows="${BASH_REMATCH[1]}"
        cols="${BASH_REMATCH[2]}"
    fi
}

# 지금 창에 맞는 공지 패널 폭. 대시보드가 허용하는 범위로 제한한다.
panel_width() {
    if ((cols > panel_max_width)); then
        echo "$panel_max_width"
    elif ((cols < panel_min_width)); then
        echo "$panel_min_width"
    else
        echo "$cols"
    fi
}

# 공지 패널은 화면 폭에 맞춰 Python이 그린다. stdin을 막아 키 입력을 가로채지 않게 한다.
# 종료 시 남은 조회는 읽기 전용이고 몇 초 안에 끝나며 --rm으로 스스로 정리되므로 기다리지 않는다.
start_notice_fetch() {
    if [[ -n "$notice_pid" ]]; then
        return
    fi
    notice_width="$(panel_width)"
    (
        compose run --rm --no-deps -T bot python -m seulseul.notices.dashboard \
            --panel --width "$notice_width" < /dev/null > "$notice_file.next" 2> "$notice_file.err"
        mv -- "$notice_file.next" "$notice_file"
    ) &
    notice_pid=$!
}

poll_notice_fetch() {
    if [[ -z "$notice_pid" ]] || kill -0 "$notice_pid" 2>/dev/null; then
        return
    fi
    if wait "$notice_pid"; then
        notice_state=''
    else
        notice_state='✖ 공지 정보를 불러오지 못했습니다. 서비스 상태(위)와 DB를 확인하세요.'
    fi
    notice_pid=''
    notice_loaded_at=$SECONDS
}

# 서비스 한 줄 요약. 폭 계산을 위해 색 없는 문자열도 함께 만든다.
refresh_services() {
    local rows_text service state status name mark word down=false
    service_text=''
    service_plain=''
    if ! rows_text="$(compose ps -a --format '{{.Service}}|{{.State}}|{{.Status}}' 2>/dev/null)"; then
        # 형식 지정을 지원하지 않거나 Docker에 연결하지 못하면 "서비스 없음"으로 오해하지 않게 알린다.
        service_text="${c_yellow}⚠${c_reset} 서비스 상태를 읽지 못했습니다 · 종료 후 ./view.sh 로 확인"
        service_plain='⚠ 서비스 상태를 읽지 못했습니다 · 종료 후 ./view.sh 로 확인'
        refresh_backup
        service_checked_at=$SECONDS
        return
    fi
    while IFS='|' read -r service state status; do
        [[ -n "$service" ]] || continue
        case "$service" in
            bot) name='봇' ;;
            oauth) name='OAuth' ;;
            ngrok) name='터널' ;;
            postgres) name='DB' ;;
            *) continue ;;
        esac
        if [[ "$state" == running && "$status" != *unhealthy* ]]; then
            mark="${c_green}●${c_reset}" word='실행 중'
        elif [[ "$state" == running ]]; then
            mark="${c_yellow}●${c_reset}" word='상태 이상' down=true
        elif [[ "$state" == restarting ]]; then
            mark="${c_yellow}●${c_reset}" word='재시작 반복' down=true
        else
            mark="${c_red}○${c_reset}" word='중지됨' down=true
        fi
        service_text+="${mark} ${name} ${word}   "
        service_plain+="● ${name} ${word}   "
    done <<< "$rows_text"
    if [[ -z "$service_plain" ]]; then
        service_text="${c_red}○${c_reset} 실행 중인 서비스가 없습니다 · ${c_yellow}9번으로 시작${c_reset}"
        service_plain='○ 실행 중인 서비스가 없습니다 · 9번으로 시작'
    elif [[ "$down" == true ]]; then
        service_text+="${c_yellow}› 9번: 시작·재배포${c_reset}"
        service_plain+='› 9번: 시작·재배포'
    fi
    refresh_backup
    service_checked_at=$SECONDS
}

refresh_backup() {
    local schedule_part schedule_plain latest_part latest_plain
    read_backup_schedule
    case "$schedule_state" in
        on)
            schedule_part="${c_green}✔${c_reset} 자동 백업 켜짐 (매일 03:00)"
            schedule_plain='✔ 자동 백업 켜짐 (매일 03:00)'
            ;;
        unavailable)
            schedule_part='• 자동 백업 상태 확인 불가'
            schedule_plain="$schedule_part"
            ;;
        stale)
            schedule_part="${c_red}✖${c_reset} 자동 백업 경로가 맞지 않아 실패 중 · 8번으로 다시 등록"
            schedule_plain='✖ 자동 백업 경로가 맞지 않아 실패 중 · 8번으로 다시 등록'
            ;;
        *)
            schedule_part="${c_yellow}⚠${c_reset} 자동 백업 꺼짐 · 8번으로 켜기"
            schedule_plain='⚠ 자동 백업 꺼짐 · 8번으로 켜기'
            ;;
    esac
    read_latest_backup
    if [[ -n "$latest_backup" ]]; then
        latest_part="${c_green}✔${c_reset} 마지막 성공 ${latest_backup_time} (보관 ${latest_backup_count}개)"
        latest_plain="✔ 마지막 성공 ${latest_backup_time} (보관 ${latest_backup_count}개)"
    else
        latest_part="${c_yellow}⚠${c_reset} 아직 성공한 백업 없음 · 7번으로 백업"
        latest_plain='⚠ 아직 성공한 백업 없음 · 7번으로 백업'
    fi
    backup_text="${schedule_part}   ${latest_part}"
    backup_plain="${schedule_plain}   ${latest_plain}"
}

# 줄 하나를 화면 폭에 맞춘다. 색 있는 문자열이 넘치면 색 없는 문자열을 잘라 쓴다.
fit_line() {
    local colored="$1" plain="$2" width="$3"
    ui_measure "$plain"
    if ((ui_width_result <= width)); then
        fitted_line="$colored"
    else
        ui_cell "$plain" "$width"
        fitted_line="$ui_cell_result"
    fi
}

# 설명 문장을 단어 단위로 width칸씩 최대 두 줄로 나눈다.
wrap_description() {
    local text="$1" width="$2" word line='' candidate
    description_lines=()
    for word in $text; do
        candidate="${line:+$line }$word"
        ui_measure "$candidate"
        if ((ui_width_result > width)) && [[ -n "$line" ]]; then
            description_lines+=("$line")
            line="$word"
        else
            line="$candidate"
        fi
    done
    description_lines+=("$line")
    if ((${#description_lines[@]} > 2)); then
        ui_cell "${description_lines[1]} ${description_lines[2]}" "$width"
        description_lines=("${description_lines[0]}" "$ui_cell_result")
    fi
}

# 반전 막대(상단·하단 바). 왼쪽 글과 오른쪽 글 사이를 공백으로 채운다.
bar_line() {
    local left="$1" right="$2" gap
    ui_measure "$left$right"
    gap=$((cols - ui_width_result))
    if ((gap < 1)); then
        ui_cell "$left" "$cols"
        bar_result="${c_rev}${c_bold}${ui_cell_result}${c_reset}"
        return
    fi
    printf -v bar_result '%s%s%s%*s%s%s' "$c_rev" "$c_bold" "$left" "$gap" '' "$right" "$c_reset"
}

menu_cell() {
    local index="$1" width="$2"
    ui_cell "  ${menu_keys[index]}  ${menu_labels[index]}" "$width"
    if ((index == selected)); then
        cell_result="${c_rev}${c_bold}${ui_cell_result}${c_reset}"
    else
        cell_result="$ui_cell_result"
    fi
}

draw() {
    local frame='' panel_rows line count=0 shown=() panel_text half rule i
    read_size
    if ((cols < min_cols || rows < min_rows)); then
        printf '\033[H\033[2J  창이 작습니다 (%s×%s). %s×%s 이상으로 키워 주세요. q: 종료' \
            "$cols" "$rows" "$min_cols" "$min_rows"
        return
    fi
    printf -v rule '%*s' "$cols" ''
    rule="${c_dim}${rule// /─}${c_reset}"
    panel_rows=$((rows - fixed_rows))

    bar_line " 슬슬 관리 콘솔 │ $(ui_now '%H:%M:%S')" "서비스 ${service_interval}초 · 공지 ${notice_interval}초마다 새로고침 "
    frame+="$bar_result"$'\033[K\n'
    fit_line " ${c_bold}서비스${c_reset}  $service_text" " 서비스  $service_plain" "$cols"
    frame+="$fitted_line"$'\033[K\n'
    fit_line " ${c_bold}백업${c_reset}    $backup_text" " 백업    $backup_plain" "$cols"
    frame+="$fitted_line"$'\033[K\n'
    frame+="$rule"$'\033[K\n'

    # 공지 패널: Python이 그린 화면을 남은 높이만큼 보여 준다.
    if [[ -s "$notice_file" ]]; then
        while IFS= read -r line; do
            shown+=("$line")
        done < "$notice_file"
    fi
    if [[ -n "$notice_state" ]]; then
        shown=("  $notice_state" "${shown[@]+"${shown[@]}"}")
    fi
    if ((${#shown[@]} == 0)); then
        shown=('  표시할 공지 정보가 없습니다.')
    fi
    if ((${#shown[@]} > panel_rows)); then
        shown=("${shown[@]:0:panel_rows-1}" '  … 화면이 작아 일부만 표시합니다 · 5번: 공지 대시보드 전체 보기')
    fi
    panel_text=''
    for line in "${shown[@]}"; do
        if ((notice_width > cols)); then
            ui_cell "$line" "$cols"
            line="$ui_cell_result"
        fi
        panel_text+="$line"$'\n'
        count=$((count + 1))
    done
    while ((count < panel_rows)); do
        panel_text+=$'\n'
        count=$((count + 1))
    done
    while IFS= read -r line; do
        frame+="$line"$'\033[K\n'
    done < <(printf '%s' "$panel_text" | ui_paint)

    frame+="$rule"$'\033[K\n'
    frame+=" ${c_bold}■ 메뉴${c_reset}  ${c_dim}↑↓←→ 고르기 · Enter 실행 · 번호를 누르면 바로 실행${c_reset}"$'\033[K\n'
    half=$((cols / 2))
    for ((i = 0; i < 5; i++)); do
        menu_cell "$i" "$half"
        line="$cell_result"
        menu_cell $((i + 5)) $((cols - half))
        frame+="$line$cell_result"$'\033[K\n'
    done
    wrap_description "[${menu_groups[selected]}] ${menu_descriptions[selected]}" $((cols - 4))
    if [[ -n "$console_notice" ]] && ((SECONDS < console_notice_until)); then
        ui_cell "$console_notice" $((cols - 4))
        frame+=" ${c_yellow}⚠${c_reset} ${ui_cell_result}"$'\033[K\n'
        frame+=" ${c_dim}┆${c_reset} ${description_lines[0]}"$'\033[K\n'
    else
        frame+=" ${c_dim}┆${c_reset} ${description_lines[0]}"$'\033[K\n'
        frame+=" ${c_dim}┆${c_reset} ${description_lines[1]:-}"$'\033[K\n'
    fi
    bar_line ' ? 도움말   Enter 실행   1~0 바로 실행   r 새로고침   q 종료' ''
    frame+="$bar_result"$'\033[K'
    printf '\033[H%s' "$frame"
}

show_help() {
    local line help_lines=(
        "${c_bold}슬슬 관리 콘솔 도움말${c_reset}"
        ''
        "${c_bold}■ 화면 읽는 법${c_reset}"
        '  서비스  봇(Slack 공지를 읽고 DM을 보냄) · OAuth(다른 워크스페이스 설치용 웹 서버)'
        '          터널(ngrok, OAuth 서버를 외부에 연결) · DB(공지·학생·체크리스트 저장)'
        "          ${c_green}●${c_reset} 실행 중  ${c_yellow}●${c_reset} 이상·재시작 반복  ${c_red}○${c_reset} 중지됨"
        '  백업    매일 03:00 자동 백업이 켜져 있는지와 마지막으로 성공한 백업 시각'
        '  공지    차트는 최근 공지 상태와 7일 안의 마감 분포, 목록은 손봐야 할 공지'
        ''
        "${c_bold}■ 공지 상태 용어${c_reset}"
        "  ${c_green}✔${c_reset} 정상            AI 분석이 끝나 학생 체크리스트에 반영됨"
        "  ↻ 자동 재시도 대기  일시 장애로 실패. 봇이 5분·15분·1시간 뒤 스스로 다시 시도"
        "  ${c_red}✖${c_reset} 수동 조치 필요    자동 재시도도 실패. 1번 메뉴로 AI 재분석하거나 2번으로 직접 수정"
        "  ${c_yellow}⚠${c_reset} 미적용 원본      받았지만 아직 반영 전. 잠시 뒤 사라지면 정상, 계속 남으면 3번 확인"
        ''
        "${c_bold}■ 키${c_reset}"
        '  ↑ ↓ ← →  메뉴 고르기      Enter  고른 메뉴 실행      1~9, 0  해당 메뉴 바로 실행'
        '  r        지금 새로고침    ?      이 도움말           q      콘솔 종료'
        '  작업이 끝나면 Enter로 콘솔에 돌아옵니다. 작업 중 Ctrl+C는 그 작업만 멈춥니다.'
        ''
        "${c_dim}아무 키나 누르면 돌아갑니다.${c_reset}"
    )
    printf '\033[H\033[2J'
    for line in "${help_lines[@]}"; do
        printf ' %s\033[K\n' "$line"
    done
    IFS= read -rsn1 _ || true
}

run_selected() {
    leave_screen
    printf '\033[H\033[2J'
    menu_run "${menu_keys[selected]}" || true
    ui_drain_input || true
    enter_screen
    service_checked_at=-$service_interval
    notice_loaded_at=-$notice_interval
}

enter_screen
read_size
start_notice_fetch
while true; do
    poll_notice_fetch
    if ((SECONDS - service_checked_at >= service_interval)); then
        refresh_services
    fi
    if [[ -z "$notice_pid" ]] && { ((SECONDS - notice_loaded_at >= notice_interval)) || ((notice_width != $(panel_width))); }; then
        start_notice_fetch
    fi
    draw
    # 시간 초과는 bash 3.2에서 1, bash 4 이상에서 128보다 큰 값이라 둘 다 새로고침으로 본다.
    # 비정규 입력 모드라 키로 EOF가 생기지 않으므로, 터미널이 사라진 경우에만 끝낸다.
    key=''
    if ! IFS= read -rsn1 -t 1 key; then
        [[ -t 0 ]] || exit 0
        continue
    fi
    if [[ "$key" == $'\033' ]]; then
        sequence=''
        IFS= read -rsn2 -t 1 sequence || true
    fi
    # 붙여넣기처럼 키가 한꺼번에 들어오면 글자를 메뉴 명령으로 실행하지 않는다.
    if ui_drain_input; then
        console_notice='붙여 넣은 입력은 메뉴에서 무시했습니다. 공지 원문은 2번 메뉴를 연 뒤 붙여 넣으세요.'
        console_notice_until=$((SECONDS + 6))
        continue
    fi
    console_notice=''
    case "$key" in
        $'\033')
            case "$sequence" in
                '[A') selected=$(((selected + 9) % 10)) ;;
                '[B') selected=$(((selected + 1) % 10)) ;;
                '[C' | '[D') selected=$(((selected + 5) % 10)) ;;
                'OP') show_help ;;
            esac
            ;;
        '' | ' ') run_selected ;;
        [0-9])
            menu_find "$key"
            selected=$menu_index
            run_selected
            ;;
        '?' | h | H) show_help ;;
        r | R)
            service_checked_at=-$service_interval
            notice_loaded_at=-$notice_interval
            ;;
        q | Q) exit 0 ;;
    esac
done
