"""AI 클라이언트와 구조화된 공지 분석을 실제 외부 API 없이 검증한다."""

import json
from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from seulseul.ai.client import AiClientError, OpenAICompatibleChatClient
from seulseul.ai.model import NoticeAnalysis
from seulseul.ai.service import MAX_NOTICE_INPUT_CHARS, NoticeAnalyzer, parse_notice_analysis

SECRET_API_KEY = "nvapi-test-secret-key"
SEOUL = ZoneInfo("Asia/Seoul")
POSTED_AT = datetime(2026, 9, 14, 9, 0, tzinfo=SEOUL)
COMMON_PHRASE_POSTED_AT = datetime(2026, 9, 30, 0, 0, tzinfo=SEOUL)


def create_client(
    handler: Callable[[httpx.Request], httpx.Response],
    timeout_seconds: float = 5.0,
    *,
    provider: str = "nvidia",
    disable_thinking: bool = False,
) -> OpenAICompatibleChatClient:
    return OpenAICompatibleChatClient(
        base_url="https://ai.example.test/v1",
        api_key=SECRET_API_KEY,
        model="test-model",
        timeout_seconds=timeout_seconds,
        provider=provider,
        disable_thinking=disable_thinking,
        transport=httpx.MockTransport(handler),
    )


def completion_response(content: object) -> httpx.Response:
    return httpx.Response(
        200, json={"choices": [{"message": {"role": "assistant", "content": content}}]}
    )


def analysis_json(**overrides: str) -> str:
    payload = {
        "title": "과제 제출",
        "summary": "과제를 폼으로 제출합니다.",
        "deadline_at": "2026-09-20T23:59:00+09:00",
        "deadline_source_text": "9월 20일까지",
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


class SequenceChatClient:
    def __init__(self, responses: list[str | AiClientError]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str]] = []

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        response = self.responses[len(self.calls) - 1]
        if isinstance(response, AiClientError):
            raise response
        return response


def test_complete_sends_decided_nvidia_parameters() -> None:
    captured_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return completion_response(analysis_json())

    result = create_client(handler, disable_thinking=True).complete("시스템 지시", "사용자 입력")

    assert result == analysis_json()
    body = json.loads(captured_requests[0].content)
    assert body["temperature"] == 0.2
    assert body["top_p"] == 0.8
    assert body["max_tokens"] == 512
    assert body["stream"] is False
    assert body["chat_template_kwargs"] == {"enable_thinking": False}


def test_ollama_complete_uses_native_api_to_disable_qwen_thinking() -> None:
    captured_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(
            200,
            json={"message": {"role": "assistant", "content": analysis_json()}},
        )

    result = create_client(
        handler,
        provider="ollama",
        disable_thinking=True,
    ).complete("시스템 지시", "사용자 입력")

    assert result == analysis_json()
    request = captured_requests[0]
    assert request.url.path == "/api/chat"
    body = json.loads(request.content)
    assert body["think"] is False
    assert body["stream"] is False
    assert body["keep_alive"] == "5m"
    assert body["format"] == {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "summary": {"type": "string"},
            "deadline_at": {"type": "string"},
            "deadline_source_text": {"type": "string"},
        },
        "required": ["title", "summary", "deadline_at", "deadline_source_text"],
    }
    assert body["options"] == {
        "temperature": 0.2,
        "top_p": 0.8,
        "num_ctx": 2048,
        "num_predict": 256,
    }


@pytest.mark.parametrize(("status", "retryable"), [(401, False), (429, True), (500, True)])
def test_complete_classifies_retryable_statuses(status: int, retryable: bool) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="error", headers={"Retry-After": "3"})

    with pytest.raises(AiClientError) as error:
        create_client(handler).complete("시스템", "사용자")

    assert error.value.retryable is retryable
    assert error.value.auto_retryable is retryable
    assert error.value.retry_after_seconds == 3
    assert SECRET_API_KEY not in str(error.value)


def test_complete_reports_timeout_as_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    with pytest.raises(AiClientError, match="5초 안에 오지 않았습니다") as error:
        create_client(handler).complete("시스템", "사용자")

    assert error.value.retryable
    assert error.value.auto_retryable


def test_complete_rejects_unexpected_response_shape() -> None:
    with pytest.raises(AiClientError, match="OpenAI 호환"):
        create_client(lambda request: httpx.Response(200, json={})).complete("시스템", "사용자")


def test_analyzer_returns_validated_json_result() -> None:
    client = SequenceChatClient([analysis_json()])
    analyzer = NoticeAnalyzer(client, sleeper=lambda seconds: None)

    result = analyzer.analyze(
        "9월 20일까지 과제를 제출하세요.", "https://forms.example.test/task", POSTED_AT
    )

    assert result == NoticeAnalysis(
        title="과제 제출",
        summary="과제를 폼으로 제출합니다.",
        deadline_at=datetime(2026, 9, 20, 23, 59, tzinfo=SEOUL),
        deadline_source_text="9월 20일까지",
    )
    assert "대상 링크: https://forms.example.test/task" in client.calls[0][1]
    assert POSTED_AT.isoformat() in client.calls[0][1]
    assert "[공지 원문 시작]" in client.calls[0][1]
    assert "[공지 원문 끝]" in client.calls[0][1]
    assert "메타데이터 문구를 사용하지 않는다" in client.calls[0][0]
    assert '"자정까지"는 해당 날짜의 24:00' in client.calls[0][0]
    assert (
        "신청 폼·제출 폼·등록 링크가 있으면 신청·제출·등록 마감을 deadline_at으로 선택한다."
        in client.calls[0][0]
    )
    assert "신청 마감과 행사 일시가 모두 있으면 신청 마감을 우선한다." in client.calls[0][0]
    assert "변경 전과 변경 후가 함께 있으면 변경 후의 일정을 선택한다." in client.calls[0][0]
    assert (
        "deadline_source_text에는 날짜와 직접 연결된 시간 표현을 함께 인용한다."
        in client.calls[0][0]
    )


def test_analyzer_retries_invalid_json_with_decided_delays() -> None:
    client = SequenceChatClient(["not-json", "still-not-json", analysis_json()])
    delays: list[float] = []
    analyzer = NoticeAnalyzer(client, sleeper=delays.append)

    result = analyzer.analyze(
        "9월 20일까지 과제를 제출하세요.", "https://docs.example.test/task", POSTED_AT
    )

    assert result.title == "과제 제출"
    assert len(client.calls) == 3
    assert delays == [2.0, 8.0]


def test_analyzer_obeys_retry_after() -> None:
    client = SequenceChatClient(
        [AiClientError("rate limited", retry_after_seconds=7), analysis_json()]
    )
    delays: list[float] = []

    NoticeAnalyzer(client, sleeper=delays.append).analyze(
        "9월 20일까지 제출", "https://forms.example.test/task", POSTED_AT
    )

    assert delays == [7]


def test_analyzer_does_not_retry_non_retryable_error() -> None:
    client = SequenceChatClient([AiClientError("unauthorized", retryable=False)])

    with pytest.raises(AiClientError, match="unauthorized") as error:
        NoticeAnalyzer(client, sleeper=lambda seconds: None).analyze(
            "9월 20일까지 제출", "https://forms.example.test/task", POSTED_AT
        )

    assert len(client.calls) == 1
    assert error.value.retry_count == 0


def test_analyzer_reports_retry_count_after_final_failure() -> None:
    client = SequenceChatClient(["not-json", "still-not-json", "also-not-json"])

    with pytest.raises(AiClientError) as error:
        NoticeAnalyzer(client, sleeper=lambda seconds: None).analyze(
            "9월 20일까지 제출", "https://forms.example.test/task", POSTED_AT
        )

    assert len(client.calls) == 3
    assert error.value.retry_count == 2


def test_analyzer_rejects_oversized_notice_without_truncating_or_calling_ai() -> None:
    client = SequenceChatClient([analysis_json()])

    with pytest.raises(AiClientError, match="입력 제한") as error:
        NoticeAnalyzer(client, sleeper=lambda seconds: None).analyze(
            "가" * (MAX_NOTICE_INPUT_CHARS + 1),
            "https://forms.example.test/task",
            POSTED_AT,
        )

    assert not error.value.retryable
    assert client.calls == []


@pytest.mark.parametrize(
    "raw_output",
    [
        analysis_json(deadline_at="2026-09-10T23:59:00+09:00"),
        analysis_json(deadline_at="2026-09-20T23:59:00"),
        analysis_json(deadline_source_text="원문에 없는 날짜"),
        json.dumps({"title": "제목"}),
    ],
)
def test_parse_notice_analysis_rejects_invalid_results(raw_output: str) -> None:
    with pytest.raises(AiClientError):
        parse_notice_analysis(raw_output, "9월 20일까지 제출", POSTED_AT)


def test_yearless_deadline_repairs_model_year_to_slack_post_year() -> None:
    result = parse_notice_analysis(
        analysis_json(
            deadline_source_text="9월 30일 오후 6시까지",
            deadline_at="2025-09-30T18:00:00+09:00",
        ),
        "9월 30일 오후 6시까지 과제를 제출해 주세요.",
        POSTED_AT,
    )

    assert result.deadline_at == datetime(2026, 9, 30, 18, tzinfo=SEOUL)


def test_accepts_equivalent_deadline_evidence_with_slack_formatting() -> None:
    notice_text = (
        ":warning: 보안 공지 — API Key 관리 안내\n\n"
        "최근 실습 Repository에 `.env` 파일이 포함되는 사례가 확인되었습니다.\n"
        "API Key, DB Password, Access Token 등 민감 정보가 Git에 올라가지 않도록 "
        "*2026년 10월 5일 18:00까지* 확인해 주세요.\n\n"
        "• 보안 체크리스트\n"
        "https://docs.google.com/document/d/SECURITYCHECK1005/edit\n\n"
        "• 보안 점검 완료 제출\n"
        "https://forms.gle/SECURITYCHECK1005"
    )

    result = parse_notice_analysis(
        analysis_json(
            deadline_at="2026-10-05T18:00:00+09:00",
            deadline_source_text="2026년 10월 5일 오후 6시까지 확인해 주세요",
        ),
        notice_text,
        POSTED_AT,
    )

    assert result.deadline_at == datetime(2026, 10, 5, 18, tzinfo=SEOUL)


def test_accepts_deadline_evidence_with_extra_date_context() -> None:
    notice_text = (
        "보안 공지\n민감 정보가 Git에 올라가지 않도록 *2026년 10월 5일 18:00까지* 확인해 주세요."
    )

    result = parse_notice_analysis(
        analysis_json(
            deadline_at="2026-10-05T18:00:00+09:00",
            deadline_source_text=(
                "Slack 게시 시각 2026년 9월 21일 기준, 2026년 10월 5일 18:00까지 확인해 주세요"
            ),
        ),
        notice_text,
        POSTED_AT,
    )

    assert result.deadline_at == datetime(2026, 10, 5, 18, tzinfo=SEOUL)


def test_explicit_past_year_is_not_repaired() -> None:
    with pytest.raises(AiClientError, match="과거"):
        parse_notice_analysis(
            analysis_json(
                deadline_source_text="2025년 9월 30일까지",
                deadline_at="2025-09-30T23:59:00+09:00",
            ),
            "2025년 9월 30일까지 과제를 제출해 주세요.",
            POSTED_AT,
        )


@pytest.mark.parametrize(
    "source,deadline",
    [
        ("9월 28일까지", "2026-09-30T23:59:00+09:00"),
        ("9월 28일까지", "2026-09-28T18:00:00+09:00"),
        ("9/28 18:00", "2026-09-28T23:59:00+09:00"),
        ("9/28 또는 9/30", "2026-09-28T23:59:00+09:00"),
        ("나중에 제출", "2026-09-28T23:59:00+09:00"),
        ("9/31", "2026-09-30T23:59:00+09:00"),
        ("9/28 자정", "2026-09-28T23:59:00+09:00"),
        ("9/28 25:00", "2026-09-28T23:59:00+09:00"),
    ],
)
def test_deadline_must_match_independently_parsed_evidence(source, deadline):
    with pytest.raises(AiClientError):
        parse_notice_analysis(
            analysis_json(deadline_source_text=source, deadline_at=deadline), source, POSTED_AT
        )


@pytest.mark.parametrize(
    "source,deadline",
    [
        ("2026-09-28 18:30", "2026-09-28T18:30:00+09:00"),
        ("9월 28일 오후 6시 30분", "2026-09-28T18:30:00+09:00"),
        ("2026.9.28", "2026-09-28T23:59:00+09:00"),
        ("내일", "2026-09-15T23:59:00+09:00"),
        ("모레 오전 11시", "2026-09-16T11:00:00+09:00"),
        ("이번 주 금요일", "2026-09-18T23:59:00+09:00"),
        ("다음 주 월요일 정오", "2026-09-21T12:00:00+09:00"),
        ("9월 28일 오후 6시 반", "2026-09-28T18:30:00+09:00"),
        ("9월 28일 밤 9시", "2026-09-28T21:00:00+09:00"),
        ("9/28 24:00", "2026-09-29T00:00:00+09:00"),
        ("9/28 자정까지", "2026-09-29T00:00:00+09:00"),
    ],
)
def test_explicit_and_relative_deadlines_match_source(source, deadline):
    result = parse_notice_analysis(
        analysis_json(deadline_source_text=source, deadline_at=deadline), source, POSTED_AT
    )
    assert result.deadline_at.isoformat() == deadline


@pytest.mark.parametrize(
    "source,deadline",
    [
        ("금일 밤 9시까지", "2026-09-30T21:00:00+09:00"),
        ("금일 오후 6시 반까지", "2026-09-30T18:30:00+09:00"),
        ("금일 여섯 시까지", "2026-09-30T06:00:00+09:00"),
        ("금일 6 PM까지", "2026-09-30T18:00:00+09:00"),
        ("금일 18시 30분 30초까지", "2026-09-30T18:30:30+09:00"),
        ("명일 오후 6시까지", "2026-10-01T18:00:00+09:00"),
        ("익일 오후 6시까지", "2026-10-01T18:00:00+09:00"),
        ("내일모레 오후 6시까지", "2026-10-02T18:00:00+09:00"),
        ("이틀 뒤 오후 6시까지", "2026-10-02T18:00:00+09:00"),
        ("금주 금요일까지", "2026-10-02T23:59:00+09:00"),
        ("차주 월요일까지", "2026-10-05T23:59:00+09:00"),
        ("다음 달 5일까지", "2026-10-05T23:59:00+09:00"),
        ("9월 말까지", "2026-09-30T23:59:00+09:00"),
        ("이번 달 말일까지", "2026-09-30T23:59:00+09:00"),
    ],
)
def test_common_date_and_time_phrases_are_independently_verified(source, deadline):
    result = parse_notice_analysis(
        analysis_json(deadline_at=deadline, deadline_source_text=source),
        source,
        COMMON_PHRASE_POSTED_AT,
    )

    assert result.deadline_at.isoformat() == deadline


def test_unparsed_relative_time_does_not_default_to_end_of_day():
    with pytest.raises(AiClientError):
        parse_notice_analysis(
            analysis_json(
                deadline_at="2026-09-30T23:59:00+09:00",
                deadline_source_text="금일 10분 전까지",
            ),
            "금일 10분 전까지",
            POSTED_AT,
        )


def test_deadline_at_must_match_the_selected_evidence_not_an_event_time():
    notice_text = "제출 마감: 10월 1일 오후 6시\n행사 시작: 10월 2일 오후 2시"

    with pytest.raises(AiClientError, match="인용 근거"):
        parse_notice_analysis(
            analysis_json(
                deadline_at="2026-10-02T14:00:00+09:00",
                deadline_source_text="10월 1일 오후 6시",
            ),
            notice_text,
            POSTED_AT,
        )


def test_related_date_and_time_lines_can_form_one_deadline_evidence():
    notice_text = "마감 날짜: 금일\n마감 시간: 오후 6시\n행사 시작: 10월 2일 13:00"

    result = parse_notice_analysis(
        analysis_json(
            deadline_at="2026-09-30T18:00:00+09:00",
            deadline_source_text="금일 오후 6시",
        ),
        notice_text,
        COMMON_PHRASE_POSTED_AT,
    )

    assert result.deadline_at.isoformat() == "2026-09-30T18:00:00+09:00"


@pytest.mark.parametrize(
    ("source", "deadline"),
    [
        ("금일 자정부터", "2026-10-01T00:00:00+09:00"),
        ("금일 6시부터", "2026-09-30T06:00:00+09:00"),
    ],
)
def test_submission_start_time_is_not_saved_as_a_deadline(source, deadline):
    with pytest.raises(AiClientError):
        parse_notice_analysis(
            analysis_json(
                deadline_at=deadline,
                deadline_source_text=source,
            ),
            f"제출 시작은 {source}입니다.",
            POSTED_AT,
        )


def test_date_only_quote_cannot_hide_explicit_time_on_same_line():
    with pytest.raises(AiClientError, match="일치"):
        parse_notice_analysis(analysis_json(), "마감: 9월 20일까지 18:00 제출", POSTED_AT)


@pytest.mark.parametrize("evidence", ["금일 13:10까지", "오늘 13:10까지", "9월 22일 13:10까지"])
def test_geumil_deadline_with_separate_presentation_time(evidence):
    source = (
        "실습 결과 리포트를 제출하세요.\n"
        "• 제출 마감: 금일 13:10까지\n"
        "• 제출 링크: https://forms.example.test/report\n"
        "전원 제출 완료\n발표 시작: 13:30\n발표 시간: 개별 5 ~ 10분"
    )
    # UTC 게시 시각도 한국 날짜로 환산한다. 재처리 시각은 사용하지 않는다.
    posted = datetime.fromisoformat("2026-09-22T00:40:36+00:00")
    result = parse_notice_analysis(
        analysis_json(deadline_source_text=evidence, deadline_at="2026-09-22T13:10:00+09:00"),
        source,
        posted,
    )
    assert result.deadline_at.isoformat() == "2026-09-22T13:10:00+09:00"


@pytest.mark.parametrize("deadline", ["2026-09-14T13:30:00+09:00", "2026-09-15T13:10:00+09:00"])
def test_geumil_rejects_wrong_time_or_day(deadline):
    with pytest.raises(AiClientError):
        parse_notice_analysis(
            analysis_json(deadline_source_text="금일 13:10까지", deadline_at=deadline),
            "제출 마감: 금일 13:10까지\n발표 시작: 13:30",
            POSTED_AT,
        )


def test_geumil_without_time_defaults_to_end_of_korean_posting_day():
    result = parse_notice_analysis(
        analysis_json(deadline_source_text="금일까지", deadline_at="2026-09-15T23:59:00+09:00"),
        "금일까지 제출",
        datetime.fromisoformat("2026-09-14T16:00:00+00:00"),
    )
    assert result.deadline_at.isoformat() == "2026-09-15T23:59:00+09:00"


def test_geumil_midnight_until_deadline_is_next_day_midnight():
    notice_text = (
        "실습 결과는 아래 설문 링크를 통해서 제출해 주세요.\n\n"
        "제출 기한은 금일 자정까지, 제출 파일명은 "
        "고유번호_이름_RAG-Pipeline.ipynb (예: G123_홍길동_RAG-Pipeline.ipynb) 입니다.\n\n"
        "제출 파일명 규칙에 맞춰서 과제 제출 기한 내 제출해 주세요."
    )
    posted_at = datetime(2026, 9, 30, 15, 46, 47, tzinfo=SEOUL)

    result = parse_notice_analysis(
        analysis_json(
            deadline_at="2026-10-01T00:00:00+09:00",
            deadline_source_text="금일 자정까지",
        ),
        notice_text,
        posted_at,
    )

    assert result.deadline_at == datetime(2026, 10, 1, 0, 0, tzinfo=SEOUL)


def test_date_quote_cannot_hide_time_on_another_line():
    text = "9월 20일까지\n마감 시간: 오후 6시"
    with pytest.raises(AiClientError, match="함께 인용"):
        parse_notice_analysis(analysis_json(), text, POSTED_AT)
    assert (
        parse_notice_analysis(
            analysis_json(deadline_source_text=text, deadline_at="2026-09-20T18:00:00+09:00"),
            text,
            POSTED_AT,
        ).deadline_at.hour
        == 18
    )


def test_date_only_deadline_ignores_unrelated_timetable_times():
    notice_text = (
        "@here 여러분~ 긴 연휴가 끝나고 다시 일상이 시작됐습니다!\n"
        "생성형 AI 서비스 개발의 이해/활용 (LangChain) 교과목 종료 서베이 "
        "https://forms.gle/example에 참여해주세요.\n"
        "※ 서베이는 익명으로 진행되며, 교육 품질 관리에 활용될 예정입니다.\n"
        "※ 마감일 : ~9/30(수)\n\n"
        "금일 타임테이블입니다.\n\n"
        "RAG Pipeline 설계 및 구축\n"
        "Day1 Time Table\n"
        "09:00 ~ 09:50 Intro: Why RAG?\n"
        "10:00 ~ 10:50 How RAG Works\n"
        "11:00 ~ 11:50 Building RAG Pipeline : Indexing\n"
        "13:10 ~ 14:00 Building RAG Pipeline : Runtime\n"
        "14:10 ~ 15:00 Limits of Basic RAG\n"
        "15:10 ~ 18:00 Hands-on"
    )
    posted_at = datetime(2026, 9, 27, 23, 38, 2, tzinfo=SEOUL)

    result = parse_notice_analysis(
        analysis_json(
            deadline_at="2026-09-30T23:59:00+09:00",
            deadline_source_text="9/30(수)",
        ),
        notice_text,
        posted_at,
    )

    assert result.deadline_at == datetime(2026, 9, 30, 23, 59, tzinfo=SEOUL)


def test_date_only_deadline_ignores_schedule_range_in_same_paragraph():
    notice_text = "설문 마감일: 9/30(수)\n교육 시간표: 09:00 ~ 18:00"

    result = parse_notice_analysis(
        analysis_json(
            deadline_at="2026-09-30T23:59:00+09:00",
            deadline_source_text="9/30(수)",
        ),
        notice_text,
        POSTED_AT,
    )

    assert result.deadline_at == datetime(2026, 9, 30, 23, 59, tzinfo=SEOUL)


def test_related_deadline_time_requires_time_in_evidence_across_paragraphs():
    notice_text = "설문 마감일: 9/30(수)\n\n마감 시간: 오후 6시"

    with pytest.raises(AiClientError, match="함께 인용"):
        parse_notice_analysis(
            analysis_json(
                deadline_at="2026-09-30T23:59:00+09:00",
                deadline_source_text="9/30(수)",
            ),
            notice_text,
            POSTED_AT,
        )


@pytest.mark.parametrize("title", ["A" * 256, "제목\x00"])
def test_title_storage_constraints_are_validated(title):
    with pytest.raises(AiClientError):
        parse_notice_analysis(analysis_json(title=title), "9월 20일까지", POSTED_AT)


@pytest.mark.parametrize("delay", ["61", "inf", "nan", "-1"])
def test_retry_after_out_of_range_fails_without_sleeping(delay):
    calls = []
    sleeps = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": delay})

    client = create_client(handler)
    try:
        with pytest.raises(AiClientError) as error:
            NoticeAnalyzer(client, sleeper=sleeps.append).analyze(
                "9월 20일까지", "https://forms.example.test/task", POSTED_AT
            )
        assert not error.value.retryable
        assert len(calls) == 1
        assert sleeps == []
    finally:
        client.close()


def test_retry_after_exact_limit_is_allowed():
    client = SequenceChatClient([AiClientError("limited", retry_after_seconds=60), analysis_json()])
    sleeps = []
    NoticeAnalyzer(client, sleeper=sleeps.append).analyze(
        "9월 20일까지", "https://forms.example.test/task", POSTED_AT
    )
    assert sleeps == [60]
