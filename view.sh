#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scripts/operations.sh"

usage() {
    cat <<'USAGE'
사용법: ./view.sh [status|dashboard [--watch]|logs [서비스] [옵션]|db|schema|config|--help]
  status(기본): 컨테이너 상태 / dashboard: 공지 처리 현황·수동 조치 목록
  dashboard 옵션: --watch(-w) 5초마다 갱신
  logs: 최근 로그 조회
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
dashboard_watch=false

case "$operation" in
    --help|-h)
        [[ $# -eq 1 ]] || fail_usage
        usage
        exit 0
        ;;
    status|db|schema|config)
        [[ $# -eq 1 ]] || fail_usage
        ;;
    dashboard)
        shift
        while [[ $# -gt 0 ]]; do
            case "$1" in
                --watch|-w)
                    [[ "$dashboard_watch" == false ]] || fail_usage
                    dashboard_watch=true
                    shift
                    ;;
                *)
                    fail_usage
                    ;;
            esac
        done
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

    echo '표시 형식: 시각 | 상태 | 주체 | 작업 결과'
    if [[ "$follow" == true ]]; then
        echo '종료: Ctrl+C (서비스는 중지하지 않음)'
    fi

    local use_color=0
    if [[ -t 1 ]]; then
        use_color=1
    fi
    compose "${log_args[@]}" "${services[@]}" |
        awk -v use_color="$use_color" '
        function trim(value) {
            gsub(/^[[:space:]]+|[[:space:]]+$/, "", value)
            return value
        }

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
            return trim(value)
        }

        function plain_value(line, key, marker, position, value) {
            marker = key "="
            position = index(line, marker)
            if (!position) {
                return ""
            }
            value = substr(line, position + length(marker))
            sub(/[[:space:]]+[a-z_]+=.*/, "", value)
            return trim(value)
        }

        function time_value(line) {
            if (match(line, /[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]/)) {
                return substr(line, RSTART + 5, 14)
            }
            return "--"
        }

        function level_value(line, lower) {
            lower = tolower(line)
            if (lower ~ /(^|[[:space:]])(error|critical|fatal)(:|[[:space:]]|$)/) {
                return "오류"
            }
            if (lower ~ /(^|[[:space:]])(warning|warn)(:|[[:space:]]|$)/) {
                return "주의"
            }
            if (lower ~ /(^|[[:space:]])debug(:|[[:space:]]|$)/) {
                return "디버그"
            }
            return "정상"
        }

        function body_value(line, rest, json_start) {
            rest = line
            if (match(rest, / (INFO|WARNING|ERROR|DEBUG) /)) {
                rest = substr(rest, RSTART + RLENGTH)
            }
            json_start = index(rest, "{")
            if (json_start > 0) {
                return substr(rest, json_start)
            }
            if (rest ~ /^[^:]+:[^:]+:[[:space:]]*/) {
                sub(/^[^:]+:[^:]+:[[:space:]]*/, "", rest)
            } else {
                sub(/^[^:]+:[[:space:]]*/, "", rest)
            }
            return trim(rest)
        }

        function action_value(operation) {
            if (operation == "refresh") return "체크리스트 새로고침"
            if (operation == "complete") return "공지 완료 처리"
            if (operation == "undo") return "공지 완료 취소"
            if (operation == "pending") return "미완료 목록 보기"
            if (operation == "completed") return "완료 목록 보기"
            if (operation == "previous") return "이전 페이지 보기"
            if (operation == "next") return "다음 페이지 보기"
            return "체크리스트 작업"
        }

        function code_value(code) {
            if (code == "missing_scope") return "Slack 권한 부족"
            if (code == "invalid_dm_response") return "개인 DM 주소 확인 실패"
            if (code == "slack_connection_error") return "Slack 연결 실패"
            if (code == "message_not_found") return "Slack 메시지 없음"
            if (code == "ratelimited") return "Slack 요청 제한"
            if (code == "invalid_delivery_response") return "Slack 발송 결과 확인 실패"
            return "원인 확인 필요"
        }

        function print_row(time, status, actor, summary, color, reset) {
            color = ""
            reset = ""
            if (use_color) {
                reset = "\033[0m"
                if (status == "오류") color = "\033[31;1m"
                else if (status == "주의") color = "\033[33;1m"
                else if (status == "무시") color = "\033[2m"
                else color = "\033[32m"
            }
            printf "%s%s | %s | %s | %s%s\n", color, time, status, actor, summary, reset
            fflush()
        }

        function flush_pending(trace, operation, actor, received_time) {
            for (trace in pending_operation) {
                operation = pending_operation[trace]
                actor = pending_actor[trace]
                received_time = pending_time[trace]
                print_row(received_time, "정상", actor, action_value(operation) " 요청 접수")
                delete pending_operation[trace]
                delete pending_actor[trace]
                delete pending_time[trace]
            }
        }

        {
            line = $0
            time = time_value(line)
            status = level_value(line)
            body = body_value(line)
            event = json_value(body, "event")

            if (event == "checklist_action_received") {
                trace = json_value(body, "trace_id")
                pending_operation[trace] = json_value(body, "operation")
                pending_actor[trace] = json_value(body, "actor_name")
                if (pending_actor[trace] == "") pending_actor[trace] = "사용자 확인 실패"
                pending_time[trace] = time
                next
            }
            if (event == "checklist_action_parsed") {
                next
            }
            if (event == "checklist_message_validation") {
                next
            }
            if (event == "checklist_action_result") {
                trace = json_value(body, "trace_id")
                actor = pending_actor[trace]
                if (actor == "") actor = json_value(body, "actor_name")
                if (actor == "") actor = "사용자 확인 실패"
                operation = pending_operation[trace]
                if (operation == "") operation = json_value(body, "operation")
                if (json_value(body, "saved") == "true" && json_value(body, "dm_synchronized") == "true") {
                    print_row(time, "정상", actor, action_value(operation) " 완료 · 개인 DM 갱신 완료")
                } else if (json_value(body, "saved") == "true") {
                    print_row(time, "주의", actor, action_value(operation) " 저장 완료 · 개인 DM 갱신 대기")
                } else {
                    print_row(time, "오류", actor, action_value(operation) " 저장 실패")
                }
                delete pending_operation[trace]
                delete pending_actor[trace]
                delete pending_time[trace]
                next
            }
            if (event == "checklist_action_error") {
                trace = json_value(body, "trace_id")
                actor = json_value(body, "actor_name")
                if (actor == "") actor = pending_actor[trace]
                if (actor == "") actor = "사용자 확인 실패"
                operation = json_value(body, "operation")
                if (operation == "") operation = pending_operation[trace]
                print_row(time, "오류", actor, action_value(operation) " 실패 · 운영자 확인 필요")
                delete pending_operation[trace]
                delete pending_actor[trace]
                delete pending_time[trace]
                next
            }
            if (event == "checklist_action_rejected") {
                trace = json_value(body, "trace_id")
                actor = json_value(body, "actor_name")
                if (actor == "") actor = pending_actor[trace]
                if (actor == "") actor = "사용자 확인 실패"
                operation = json_value(body, "operation")
                if (operation == "") operation = pending_operation[trace]
                print_row(time, "주의", actor, action_value(operation) " 요청 거부")
                delete pending_operation[trace]
                delete pending_actor[trace]
                delete pending_time[trace]
                next
            }
            if (event == "checklist_action_invalid_body") {
                actor = json_value(body, "actor_name")
                if (actor == "") actor = "사용자 확인 실패"
                print_row(time, "주의", actor, "버튼 요청 형식 오류")
                next
            }
            if (event ~ /^checklist_action_/) {
                next
            }
            if (event == "checklist_delivery_saved") {
                print_row(time, "정상", "봇", "체크리스트 개인 DM 발송 기록 저장")
                next
            }
            if (event == "checklist_delivery_save_conflict") {
                print_row(time, "오류", "봇", "체크리스트 발송 기록 저장 충돌")
                next
            }
            if (event != "") {
                print_row(time, status, "봇", "내부 처리 기록")
                next
            }

            if (body ~ /메시지 이벤트 제외:/) {
                print_row(time, "무시", "Slack", "수집 대상이 아닌 메시지를 무시했습니다")
                next
            }
            if (body ~ /메시지 이벤트 처리:/) {
                notices = plain_value(body, "notices")
                processed = plain_value(body, "processed")
                failed = plain_value(body, "failed")
                summary = "공지 메시지 처리 완료"
                if (notices != "") summary = summary " · 공지 " notices "개"
                if (processed != "") summary = summary " · 분석 성공 " processed "개"
                if (failed != "") summary = summary " · 실패 " failed "개"
                print_row(time, status, "봇", summary)
                next
            }
            if (body ~ /명령어 처리:/) {
                actor = plain_value(body, "actor")
                if (actor == "") actor = "사용자 확인 실패"
                operation = plain_value(body, "action")
                result = plain_value(body, "result")
                if (operation == "") operation = "알 수 없는 명령"
                if (result == "") result = "처리 완료"
                print_row(time, status, actor, "/seulseul " operation " · " result)
                next
            }
            if (body ~ /명령 DM 처리 실패:/) {
                actor = plain_value(body, "actor")
                if (actor == "") actor = "사용자 확인 실패"
                operation = plain_value(body, "action")
                code = plain_value(body, "code")
                print_row(time, "오류", actor, "/seulseul " operation " 실패 · " code_value(code))
                next
            }
            if (body ~ /슬래시 명령 처리 실패:/) {
                actor = plain_value(body, "actor")
                if (actor == "") actor = "사용자 확인 실패"
                operation = plain_value(body, "action")
                print_row(time, "오류", actor, "/seulseul " operation " 실패 · 운영자 확인 필요")
                next
            }
            if (body ~ /명령어 식별자 누락/) {
                print_row(time, "주의", "사용자 확인 실패", "슬래시 명령 정보 누락")
                next
            }
            if (body ~ /개인 DM 갱신 실패/) {
                code = plain_value(body, "code")
                print_row(time, "오류", "봇", "개인 체크리스트 DM 갱신 실패 · " code_value(code))
                next
            }
            if (body ~ /Socket Mode로 Slack에 연결합니다/) {
                notices = plain_value(body, "설정")
                print_row(time, "정상", "봇", "Slack 연결 시작 · 공지 채널 설정 " notices "개")
                next
            }
            if (body ~ /AI 요약 사용/) {
                print_row(time, "정상", "봇", "공지 AI 요약 기능 사용")
                next
            }
            if (body ~ /공지 원문 링크 조회 실패/) {
                print_row(time, "주의", "봇", "공지 원문 링크 조회 실패 · 재처리 필요")
                next
            }
            if (body ~ /공지 AI 분석 실패/) {
                print_row(time, "주의", "봇", "공지 AI 분석 실패 · 재처리 필요")
                next
            }
            if (body ~ /공지 자동 재처리 종료/) {
                print_row(time, "주의", "봇", "공지 자동 재처리 종료 · 운영자 확인 필요")
                next
            }
            if (body ~ /설정 오류/) {
                print_row(time, "오류", "봇", "설정 오류 · 환경변수 확인 필요")
                next
            }

            message = body
            gsub(/(channel|channel_id|ts|trace|trace_id|workspace|workspace_id|user|user_id|daily_id|item_id|delivery|pid|instance_id|url)=[^ ,}]+/, "", message)
            message = trim(message)
            if (message != "") {
                print_row(time, status, "시스템", message)
            }
        }

        END {
            flush_pending()
        }
        '
}

case "$operation" in
    dashboard)
        if [[ "$dashboard_watch" == true ]]; then
            trap 'printf "\n대시보드 새로고침을 종료합니다.\n"; exit 130' INT TERM
            while true; do
                if [[ -t 1 ]]; then
                    printf '\033[2J\033[H'
                fi
                compose run --rm --no-deps -T bot python -m seulseul.notices.dashboard
                printf '\n5초 후 새로고침 · 종료: Ctrl+C\n'
                sleep 5
            done
        fi
        compose run --rm --no-deps -T bot python -m seulseul.notices.dashboard
        ;;
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
