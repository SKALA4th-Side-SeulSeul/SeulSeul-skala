"""개발 환경에서 src 레이아웃 패키지를 불러올 수 있는지 확인한다."""


def test_package_is_importable() -> None:
    import seulseul

    assert seulseul.__package__ == "seulseul"
