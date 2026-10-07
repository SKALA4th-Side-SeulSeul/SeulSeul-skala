"""운영 터미널 공통 표시 폭 계산을 확인한다."""

from seulseul.notices.screen import banner, display_width, footer, pad_display, truncate_display


def test_korean_characters_use_two_display_columns():
    assert display_width("한A글") == 5
    assert pad_display("공지", 6) == "공지  "


def test_truncation_uses_display_width_and_adds_ellipsis():
    result = truncate_display("가나다라마바사", 5)

    assert result == "가나…"
    assert display_width(result) == 5
    assert truncate_display("한", 1) == "…"
    assert truncate_display("한글", 0) == ""


def test_banner_and_footer_accept_an_explicit_display_width():
    assert display_width(banner("대시보드", "현재 기준", width=80)[0]) == 80
    assert footer(("상세 보기",), width=120)[1] == "─" * 120
