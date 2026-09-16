"""체크리스트 ORM 모델의 관계와 상태 컬럼을 확인한다."""

from sqlalchemy import UniqueConstraint
from sqlalchemy.orm import configure_mappers

from seulseul.checklists.model import ChecklistModel
from seulseul.notices.model import NoticeModel
from seulseul.users.model import StudentModel


def test_checklist_model_links_student_and_notice_with_cascade_delete() -> None:
    configure_mappers()
    table = ChecklistModel.__table__
    foreign_keys = {
        foreign_key.target_fullname: foreign_key.ondelete for foreign_key in table.foreign_keys
    }
    unique_constraints = {
        constraint.name
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }

    assert foreign_keys == {"students.id": "CASCADE", "notices.id": "CASCADE"}
    assert "uq_checklists_student_notice" in unique_constraints
    assert {"completed_at", "deleted_at"} <= set(table.columns.keys())
    assert StudentModel.checklists.property.back_populates == "student"
    assert NoticeModel.checklists.property.back_populates == "notice"
