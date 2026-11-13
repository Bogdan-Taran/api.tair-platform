from datetime import date, time
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Login(Input):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=200)


class TokenInput(Input):
    refresh_token: str = Field(min_length=20, max_length=300)


class UserInput(Login):
    password: str = Field(min_length=12, max_length=200)
    name: str = Field(min_length=1, max_length=200)


class BranchInput(Input):
    name: str = Field(min_length=1, max_length=200)
    address: str = Field(default="", max_length=500)
    timezone: str = "Asia/Tomsk"
    archived: bool = False

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value):
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError:
            raise ValueError("Unknown IANA timezone")
        return value


class CoachInput(Input):
    name: str = Field(min_length=1, max_length=200)
    salary_bps: int | None = Field(default=None, ge=0, le=10000)
    archived: bool = False


class GroupInput(Input):
    branch_id: str
    coach_id: str
    name: str = Field(min_length=1, max_length=200)
    age_min: int = Field(default=0, ge=0, le=100)
    age_max: int = Field(default=18, ge=0, le=100)
    archived: bool = False

    @model_validator(mode="after")
    def ages(self):
        if self.age_max < self.age_min:
            raise ValueError("age_max must be >= age_min")
        return self


class AthleteInput(Input):
    name: str = Field(min_length=1, max_length=200)
    birth_date: date | None = None
    parent_name: str = Field(default="", max_length=200)
    parent_phone: str = Field(default="", max_length=50)
    notes: str = Field(default="", max_length=2000)
    archived: bool = False

    @field_validator("birth_date")
    @classmethod
    def valid_birth(cls, value):
        if value and value > date.today():
            raise ValueError("Birth date cannot be in the future")
        return value


class EnrollmentInput(Input):
    athlete_id: str
    group_id: str
    starts_on: date
    ends_on: date | None = None

    @model_validator(mode="after")
    def dates(self):
        if self.ends_on and self.ends_on < self.starts_on:
            raise ValueError("Invalid enrollment interval")
        return self


class TariffInput(Input):
    name: str = Field(min_length=1, max_length=200)
    lessons: Literal[1, 4, 8, 12]
    amount_kopecks: int = Field(gt=0, le=100_000_000)
    archived: bool = False


class PaymentInput(Input):
    athlete_id: str
    group_id: str
    tariff_id: str
    paid_on: date
    amount_kopecks: int | None = Field(default=None, gt=0, le=100_000_000)
    method: Literal["cash", "transfer", "other"] = "transfer"
    request_key: str = Field(min_length=8, max_length=100)


class PaymentCorrection(Input):
    amount_kopecks: int = Field(gt=0, le=100_000_000)
    paid_on: date
    voided: bool = False
    reason: str = Field(min_length=3, max_length=500)


class ScheduleInput(Input):
    group_id: str
    weekday: int = Field(ge=0, le=6)
    starts_at: time
    ends_at: time
    starts_on: date
    ends_on: date | None = None
    archived: bool = False

    @model_validator(mode="after")
    def interval(self):
        if self.ends_at <= self.starts_at or (self.ends_on and self.ends_on < self.starts_on):
            raise ValueError("Invalid schedule interval")
        return self


class TrainingInput(Input):
    group_id: str
    coach_id: str
    date: date
    starts_at: time
    ends_at: time
    cancelled: bool = False

    @model_validator(mode="after")
    def interval(self):
        if self.ends_at <= self.starts_at:
            raise ValueError("ends_at must be after starts_at")
        return self


class AttendanceItem(Input):
    athlete_id: str
    status: Literal["present", "absent", "unmarked"]


class AttendanceBatch(Input):
    items: list[AttendanceItem] = Field(min_length=1, max_length=200)
    reason: str = Field(default="Отметка посещаемости", min_length=3, max_length=500)

    @model_validator(mode="after")
    def unique(self):
        if len({i.athlete_id for i in self.items}) != len(self.items):
            raise ValueError("Duplicate athletes")
        return self


class GenerateInput(Input):
    group_id: str
    starts_on: date
    ends_on: date

    @model_validator(mode="after")
    def interval(self):
        if not 0 <= (self.ends_on - self.starts_on).days <= 92:
            raise ValueError("Generate between 0 and 92 days")
        return self


class CorrectTraining(TrainingInput):
    reason: str = Field(min_length=3, max_length=500)
