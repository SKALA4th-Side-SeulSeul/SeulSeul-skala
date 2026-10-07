#!/usr/bin/env bash
# 관리 메뉴 항목과 동작. 전체 화면 콘솔(scripts/console.sh)과 줄 단위 메뉴(admin.sh)가 같은 정의를 쓴다.
# operations.sh를 먼저 source해야 한다. 실제 작업은 각 운영 스크립트에 위임하고 여기서는 확인 절차만 둔다.

menu_keys=(1 2 3 4 5 6 7 8 9 0)
menu_groups=('공지' '공지' '공지' '모니터링' '모니터링' '학생 안내' '백업' '백업' '서비스' '서비스')
menu_labels=(
    '실패한 공지 AI로 다시 분석'
    '공지 직접 등록·수정·삭제'
    '반영 안 된 Slack 원본 보기'
    '봇 로그 실시간 보기'
    '공지 대시보드 전체 보기'
    '가입 학생 전체에 안내 DM'
    '지금 DB 백업하기'
    '자동 백업 켜기·끄기'
    '서비스 시작·재배포'
    '서비스 중지'
)
menu_descriptions=(
    'AI 분석에 실패해 학생 체크리스트에 반영되지 못한 공지를 다시 분석합니다. 번호는 공지 패널의 "처리가 필요한 공지" 번호입니다.'
    '봇이 읽지 못하는 채널의 공지를 등록하거나, AI가 잘못 뽑은 제목·마감일을 직접 고치거나, 공지를 삭제합니다. 저장 전에 항상 확인합니다.'
    'Slack에서 받았지만 아직 체크리스트에 반영되지 않은 원본입니다. 잠시 뒤 사라지면 정상이고, 계속 남으면 원문을 확인하세요.'
    '봇이 지금 무엇을 하는지 실시간으로 봅니다. 오류는 빨간색, 주의는 노란색입니다. Ctrl+C를 누르면 돌아옵니다.'
    '공지 패널에서 잘린 목록과 차트를 한 화면에 모두 펼쳐 봅니다.'
    '점검·장애 안내처럼 모든 가입 학생에게 알릴 내용을 DM으로 보냅니다. 미리보기와 수신 인원을 확인하고 y를 입력해야 발송됩니다.'
    '운영 DB를 지금 백업합니다. 봇은 멈추지 않습니다. 배포·마이그레이션 전에 꼭 실행하세요.'
    '매일 새벽 3시(한국 시간) 자동 백업이 켜져 있는지 보고 켜거나 끕니다.'
    'git pull로 받은 최신 코드로 이미지를 다시 만들고 DB 마이그레이션 뒤 봇을 다시 시작합니다. 몇 분 걸리며 그동안 봇이 멈춥니다.'
    '봇만 또는 전체 서비스를 멈춥니다. DB 데이터는 지워지지 않습니다.'
)

# 키에 해당하는 메뉴 번호(0부터)를 menu_index에 담는다. 없으면 -1.
menu_find() {
    local i
    menu_index=-1
    for i in "${!menu_keys[@]}"; do
        if [[ "${menu_keys[i]}" == "$1" ]]; then
            menu_index=$i
            return 0
        fi
    done
    return 1
}

menu_pause() {
    local ignored
    echo
    IFS= read -r -p '  Enter를 누르면 돌아갑니다: ' ignored || true
}

# 입력을 받는다. EOF(Ctrl+D)면 실패를 돌려 취소로 처리한다.
menu_read() {
    local variable="$1" prompt="$2" value
    if ! IFS= read -r -p "  $prompt" value; then
        echo
        return 1
    fi
    printf -v "$variable" '%s' "$value"
}

# 하위 작업이 실패하거나 Ctrl+C로 중단돼도 메뉴는 계속 쓸 수 있게 한다.
# set -e 아래에서 메뉴가 끝나지 않도록 항상 0을 돌려주고, 결과는 tool_status로 남긴다.
run_tool() {
    local previous
    tool_status=0
    previous="$(trap -p INT)"
    trap 'echo' INT
    bash "$@" || tool_status=$?
    eval "${previous:-trap - INT}"
    if [[ $tool_status -ne 0 ]]; then
        echo
        ui_warn '작업이 끝나지 않았거나 실패했습니다. 위 안내를 확인하세요.'
    fi
    return 0
}

menu_cancelled() {
    ui_info '취소했습니다.'
    menu_pause
}

menu_retry() {
    local index=''
    ui_banner '실패한 공지 AI로 다시 분석'
    run_tool "$ops_root/scripts/retry.sh" list
    echo
    menu_read index '다시 분석할 번호 (Enter는 취소): ' || return 0
    if [[ -z "$index" ]]; then
        return 0
    fi
    if [[ ! "$index" =~ ^[1-9][0-9]*$ ]]; then
        ui_warn '번호는 1 이상의 숫자로 입력하세요.'
        menu_pause
        return 0
    fi
    if ! ui_confirm "${index}번 공지를 AI로 다시 분석할까요?"; then
        menu_cancelled
        return 0
    fi
    run_tool "$ops_root/scripts/retry.sh" "$index"
    menu_pause
}

menu_backup_now() {
    if ui_confirm '지금 운영 DB를 백업할까요? (봇과 DB는 계속 실행됩니다)'; then
        run_tool "$ops_root/scripts/backup.sh"
        menu_pause
    else
        menu_cancelled
    fi
}

menu_backup_schedule() {
    local choice=''
    run_tool "$ops_root/scripts/backup.sh" status
    echo
    ui_menu_item s '자동 백업 켜기 (이미 켜져 있으면 다시 등록)'
    ui_menu_item u '자동 백업 끄기'
    ui_menu_item Enter '돌아가기'
    echo
    menu_read choice '선택: ' || return 0
    case "$choice" in
        s | S) run_tool "$ops_root/scripts/backup.sh" schedule ;;
        u | U)
            if ui_confirm '자동 백업을 끌까요? 실행 중인 백업과 기존 파일은 유지됩니다.'; then
                run_tool "$ops_root/scripts/backup.sh" unschedule
            else
                ui_info '취소했습니다.'
            fi
            ;;
        *) return 0 ;;
    esac
    menu_pause
}

menu_deploy() {
    ui_banner '서비스 시작·재배포' '최신 코드로 다시 빌드하고 봇을 재시작합니다'
    ui_info '서버에 최신 코드를 먼저 받아 두세요: git pull --ff-only origin main'
    ui_info '진행하는 몇 분 동안 봇이 멈춥니다. 실패하면 봇을 자동으로 다시 켜지 않습니다.'
    echo
    if ! ui_confirm '재배포를 시작할까요?'; then
        menu_cancelled
        return 0
    fi
    if ui_confirm '먼저 운영 DB를 백업할까요? (권장)'; then
        run_tool "$ops_root/scripts/backup.sh"
        # 백업을 원했는데 실패했다면 마이그레이션을 포함한 재배포로 넘어가지 않는다.
        if [[ $tool_status -ne 0 ]]; then
            ui_fail '백업이 실패해 재배포를 시작하지 않았습니다. 원인을 해결한 뒤 다시 시도하세요.'
            menu_pause
            return 0
        fi
    fi
    run_tool "$ops_root/run.sh"
    menu_pause
}

menu_stop() {
    local choice='' target=''
    ui_banner '서비스 중지' 'DB 데이터는 지워지지 않습니다'
    echo
    ui_menu_item b '봇만 중지 (DB는 계속 실행)'
    ui_menu_item a '전체 중지 (봇·OAuth·ngrok → DB 순서)'
    ui_menu_item Enter '돌아가기'
    echo
    menu_read choice '선택: ' || return 0
    case "$choice" in
        b | B) target=bot ;;
        a | A) target=all ;;
        *) return 0 ;;
    esac
    if ! ui_confirm "정말 중지할까요? 그동안 학생 DM이 갱신되지 않습니다."; then
        menu_cancelled
        return 0
    fi
    run_tool "$ops_root/stop.sh" "$target"
    menu_pause
}

# 메뉴 키 하나를 실행한다. 일반 화면(줄 단위 입력)에서 호출해야 한다.
menu_run() {
    case "$1" in
        1) menu_retry ;;
        2)
            run_tool "$ops_root/scripts/notice.sh" --limit 100
            menu_pause
            ;;
        3)
            run_tool "$ops_root/scripts/retry.sh" pending
            menu_pause
            ;;
        4) run_tool "$ops_root/view.sh" logs bot --follow ;;
        5)
            run_tool "$ops_root/view.sh" dashboard
            menu_pause
            ;;
        6)
            run_tool "$ops_root/scripts/announce.sh"
            menu_pause
            ;;
        7) menu_backup_now ;;
        8) menu_backup_schedule ;;
        9) menu_deploy ;;
        0) menu_stop ;;
        *) return 1 ;;
    esac
}
