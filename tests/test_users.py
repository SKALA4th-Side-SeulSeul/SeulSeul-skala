"""학생 ORM 모델의 식별자와 소속 제약을 확인한다."""

from sqlalchemy import CheckConstraint, UniqueConstraint

from seulseul.users.model import StudentModel


def test_student_model_has_workspace_user_identity_and_affiliation() -> None:
    table = StudentModel.__table__
    unique_constraints = {
        constraint.name
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    check_constraints = {
        constraint.name
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    }

    assert {"workspace_id", "slack_user_id", "display_name", "campus", "class_number"} <= set(
        table.columns.keys()
    )
    assert "uq_students_workspace_slack_user" in unique_constraints
    assert "ck_students_class_number_positive" in check_constraints
