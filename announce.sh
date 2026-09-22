#!/usr/bin/env bash
# 현재 가입 학생에게 운영 안내 DM을 수동 발송한다.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scripts/operations.sh"
if [[ "${1:-}" == --help || "${1:-}" == -h ]]; then
    echo '사용법: ./announce.sh'
    echo '안내문 입력(빈 줄에서 Ctrl+D로 완료) → 미리보기 → y 입력 시 발송'
    echo '줄바꿈: Enter · 취소: Ctrl+C'
    echo '운영 Compose·Rootless Docker 전용. 실행 중인 DB와 빌드된 bot 이미지가 필요합니다.'
    exit 0
fi
require_operations
# Ctrl+D 이후에도 확인 입력을 받을 수 있도록 파이프가 아닌 터미널을 연결한다.
compose run --rm --no-deps --interactive --tty bot python -m seulseul.users.announce "$@"
