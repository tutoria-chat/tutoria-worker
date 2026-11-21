"""The worker's course routes (question bank, upload jobs) only let staff reach
courses of their own university; super admins reach any course."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from app.api.routes.courses import _get_course


def _db(course):
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = course
    return db


def _staff(university_id, kind="professor"):
    return {"type": kind, "user": SimpleNamespace(university_id=university_id)}


def test_same_university_and_super_admin_pass():
    course = SimpleNamespace(id=10, university_id=1)
    assert _get_course(10, _db(course), _staff(1)) is course
    assert _get_course(10, _db(course), _staff(1, "manager")) is course
    assert _get_course(10, _db(course), {"type": "super_admin", "user": SimpleNamespace()}) is course


@pytest.mark.parametrize("caller", [_staff(2), _staff(2, "manager"), _staff(None), None])
def test_other_university_is_a_404(caller):
    with pytest.raises(HTTPException) as exc:
        _get_course(10, _db(SimpleNamespace(id=10, university_id=1)), caller)
    assert exc.value.status_code == 404


def test_missing_course_is_a_404():
    with pytest.raises(HTTPException) as exc:
        _get_course(10, _db(None), _staff(1))
    assert exc.value.status_code == 404
