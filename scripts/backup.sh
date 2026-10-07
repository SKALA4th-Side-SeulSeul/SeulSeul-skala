#!/usr/bin/env bash
# 운영 DB의 일관된 논리 백업과 매일 자동 백업 예약 관리. ./admin.sh backup 으로 실행한다.
# 기존 백업과 DB는 삭제하지 않는다.
# 등록된 systemd 서비스가 인자 없이 이 파일을 실행하므로, 인자 없는 실행은 항상 즉시 백업이어야 한다.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/operations.sh"

usage() {
    ui_banner '슬슬 DB 백업' '사용법'
    ui_section '이렇게 실행하세요'
    ui_usage_row './admin.sh backup' '지금 운영 DB 백업 (봇과 DB는 계속 실행)'
    ui_usage_row './admin.sh backup status' '자동 백업 예약과 최근 백업 확인'
    ui_usage_row './admin.sh backup schedule' '매일 한국 시간 03:00 자동 백업 켜기·다시 등록'
    ui_usage_row './admin.sh backup unschedule' '자동 백업 끄기 (실행 중인 백업·기존 파일 유지)'
    ui_section '저장 위치와 안전 장치'
    ui_info '저장소 상위의 backups/ (~/app 기준 ~/backups), 디렉터리 700·파일 600'
    ui_info '덤프 후 아카이브 전체를 해독해 손상 여부를 확인하고 SHA-256을 남깁니다.'
    ui_info '기존 백업 자동 삭제·DB 중지·마이그레이션·복구는 하지 않습니다.'
}

run_backup() {
    require_operations
    umask 077
    if [[ -L "$backup_root" || ( -e "$backup_root" && ! -d "$backup_root" ) ]]; then
        ui_fail 'backups는 심볼릭 링크가 아닌 실제 디렉터리여야 합니다.'
        exit 1
    fi
    mkdir -p "$backup_root"
    if [[ ! -O "$backup_root" ]]; then
        ui_fail 'backups 디렉터리가 현재 사용자 소유가 아닙니다.'
        exit 1
    fi
    chmod 700 "$backup_root"
    backup_dir="$(mktemp -d "$backup_root/seulseul-$(date -u +%Y%m%dT%H%M%SZ)-XXXXXX")"
    partial_file="$backup_dir/database.dump.partial"
    archive_file="$backup_dir/database.dump"
    # EXIT trap이 함수 반환 뒤에도 읽으므로 local로 선언하지 않는다.
    completed=false
    trap cleanup_backup EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM

    ui_banner '슬슬 DB 백업' '지금 백업 · 봇과 DB는 계속 실행됩니다'
    ui_step 1 3 'DB 덤프 만들기'
    compose exec -T postgres sh -c \
        'exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom' > "$partial_file"
    if [[ ! -s "$partial_file" ]]; then
        ui_fail '백업 파일이 비어 있습니다.'
        exit 1
    fi
    ui_step 2 3 '아카이브 손상 여부 확인'
    # SQL을 실행하지 않고 아카이브 전체를 SQL로 해독해 손상·압축 오류를 확인한다.
    compose exec -T postgres pg_restore --file=/dev/null < "$partial_file"
    mv -- "$partial_file" "$archive_file"
    ui_step 3 3 'SHA-256 체크섬 기록'
    (
        cd "$backup_dir"
        if command -v sha256sum >/dev/null 2>&1; then
            sha256sum database.dump > SHA256SUMS
        elif command -v shasum >/dev/null 2>&1; then
            shasum -a 256 database.dump > SHA256SUMS
        else
            ui_fail 'SHA-256 도구(sha256sum 또는 shasum)가 필요합니다.'
            exit 1
        fi
    )
    completed=true
    echo
    ui_ok "백업 완료: $archive_file"
    ui_info "체크섬: $backup_dir/SHA256SUMS"
    ui_footer '실제 복구 시험은 별도 격리 DB에서 수행해야 합니다.' \
        '덤프에는 개인정보가 있습니다. 서버 밖 접근 제한된 곳에도 안전하게 복사하세요.'
}

cleanup_backup() {
    if [[ "$completed" != true ]]; then
        # 이번 실행에서 생성한 정확한 파일만 제거한다. 이전 백업은 건드리지 않는다.
        rm -f -- "$partial_file" "$archive_file" "$backup_dir/SHA256SUMS"
        rmdir -- "$backup_dir" || true
        ui_fail '백업 실패: 불완전한 파일을 정리했습니다. 기존 백업과 DB는 그대로입니다.'
    fi
}

require_scheduler() {
    command -v systemctl >/dev/null || { ui_fail 'Ubuntu systemd 사용자 세션이 필요합니다.'; exit 1; }
    systemctl --user show-environment >/dev/null
}

check_schedule_files() {
    local unit path first_line
    for unit in service timer; do
        path="$schedule_dir/seulseul-db-backup.$unit"
        if [[ -L "$path" || ( -e "$path" && ! -f "$path" ) ]]; then
            ui_fail "안전하지 않은 예약 파일: $path"
            exit 1
        fi
        if [[ -f "$path" ]]; then
            first_line=''
            IFS= read -r first_line < "$path" || true
            if [[ "$first_line" != "$schedule_marker" ]]; then
                ui_fail "직접 작성된 예약 파일은 덮어쓰거나 중지하지 않습니다: $path"
                exit 1
            fi
        fi
    done
}

install_schedule() {
    require_scheduler
    require_operations
    if [[ "$(loginctl show-user "$(id -u)" --property=Linger --value)" != yes ]]; then
        ui_fail '로그아웃·재부팅 후에도 실행하려면 사용자 lingering이 필요합니다.'
        ui_hint "관리자 계정에서 먼저 실행하세요: sudo loginctl enable-linger $(id -un)" >&2
        exit 1
    fi

    umask 077
    mkdir -p "$schedule_dir"
    check_schedule_files
    local exec_line docker_path unit
    exec_line="$(expected_backup_exec)"
    docker_path="$(dirname "$(command -v docker)"):/usr/local/bin:/usr/bin:/bin"
    docker_path="$(unit_quote "PATH=$docker_path")"
    ui_banner '슬슬 자동 백업' '매일 03:00 (Asia/Seoul) 예약'
    ui_step 1 2 '예약 파일 작성'
    # EXIT trap이 함수 반환 뒤에도 읽으므로 local로 선언하지 않는다.
    schedule_stage="$(mktemp -d "$schedule_dir/.seulseul-schedule-XXXXXX")"
    trap 'rm -f -- "$schedule_stage/seulseul-db-backup.service" "$schedule_stage/seulseul-db-backup.timer"; rmdir -- "$schedule_stage"' EXIT
    {
        echo "$schedule_marker"
        echo '[Unit]'
        echo 'Description=SeulSeul PostgreSQL backup'
        echo '[Service]'
        echo 'Type=oneshot'
        echo 'UMask=0077'
        echo 'Environment="DOCKER_HOST=unix://%t/docker.sock"'
        echo 'Environment="DOCKER_CONTEXT="'
        echo "Environment=$docker_path"
        echo "$exec_line"
        # 실행이 멈춘 백업이 다음 예약까지 서비스를 점유하지 않도록 제한한다.
        echo 'TimeoutStartSec=30min'
        echo 'TimeoutStopSec=30s'
    } > "$schedule_stage/seulseul-db-backup.service"
    {
        echo "$schedule_marker"
        echo '[Unit]'
        echo 'Description=SeulSeul daily backup at 03:00 Asia/Seoul'
        echo '[Timer]'
        echo 'OnCalendar=*-*-* 03:00:00 Asia/Seoul'
        echo 'Persistent=true'
        echo 'Unit=seulseul-db-backup.service'
        echo '[Install]'
        echo 'WantedBy=timers.target'
    } > "$schedule_stage/seulseul-db-backup.timer"
    for unit in service timer; do
        mv -- "$schedule_stage/seulseul-db-backup.$unit" "$schedule_dir/seulseul-db-backup.$unit"
    done
    ui_step 2 2 'systemd 사용자 타이머 등록'
    systemctl --user daemon-reload
    systemctl --user enable seulseul-db-backup.timer
    systemctl --user restart seulseul-db-backup.timer
    echo
    ui_ok '자동 백업 등록 완료: 매일 03:00 (Asia/Seoul). 서버가 꺼져 놓친 예약은 켜질 때 한 번 실행합니다.'
    show_backup_status
}

# 예약만 해제한다. 이미 실행 중인 백업·기존 파일·DB는 유지하며 운영 .env 없이도 동작한다.
remove_schedule() {
    require_scheduler
    check_schedule_files
    ui_banner '슬슬 자동 백업' '예약 해제'
    echo
    if [[ ! -f "$schedule_dir/seulseul-db-backup.timer" ]]; then
        ui_info '등록된 자동 백업이 없습니다.'
        return
    fi
    systemctl --user disable --now seulseul-db-backup.timer
    ui_ok '자동 백업 예약을 해제했습니다. 실행 중인 백업과 기존 백업 파일·DB는 그대로입니다.'
    ui_footer '다시 켜기: ./admin.sh backup schedule'
}

# 조회만 한다. 예약 상태와 마지막으로 성공한 백업을 한 화면에 보여 준다.
show_backup_status() {
    local size
    ui_section '자동 백업 예약'
    ui_info '매일 새벽 3시(한국 시간)에 운영 DB를 자동으로 백업하는 예약입니다.'
    read_backup_schedule
    case "$schedule_state" in
        unavailable) ui_info 'systemd를 쓸 수 없는 환경이라 예약 상태를 확인할 수 없습니다.' ;;
        none)
            ui_warn '꺼짐 · 등록된 자동 백업이 없습니다.'
            ui_hint '켜기: ./admin.sh backup schedule'
            ;;
        on) ui_ok "켜짐 · 매일 03:00 (Asia/Seoul)${schedule_next:+ · 다음 실행 $schedule_next}" ;;
        off)
            ui_warn '꺼짐 · 예약 파일은 있지만 비활성화되어 있습니다.'
            ui_hint '다시 켜기: ./admin.sh backup schedule'
            ;;
        stale)
            ui_fail '예약이 이 저장소의 scripts/backup.sh가 아닌 경로를 실행해 예약 백업이 실패합니다.'
            ui_hint '다시 등록: ./admin.sh backup schedule'
            ;;
    esac

    ui_section '최근 백업'
    read_latest_backup
    if [[ -z "$latest_backup" ]]; then
        ui_warn '아직 성공한 백업이 없습니다.'
        ui_hint '지금 백업: ./admin.sh backup'
        return
    fi
    size="$(du -h "$latest_backup" | cut -f1)"
    ui_ok "마지막 성공 ${latest_backup_time} (한국 시간) · 크기 ${size} · 보관 ${latest_backup_count}개"
    ui_info "위치: $latest_backup"
    ui_info '오래된 백업은 자동 삭제하지 않습니다. 디스크 용량을 가끔 확인하세요.'
}

[[ $# -le 1 ]] || { usage >&2; exit 2; }
case "${1:-}" in
    "") run_backup ;;
    status)
        ui_banner '슬슬 DB 백업 상태' "$(TZ=Asia/Seoul date '+%m/%d %H:%M') 기준"
        show_backup_status
        ;;
    schedule) install_schedule ;;
    unschedule) remove_schedule ;;
    --help | -h) usage ;;
    *)
        usage >&2
        exit 2
        ;;
esac
