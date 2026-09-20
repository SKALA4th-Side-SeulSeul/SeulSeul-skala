#!/usr/bin/env bash
# 운영 DB의 일관된 논리 백업. 기존 백업과 DB는 삭제하지 않는다.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scripts/operations.sh"

usage() {
    echo '사용법: ./backup.sh [--help]'
    echo '실행 중인 운영 PostgreSQL을 custom-format pg_dump로 백업합니다.'
    echo '저장 위치: 저장소 상위의 backups/ (~/app 기준 ~/backups)'
    echo '기존 백업 자동 삭제·DB 중지·마이그레이션·복구는 수행하지 않습니다.'
}
if [[ $# -gt 0 ]]; then
    if [[ $# -eq 1 && ( "$1" == --help || "$1" == -h ) ]]; then
        usage
        exit 0
    fi
    usage >&2
    exit 2
fi
require_operations

umask 077
backup_root="$(dirname "$ops_root")/backups"
if [[ -L "$backup_root" || ( -e "$backup_root" && ! -d "$backup_root" ) ]]; then
    echo 'backups는 심볼릭 링크가 아닌 실제 디렉터리여야 합니다.' >&2
    exit 1
fi
mkdir -p "$backup_root"
if [[ ! -O "$backup_root" ]]; then
    echo 'backups 디렉터리가 현재 사용자 소유가 아닙니다.' >&2
    exit 1
fi
chmod 700 "$backup_root"
backup_dir="$(mktemp -d "$backup_root/seulseul-$(date -u +%Y%m%dT%H%M%SZ)-XXXXXX")"
partial_file="$backup_dir/database.dump.partial"
archive_file="$backup_dir/database.dump"
completed=false
cleanup() {
    if [[ "$completed" != true ]]; then
        # 이번 실행에서 생성한 정확한 파일만 제거한다. 이전 백업은 건드리지 않는다.
        rm -f -- "$partial_file" "$archive_file" "$backup_dir/SHA256SUMS"
        rmdir -- "$backup_dir" || true
        echo '백업 실패: 불완전한 파일을 정리했습니다. 기존 백업과 DB는 유지됩니다.' >&2
    fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

echo '운영 DB 백업 중… (봇과 DB는 계속 실행됩니다)'
compose exec -T postgres sh -c \
    'exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom' > "$partial_file"
if [[ ! -s "$partial_file" ]]; then
    echo '백업 파일이 비어 있습니다.' >&2
    exit 1
fi
# SQL을 실행하지 않고 아카이브 전체를 SQL로 해독해 손상·압축 오류를 확인한다.
compose exec -T postgres pg_restore --file=/dev/null < "$partial_file"
mv -- "$partial_file" "$archive_file"
(
    cd "$backup_dir"
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum database.dump > SHA256SUMS
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 database.dump > SHA256SUMS
    else
        echo 'SHA-256 도구(sha256sum 또는 shasum)가 필요합니다.' >&2
        exit 1
    fi
)
completed=true
echo "백업 완료: $archive_file"
echo "체크섬: $backup_dir/SHA256SUMS"
echo '실제 복구 시험은 별도 격리 DB에서 수행해야 합니다. 서버 밖에도 안전하게 복사하세요.'
