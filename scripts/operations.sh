#!/usr/bin/env bash
# 운영 Compose 공통 경계. .env를 shell source하거나 출력하지 않는다.
set -euo pipefail

ops_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ops_root"
source "$ops_root/scripts/ui.sh"

# 자동 백업 예약 위치와 표식. 서버에 이미 설치된 예약 파일을 식별하므로 표식 문구를 바꾸지 않는다.
schedule_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
schedule_marker='# Managed by SeulSeul backup scheduling scripts'
backup_root="$(dirname "$ops_root")/backups"
# 수동 공지 처리 기록. 일회성 컨테이너는 끝나면 로그가 사라지므로 서버 파일에 남긴다.
# 저장소 밖(백업과 같은 상위 폴더)에 두어 Git에 올라가지 않게 한다.
manual_log_dir="$(dirname "$ops_root")/logs"
manual_log_file="$manual_log_dir/manual-notice.log"

# 기록 파일을 현재 사용자만 읽을 수 있게 준비한다. 심볼릭 링크로 다른 파일을 덮지 않는다.
prepare_manual_log() {
    if [[ -L "$manual_log_dir" || ( -e "$manual_log_dir" && ! -d "$manual_log_dir" ) ]]; then
        ui_fail "기록 폴더가 실제 디렉터리가 아닙니다: $manual_log_dir"
        exit 1
    fi
    if [[ -L "$manual_log_file" ]]; then
        ui_fail "기록 파일이 심볼릭 링크입니다: $manual_log_file"
        exit 1
    fi
    (umask 077 && mkdir -p "$manual_log_dir" && touch "$manual_log_file")
    chmod 700 "$manual_log_dir"
    chmod 600 "$manual_log_file"
}

# systemd 인용 규칙: 경로의 공백·따옴표·specifier 및 ExecStart 변수 확장 방어.
unit_quote() {
    local value="$1"
    if [[ "$value" == *$'\n'* || "$value" == *$'\r'* ]]; then
        ui_fail '개행이 포함된 경로는 지원하지 않습니다.'
        return 1
    fi
    value="${value//\\/\\\\}"
    value="${value//\"/\\\"}"
    value="${value//%/%%}"
    printf '"%s"' "$value"
}

# 이 저장소의 예약 백업 서비스가 가져야 할 ExecStart 줄. 등록과 상태 확인이 같은 값을 쓴다.
expected_backup_exec() {
    local exec_path
    exec_path="$(unit_quote "${ops_root//\$/\$\$}/scripts/backup.sh")" || return 1
    printf 'ExecStart=/bin/bash %s' "$exec_path"
}

# 스크립트가 만든 예약이 이 저장소의 scripts/backup.sh가 아닌 경로를 실행하면 성공한다.
# D-041 이전의 루트 backup.sh 예약과 저장소를 옮긴 뒤 남은 예약이 여기에 해당한다.
backup_schedule_is_stale() {
    local service="$schedule_dir/seulseul-db-backup.service" first_line='' expected
    if [[ ! -f "$service" || -L "$service" ]]; then
        return 1
    fi
    IFS= read -r first_line < "$service" || true
    [[ "$first_line" == "$schedule_marker" ]] || return 1
    expected="$(expected_backup_exec)" || return 1
    ! grep -qxF "$expected" "$service"
}

# 자동 백업 예약 상태를 schedule_state(on·off·stale·none·unavailable)와 schedule_next에 담는다. 조회만 한다.
# stale은 예약이 다른 경로를 실행해 켜져 있어도 백업이 실패하는 상태다.
read_backup_schedule() {
    schedule_next=''
    if ! command -v systemctl >/dev/null; then
        schedule_state=unavailable
    elif [[ ! -f "$schedule_dir/seulseul-db-backup.timer" ]]; then
        schedule_state=none
    elif backup_schedule_is_stale; then
        schedule_state=stale
    elif [[ "$(systemctl --user is-enabled seulseul-db-backup.timer 2>/dev/null || true)" == enabled ]]; then
        schedule_state=on
        schedule_next="$(systemctl --user show seulseul-db-backup.timer \
            --property=NextElapseUSecRealtime --value 2>/dev/null || true)"
    else
        schedule_state=off
    fi
}

# 마지막으로 성공한 백업을 latest_backup·latest_backup_time·latest_backup_count에 담는다.
# 디렉터리 이름이 UTC 시각으로 시작하므로 이름순 정렬이 곧 시간순이다.
# 조회용이므로 읽을 수 없는 하위 폴더가 있어도 중단하지 않는다.
read_latest_backup() {
    local dumps=''
    latest_backup=''
    latest_backup_time=''
    latest_backup_count=0
    if [[ -d "$backup_root" && ! -L "$backup_root" ]]; then
        dumps="$(find "$backup_root" -mindepth 2 -maxdepth 2 -type f -name database.dump 2>/dev/null | sort || true)"
    fi
    if [[ -n "$dumps" ]]; then
        latest_backup="$(tail -n 1 <<< "$dumps")"
        latest_backup_count="$(wc -l <<< "$dumps" | tr -d ' ')"
        latest_backup_time="$(TZ=Asia/Seoul date -r "$latest_backup" '+%m/%d %H:%M')"
    fi
}

compose() {
    docker compose --project-directory "$ops_root" --env-file "$ops_root/.env" \
        -f "$ops_root/compose.prod.yaml" "$@"
}

require_operations() {
    if [[ ! -f .env ]]; then
        ui_fail '운영 설정 파일(.env)이 없습니다.'
        ui_hint '처음이라면 ./run.sh setup 으로 템플릿을 만든 뒤 nano .env 로 값을 입력하세요.' >&2
        exit 1
    fi
    command -v docker >/dev/null || { ui_fail 'Docker가 필요합니다.'; exit 1; }
    docker compose version >/dev/null
    local security
    security="$(docker info --format '{{json .SecurityOptions}}')"
    if [[ "$security" != *'name=rootless'* ]]; then
        ui_fail '운영 정책상 Rootless Docker가 필요합니다.'
        ui_hint 'seulseul 계정으로 직접 로그인해 다시 실행하세요.' >&2
        exit 1
    fi
    compose config --quiet
    warn_stale_backup_schedule
}

# 다른 경로를 실행하는 예약은 재등록할 때까지 운영 명령마다 알린다. 직접 작성한 예약 파일은 판단하지 않는다.
warn_stale_backup_schedule() {
    if backup_schedule_is_stale; then
        ui_warn '자동 백업 예약이 이 저장소의 scripts/backup.sh가 아닌 경로를 실행해 예약 백업이 실패합니다.' >&2
        ui_hint '한 번만 다시 등록하세요: ./admin.sh backup schedule' >&2
    fi
}

# 컨테이너 상태를 사람이 읽는 한 줄 요약으로 보여 준다.
# 형식 지정을 지원하지 않는 Compose라면 원본 표로 대신한다.
show_services() {
    local rows service state status mark label role
    if ! rows="$(compose ps -a --format '{{.Service}}|{{.State}}|{{.Status}}' 2>/dev/null)"; then
        compose ps -a
        return
    fi
    if [[ -z "$rows" ]]; then
        ui_info '만들어진 서비스 컨테이너가 없습니다. 시작하려면 ./run.sh 를 실행하세요.'
        return
    fi
    while IFS='|' read -r service state status; do
        case "$service" in
            bot) role='Slack 봇' ;;
            oauth) role='Slack 설치(OAuth) 서버' ;;
            ngrok) role='외부 접속 터널' ;;
            postgres) role='데이터베이스' ;;
            migrate) role='DB 마이그레이션' ;;
            *) role='' ;;
        esac
        case "$state" in
            running) mark="${ui_green}●${ui_reset}" label='실행 중' ;;
            restarting) mark="${ui_yellow}●${ui_reset}" label='재시작 반복 중' ;;
            paused) mark="${ui_yellow}●${ui_reset}" label='일시 정지' ;;
            exited | dead) mark="${ui_dim}○${ui_reset}" label='중지됨' ;;
            created) mark="${ui_dim}○${ui_reset}" label='만들어졌지만 시작 안 함' ;;
            *) mark='•' label="$state" ;;
        esac
        printf '  %s %s%-9s%s %s%s %s· %s%s\n' "$mark" "$ui_bold" "$service" "$ui_reset" \
            "$label" "${role:+ · $role}" "$ui_dim" "$status" "$ui_reset"
    done <<< "$rows"
}
