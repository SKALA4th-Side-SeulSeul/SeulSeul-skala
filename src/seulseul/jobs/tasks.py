"""정기 작업의 서비스 호출만 담당한다."""

from collections.abc import Callable

from seulseul.checklists.service import DailyChecklistService
from seulseul.notices.service import NoticeService


def refresh_checklists(
    service: DailyChecklistService,
    should_stop: Callable[[], bool] = lambda: False,
) -> None:
    service.run_due(should_stop)


def retry_notices(
    service: NoticeService, workspace_id: str, should_stop: Callable[[], bool]
) -> None:
    service.run_retries(workspace_id, should_stop)
