#!/usr/bin/env bash
# 운영 스크립트 공통 화면 출력. 처음 보는 운영자도 상태와 다음 할 일을 알 수 있도록
# 배너·구획·상태 기호를 통일한다. 기호 규칙은 Python 출력(notices/screen.py)과 같다.
# 한글 표시 폭을 계산하지 않아도 깨지지 않도록 오른쪽 테두리와 한글 열 정렬은 쓰지 않는다.

# 색은 터미널에서만 쓴다. 파이프·파일·NO_COLOR·TERM=dumb에서는 기호만 남긴다.
if [[ -t 1 && -z "${NO_COLOR:-}" && "${TERM:-dumb}" != dumb ]]; then
    ui_bold=$'\033[1m'
    ui_dim=$'\033[2m'
    ui_red=$'\033[31m'
    ui_green=$'\033[32m'
    ui_yellow=$'\033[33m'
    ui_blue=$'\033[34m'
    ui_cyan=$'\033[36m'
    ui_reset=$'\033[0m'
else
    ui_bold='' ui_dim='' ui_red='' ui_green='' ui_yellow='' ui_blue='' ui_cyan='' ui_reset=''
fi
ui_heavy_rule='━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━'
ui_light_rule='────────────────────────────────────────────────────────────'

# 화면 제목. 두 번째 인자는 흐린 부가 정보로 붙인다.
ui_banner() {
    local subtitle=''
    if [[ -n "${2:-}" ]]; then
        subtitle=" ${ui_dim}· $2${ui_reset}"
    fi
    printf '%s\n  %s%s%s%s\n%s\n' \
        "$ui_heavy_rule" "$ui_bold" "$1" "$ui_reset" "$subtitle" "$ui_heavy_rule"
}

ui_section() { printf '\n%s■ %s%s\n' "$ui_bold" "$1" "$ui_reset"; }
ui_ok() { printf '  %s✔%s %s\n' "$ui_green" "$ui_reset" "$1"; }
ui_warn() { printf '  %s⚠%s %s\n' "$ui_yellow" "$ui_reset" "$1"; }
ui_info() { printf '  • %s\n' "$1"; }
ui_hint() { printf '  %s›%s %s\n' "$ui_cyan" "$ui_reset" "$1"; }
ui_step() { printf '\n  %s[%s/%s]%s %s%s%s\n' "$ui_blue" "$1" "$2" "$ui_reset" "$ui_bold" "$3" "$ui_reset"; }

# 실패는 표준 오류로 보낸다. 표준 출력을 파이프로 넘겨도 오류 안내는 화면에 남는다.
ui_fail() {
    if [[ -t 2 && -n "$ui_reset" ]]; then
        printf '  %s✖%s %s\n' "$ui_red" "$ui_reset" "$1" >&2
    else
        printf '  ✖ %s\n' "$1" >&2
    fi
}

# 꼬리말: 연한 가로줄과 다음에 실행할 명령 안내.
ui_footer() {
    local line
    printf '\n%s%s%s\n' "$ui_dim" "$ui_light_rule" "$ui_reset"
    for line in "$@"; do
        ui_hint "$line"
    done
}

# 도움말 한 줄. 명령은 ASCII라서 printf 폭 지정만으로 설명 열이 맞는다.
ui_usage_row() { printf '  %s%-36s%s %s\n' "$ui_cyan" "$1" "$ui_reset" "$2"; }

# 메뉴 항목: 번호와 할 일.
ui_menu_item() { printf '    %s%s%s  %s\n' "$ui_bold" "$1" "$ui_reset" "$2"; }

# 붙여넣기·연타처럼 이미 도착해 있는 키 입력을 버린다. 버린 입력이 있으면 0을 돌려준다.
# 이전 작업에서 남은 붙여넣기 줄이 다음 질문(확인·메뉴 선택)의 답으로 들어가는 것을 막는다.
# 터미널일 때만 동작하며, 비정규 모드로 0.1초 동안 새 입력이 없을 때까지 읽어 버린 뒤 원래 모드로 돌린다.
ui_drain_input() {
    local mode count
    [[ -t 0 ]] || return 1
    mode="$(stty -g 2>/dev/null)" || return 1
    stty -icanon min 0 time 1 2>/dev/null || return 1
    count="$(cat | wc -c)"
    stty "$mode" 2>/dev/null || true
    ((count > 0))
}

# y를 입력해야만 0을 반환한다. Enter·EOF·그 밖의 입력은 모두 취소다.
ui_confirm() {
    local answer
    ui_drain_input || true
    if ! IFS= read -r -p "  $1 [y/N]: " answer; then
        echo
        return 1
    fi
    [[ "$answer" =~ ^[Yy]$ ]]
}

ui_clear() {
    if [[ -t 1 ]]; then
        printf '\033[2J\033[H'
    fi
}

# 문자 단위로 자르고 폭을 재려면 UTF-8 로캘이 필요하다. 현재 로캘이 UTF-8이 아니면 있는 것으로 바꾼다.
ui_use_utf8_locale() {
    local candidate
    if [[ "$(locale charmap 2>/dev/null)" == UTF-8 ]]; then
        return 0
    fi
    for candidate in C.UTF-8 C.utf8 en_US.UTF-8 en_US.utf8; do
        if locale -a 2>/dev/null | grep -qx "$candidate"; then
            export LC_ALL="$candidate"
            return 0
        fi
    done
    return 1
}

# 표시 폭(한글·한자·전각 2칸, 나머지 1칸)을 ui_width_result에 담는다. 하위 셸을 만들지 않아 화면 갱신이 빠르다.
# bash 4 이상은 문자 코드를, macOS 기본 bash 3.2는 UTF-8 첫 바이트를 돌려주므로 둘 다 판별한다.
# 상자·막대·상태 기호(━ █ ✔ ⚠ 등)는 1칸으로 센다(대부분 터미널 기본값).
ui_measure() {
    local text="$1" i code width=0
    for ((i = 0; i < ${#text}; i++)); do
        printf -v code '%d' "'${text:i:1}"
        if ((code > 255)); then
            if ((code >= 0x1100 && code <= 0x115F || code >= 0x2E80 && code <= 0xD7A3 \
                || code >= 0xF900 && code <= 0xFAFF || code >= 0xFF00 && code <= 0xFF60 \
                || code >= 0xFFE0 && code <= 0xFFE6 || code >= 0x1F300)); then
                width=$((width + 2))
            else
                width=$((width + 1))
            fi
        else
            ((code >= 0)) || code=$((code + 256))
            if ((code >= 0xE3 && code <= 0xED || code >= 0xF0)); then
                width=$((width + 2))
            else
                width=$((width + 1))
            fi
        fi
    done
    ui_width_result=$width
}

# 문자열을 정확히 width칸으로 맞춰 ui_cell_result에 담는다. 넘치면 잘라 …를 붙이고 모자라면 공백으로 채운다.
ui_cell() {
    local text="$1" width="$2" i code char_width used=0 fitted=''
    ui_measure "$text"
    if ((ui_width_result <= width)); then
        printf -v ui_cell_result '%s%*s' "$text" $((width - ui_width_result)) ''
        return
    fi
    for ((i = 0; i < ${#text}; i++)); do
        ui_measure "${text:i:1}"
        char_width=$ui_width_result
        if ((used + char_width > width - 1)); then
            break
        fi
        fitted+="${text:i:1}"
        used=$((used + char_width))
    done
    printf -v ui_cell_result '%s…%*s' "$fitted" $((width - used - 1)) ''
}

# 한국어 요일을 포함한 현재 시각. 예: 10/07(화) 14:32
ui_now() {
    local weekdays=(월 화 수 목 금 토 일) day
    day="$(TZ=Asia/Seoul date '+%u')"
    printf '%s(%s) %s' "$(TZ=Asia/Seoul date '+%m/%d')" "${weekdays[day - 1]}" \
        "$(TZ=Asia/Seoul date "+${1:-%H:%M}")"
}

# Python 화면(색 없는 출력)에 같은 기호 규칙으로 색을 입힌다. 터미널이 아니면 그대로 통과시킨다.
ui_paint() {
    if [[ -z "$ui_reset" ]]; then
        cat
        return
    fi
    # 막대는 같은 줄의 상태 기호 색을 따른다. 기호 자체의 색 입히기보다 먼저 처리한다.
    sed -e "/✔/s/\(█\)\{1,\}/${ui_green}&${ui_reset}/" \
        -e "/✖/s/\(█\)\{1,\}/${ui_red}&${ui_reset}/" \
        -e "/⚠/s/\(█\)\{1,\}/${ui_yellow}&${ui_reset}/" \
        -e "/↻/s/\(█\)\{1,\}/${ui_blue}&${ui_reset}/" \
        -e "/^[[:space:]]*•[[:space:]]*[0-9][0-9]\/[0-9][0-9](/s/\(█\)\{1,\}/${ui_cyan}&${ui_reset}/" \
        -e "s/\(░\)\{1,\}/${ui_dim}&${ui_reset}/g" \
        -e "s/■.*/${ui_bold}&${ui_reset}/" \
        -e "s/^\([[:space:]]*\)\(┆.*\)$/\1${ui_dim}\2${ui_reset}/" \
        -e "s/^[━─][━─]*$/${ui_dim}&${ui_reset}/" \
        -e "s/✔/${ui_green}✔${ui_reset}/g" \
        -e "s/✖/${ui_red}✖${ui_reset}/g" \
        -e "s/⚠/${ui_yellow}⚠${ui_reset}/g" \
        -e "s/↻/${ui_blue}↻${ui_reset}/g" \
        -e "s/›/${ui_cyan}›${ui_reset}/g"
}
