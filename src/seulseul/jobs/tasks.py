"""정기 작업의 서비스 호출만 담당한다."""

from collections.abc import Callable

from seulseul.checklists.service import DailyChecklistService


def refresh_daily_checklists(
    service: DailyChecklistService,
    should_stop: Callable[[], bool] = lambda: False,
) -> None:
    service.run_due(should_stop)
