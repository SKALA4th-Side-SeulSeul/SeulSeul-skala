"""SQLAlchemy 관계 구성을 위해 모든 영속 모델을 한 번에 등록한다."""

from seulseul.checklists.model import ChecklistModel, DailyChecklistMessageModel
from seulseul.notices.model import NoticeModel, NoticeSourceModel
from seulseul.users.model import StudentModel

MAPPED_MODELS = (
    StudentModel,
    NoticeModel,
    NoticeSourceModel,
    ChecklistModel,
    DailyChecklistMessageModel,
)
