#!/usr/bin/env bash
# 운영 bot은 정적 Bot Token을 사용하므로 Bolt의 자동 OAuth 감지를 끈다.
set -euo pipefail

unset SLACK_CLIENT_ID SLACK_CLIENT_SECRET
exec "$@"
