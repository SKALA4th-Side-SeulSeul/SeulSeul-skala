"""정기 작업은 서비스만 호출하며 변경 신호·종료를 처리한다."""

from threading import Event
from unittest.mock import Mock

from seulseul.jobs.scheduler import ChecklistScheduler
from seulseul.jobs.tasks import refresh_daily_checklists


def test_task_delegates_to_service() -> None:
    service = Mock()
    stop = Mock(return_value=False)
    refresh_daily_checklists(service, stop)
    service.run_due.assert_called_once_with(stop)


def test_scheduler_runs_on_start_and_wakeup_and_stops() -> None:
    wakeup, first, second = Event(), Event(), Event()
    calls = []

    def task():
        calls.append(True)
        (first if len(calls) == 1 else second).set()

    scheduler = ChecklistScheduler(task, wakeup, interval=60)
    scheduler.start()
    try:
        assert first.wait(2)
        wakeup.set()
        assert second.wait(2)
    finally:
        scheduler.stop()
    assert len(calls) == 2


def test_scheduler_survives_task_failure() -> None:
    wakeup, failed, recovered = Event(), Event(), Event()
    calls = []

    def task():
        calls.append(True)
        if len(calls) == 1:
            failed.set()
            raise RuntimeError("synthetic failure")
        recovered.set()

    scheduler = ChecklistScheduler(task, wakeup, interval=60)
    scheduler.start()
    try:
        assert failed.wait(2)
        wakeup.set()
        assert recovered.wait(2)
    finally:
        scheduler.stop()
