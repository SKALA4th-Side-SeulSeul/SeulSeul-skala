#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scripts/operations.sh"

usage() {
    cat <<'USAGE'
사용법: ./view.sh [status|logs [서비스] [옵션]|db|schema|config|--help]
  status(기본): 컨테이너 상태 / logs: 최근 로그 조회
  서비스: bot(기본), oauth, ngrok, postgres, all
  logs 옵션: --follow(-f) 실시간 추적, --tail N 최근 N줄, --raw 원본 출력
  db: 읽기 전용 psql 접속 (종료: \q) / schema: 테이블 목록
  config: 값 출력 없이 Compose 설정 검증
  기본 logs는 최근 로그만 출력하고 종료합니다. 실시간 로그는 --follow를 붙이세요.
  로그 공유 전 토큰·개인정보를 가리세요. Ctrl+C는 로그 보기만 종료합니다.
USAGE
}

fail_usage() {
    usage
    exit 2
}

operation="${1:-status}"
log_service="bot"
log_tail=100
log_follow=false
log_raw=false

case "$operation" in
    --help|-h)
        [[ $# -eq 1 ]] || fail_usage
        usage
        exit 0
        ;;
    status|db|schema|config)
        [[ $# -eq 1 ]] || fail_usage
        ;;
    logs)
        shift
        service_set=false
        while [[ $# -gt 0 ]]; do
            case "$1" in
                bot|oauth|ngrok|postgres|all)
                    [[ "$service_set" == false ]] || fail_usage
                    log_service="$1"
                    service_set=true
                    shift
                    ;;
                --follow|-f)
                    [[ "$log_follow" == false ]] || fail_usage
                    log_follow=true
                    shift
                    ;;
                --raw)
                    [[ "$log_raw" == false ]] || fail_usage
                    log_raw=true
                    shift
                    ;;
                --tail)
                    [[ $# -ge 2 ]] || fail_usage
                    [[ "$2" =~ ^[1-9][0-9]*$ ]] || fail_usage
                    log_tail="$2"
                    shift 2
                    ;;
                --tail=*)
                    log_tail="${1#--tail=}"
                    [[ "$log_tail" =~ ^[1-9][0-9]*$ ]] || fail_usage
                    shift
                    ;;
                *)
                    fail_usage
                    ;;
            esac
        done
        ;;
*)
        fail_usage
        ;;
esac

require_operations

format_logs() {
    local service="$1"
    local tail="$2"
    local follow="$3"
    local raw="$4"
    local mode='최근 로그'
    local -a services=("$service")
    local -a log_args=(logs --no-color --tail "$tail")

    if [[ "$service" == all ]]; then
        services=(bot oauth ngrok postgres)
    fi
    if [[ "$follow" == true ]]; then
        log_args+=(-f)
        mode+=' + 실시간 추적'
    fi

    printf 'SeulSeul 운영 로그 · 대상: %s · %s\n' "$service" "$mode"
    if [[ "$raw" == true ]]; then
        echo '표시 형식: Docker 원본 로그 (--raw)'
        compose "${log_args[@]}" "${services[@]}"
        return
    fi

    echo '표시 형식: [INFO] 정상 · [WARN] 확인 필요 · [ERROR] 조치 필요'
    if [[ "$follow" == true ]]; then
        echo '종료: Ctrl+C (서비스는 중지하지 않음)'
    fi

    local use_color=0
    if [[ -t 1 ]]; then
        use_color=1
    fi
    compose "${log_args[@]}" "${services[@]}" |
        awk -v use_color="$use_color" '
        function json_value(line, key, marker, position, value) {
            marker = "\"" key "\":"
            position = index(line, marker)
            if (!position) {
                return ""
            }
            value = substr(line, position + length(marker))
            sub(/^[[:space:]]*/, "", value)
            if (substr(value, 1, 1) == "\"") {
                value = substr(value, 2)
                sub(/\".*/, "", value)
            } else {
                sub(/[,}].*/, "", value)
            }
            return value
        }

        {
            line = $0
            service = ""
            separator = index(line, "|")
            if (separator > 0 && separator < 120) {
                service = substr(line, 1, separator - 1)
                gsub(/^[[:space:]]+|[[:space:]]+$/, "", service)
                line = substr(line, separator + 1)
                sub(/^[[:space:]]+/, "", line)
            }

            lower = tolower(line)
            label = "INFO"
            if (lower ~ /(^|[[:space:]])(error|critical|fatal)(:|[[:space:]]|$)/) {
                label = "ERROR"
            } else if (lower ~ /(^|[[:space:]])(warning|warn)(:|[[:space:]]|$)/) {
                label = "WARN"
            } else if (lower ~ /(^|[[:space:]])debug(:|[[:space:]]|$)/) {
                label = "DEBUG"
            }

            event = json_value(line, "event")
            if (event != "") {
                summary = "event=" event
                count = split("operation result reason code saved dm_synchronized trace_id", keys, " ")
                for (i = 1; i <= count; i++) {
                    value = json_value(line, keys[i])
                    if (value != "" && value != "null") {
                        key = keys[i]
                        if (key == "trace_id") {
                            key = "trace"
                        }
                        summary = summary " " key "=" value
                    }
                }
                json_start = index(line, "{")
                if (json_start > 0) {
                    prefix = substr(line, 1, json_start - 1)
                    sub(/[[:space:]]+$/, "", prefix)
                    line = prefix " · " summary
                } else {
                    line = summary
                }
            }

            if (service == "") {
                service = "app"
            }
            color = ""
            reset = ""
            if (use_color) {
                reset = "\033[0m"
                if (label == "ERROR") {
                    color = "\033[31;1m"
                } else if (label == "WARN") {
                    color = "\033[33;1m"
                } else if (label == "DEBUG") {
                    color = "\033[2m"
                } else {
                    color = "\033[32m"
                }
            }
            printf "%s[%s]%s %-24s %s\n", color, label, reset, service, line
            fflush()
        }
        '
}

case "$operation" in
    status)
        compose ps -a
        ;;
    logs)
        format_logs "$log_service" "$log_tail" "$log_follow" "$log_raw"
        ;;
    db)
        compose exec -e PGOPTIONS='-c default_transaction_read_only=on' postgres \
            sh -c 'exec psql -X -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
        ;;
    schema)
        compose exec -T -e PGOPTIONS='-c default_transaction_read_only=on' postgres \
            sh -c 'exec psql -X -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "\dt"'
        ;;
    config)
        echo 'Compose 설정 검증 통과 (DB 연결·AI 키 유효성 검증은 아님).'
        ;;
esac
