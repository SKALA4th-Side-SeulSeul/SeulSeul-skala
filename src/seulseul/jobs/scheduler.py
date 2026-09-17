"""30초 주기와 변경 신호를 합쳐 실행하는 단일 작업 스레드."""

import logging
from collections.abc import Callable
from threading import Event, Thread

logger = logging.getLogger(__name__)


class ChecklistScheduler:
    def __init__(self, task: Callable[[], None], wakeup: Event, *, interval: float = 30) -> None:
        self._task = task
        self._wakeup = wakeup
        self._interval = interval
        self.stopped = Event()
        self._thread = Thread(target=self._run, name="checklist-sync", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self.stopped.set()
        self._wakeup.set()
        self._thread.join()

    def _run(self) -> None:
        while not self.stopped.is_set():
            self._wakeup.clear()
            try:
                self._task()
            except Exception as error:
                logger.error("정기 작업 오류: type=%s", type(error).__name__)
            self._wakeup.wait(self._interval)
