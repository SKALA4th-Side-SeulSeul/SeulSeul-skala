#!/usr/bin/env bash
# 운영 계정의 systemd 사용자 타이머로 매일 백업한다.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scripts/scheduling.sh"
schedule_arguments "$@"
require_scheduler
require_operations
if [[ "$(loginctl show-user "$(id -u)" --property=Linger --value)" != yes ]]; then
    echo "로그아웃·재부팅 후 실행하려면 관리자가 먼저 실행하세요: sudo loginctl enable-linger $(id -un)" >&2
    exit 1
fi

umask 077
mkdir -p "$schedule_dir"
check_schedule_files
# systemd 인용 규칙: 경로의 공백·따옴표·specifier 및 ExecStart 변수 확장 방어.
unit_quote() {
    local value="$1"
    if [[ "$value" == *$'\n'* || "$value" == *$'\r'* ]]; then
        echo '개행이 포함된 경로는 지원하지 않습니다.' >&2
        return 1
    fi
    value="${value//\\/\\\\}"
    value="${value//\"/\\\"}"
    value="${value//%/%%}"
    printf '"%s"' "$value"
}
exec_path="${ops_root//\$/\$\$}/backup.sh"
exec_path="$(unit_quote "$exec_path")"
docker_path="$(dirname "$(command -v docker)"):/usr/local/bin:/usr/bin:/bin"
docker_path="$(unit_quote "PATH=$docker_path")"
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
    echo "ExecStart=/bin/bash $exec_path"
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
systemctl --user daemon-reload
systemctl --user enable seulseul-db-backup.timer
systemctl --user restart seulseul-db-backup.timer
echo '자동 백업 등록 완료: 매일 03:00 (Asia/Seoul). 누락된 예약은 서버 복귀 시 한 번 실행합니다.'
systemctl --user list-timers --all seulseul-db-backup.timer --no-pager
