"""Explicit OpenAPI response contracts used by Flutter code generation."""

from datetime import date
from typing import TypeVar

from pydantic import BaseModel, ConfigDict, create_model

from app import models as m


class Output(BaseModel):
    model_config = ConfigDict(from_attributes=True)


def row_model(model):
    fields = {}
    for column in model.__table__.columns:
        kind = column.type.python_type
        fields[column.name] = (kind | None if column.nullable else kind, ...)
    return create_model(model.__name__ + "Record", __base__=Output, **fields)


Athlete = row_model(m.Athlete)
Payment = row_model(m.Payment)
Training = row_model(m.Training)
Attendance = row_model(m.Attendance)
Enrollment = row_model(m.Enrollment)
Audit = row_model(m.Audit)
T = TypeVar("T")


class Page[T](Output):
    items: list[T]
    total: int
    offset: int
    limit: int


class Items[T](Output):
    items: list[T]


class UserInfo(Output):
    id: str
    username: str
    name: str


class Health(Output):
    status: str


class Balance(Output):
    remaining_lessons: int
    debt_lessons: int
    debt_kopecks: int
    attended: int
    earned_kopecks: int


class AthleteCard(Output):
    athlete: Athlete
    balance: Balance
    enrollments: list[Enrollment]
    payments: list[Payment]
    attendance: list[Attendance]


class RosterItem(Output):
    athlete: Athlete
    attendance: Attendance | None
    balance: Balance


class Roster(Output):
    training: Training
    items: list[RosterItem]


class Generated(Output):
    items: list[Training]
    created: int


class FinanceItem(Output):
    branch_id: str
    group_id: str
    coach_id: str
    attended: int
    earned_kopecks: int
    debt_kopecks: int
    salary_kopecks: int | None


class Finance(Output):
    starts_on: date
    ends_on: date
    received_kopecks: int | None
    earned_kopecks: int
    debt_kopecks: int
    attended: int
    salary_kopecks: int | None
    items: list[FinanceItem]


class TimesheetItem(Output):
    athlete: Athlete
    attendance: list[Attendance]
    attended: int
    earned_kopecks: int
    debt_kopecks: int
    balance: Balance


class Timesheet(Output):
    trainings: list[Training]
    items: list[TimesheetItem]


class Notification(Balance):
    athlete_id: str
    group_id: str
    name: str
    kind: str
