from datetime import date, datetime, timedelta
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, create_model
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app import contracts as out
from app import models as m
from app import schemas as s
from app.config import settings
from app.db import db_session
from app.security import current_user, hasher, issue_tokens, refresh_record
from app.services import audit, balance, enrolled, get, lock_athletes, recalculate, snapshot
from app.throttle import AuthThrottle

app = FastAPI(
    title="ТАИР — API учёта тренировок",
    version="0.1.0",
    description="Все суммы в копейках. Даты занятий — в часовом поясе филиала. API v1.",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings().cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH"],
    allow_headers=["Authorization", "Content-Type"],
)
app.add_middleware(AuthThrottle)
DB = Annotated[object, Depends(db_session, scope="function")]
User = Annotated[m.User, Depends(current_user, scope="function")]
api = APIRouter(prefix="/api/v1", dependencies=[Depends(current_user, scope="function")])
auth = APIRouter(prefix="/api/v1/auth", tags=["auth"])


@app.exception_handler(IntegrityError)
async def conflict(_, exc):
    return JSONResponse(status_code=409, content={"detail": "Conflicting or invalid related record"})


@app.get("/health/live", response_model=out.Health, tags=["health"])
def live():
    return {"status": "ok"}


@app.get("/health/ready", response_model=out.Health, tags=["health"])
def ready(db: DB):
    db.execute(text("SELECT 1"))
    return {"status": "ok"}


class Tokens(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str
    expires_in: int


@auth.post("/login", response_model=Tokens)
def login(body: s.Login, db: DB):
    user = db.scalar(select(m.User).where(m.User.username == body.username))
    # Verify a dummy hash too, to avoid a fast path for unknown usernames.
    candidate = user.password_hash if user else DUMMY_HASH
    valid = hasher.verify(body.password, candidate)
    if not user or not valid or not user.active:
        raise HTTPException(401, "Invalid username or password")
    return issue_tokens(db, user)


DUMMY_HASH = hasher.hash("unused-dummy-password")


@auth.post("/refresh", response_model=Tokens)
def refresh(body: s.TokenInput, db: DB):
    token = refresh_record(db, body.refresh_token)
    user = get(db, m.User, token.user_id)
    if not user.active:
        raise HTTPException(401, "Inactive user")
    token.revoked = True
    return issue_tokens(db, user)


@auth.post("/logout", status_code=204)
def logout(body: s.TokenInput, db: DB):
    refresh_record(db, body.refresh_token).revoked = True
    return Response(status_code=204)


@api.get("/me", response_model=out.UserInfo, tags=["users"])
def me(user: User):
    return {"id": user.id, "username": user.username, "name": user.name}


@api.post("/users", status_code=201, response_model=out.UserInfo, tags=["users"])
def add_user(body: s.UserInput, db: DB, user: User):
    obj = m.User(username=body.username, name=body.name, password_hash=hasher.hash(body.password))
    db.add(obj)
    db.flush()
    # Password hashes never enter the audit log.
    db.add(
        m.Audit(
            user_id=user.id,
            entity="users",
            entity_id=obj.id,
            reason="Создание пользователя",
            before={},
            after={"username": obj.username, "name": obj.name},
        )
    )
    return {"id": obj.id, "username": obj.username, "name": obj.name}


def validate_catalog(db, model, data, old=None):
    if model is m.Group:
        get(db, m.Branch, data["branch_id"], active=not data["archived"])
        get(db, m.Coach, data["coach_id"], active=not data["archived"])
        if old and old.branch_id != data["branch_id"]:
            raise HTTPException(409, "Create a new group to move branches; preserve historical reports")
    if model is m.Schedule:
        get(db, m.Group, data["group_id"], active=not data["archived"])
    if model is m.Enrollment:
        get(db, m.Athlete, data["athlete_id"], lock=True, active=True)
        get(db, m.Group, data["group_id"], active=True)
        if old and (old.athlete_id != data["athlete_id"] or old.group_id != data["group_id"]):
            raise HTTPException(409, "Enrollment identity is immutable; close it and create another")
        overlaps = select(m.Enrollment).where(
            m.Enrollment.athlete_id == data["athlete_id"],
            m.Enrollment.group_id == data["group_id"],
            m.Enrollment.starts_on <= (data["ends_on"] or date.max),
            (m.Enrollment.ends_on.is_(None) | (m.Enrollment.ends_on >= data["starts_on"])),
        )
        if old:
            overlaps = overlaps.where(m.Enrollment.id != old.id)
        if db.scalar(overlaps):
            raise HTTPException(409, "Overlapping enrollment")
        if old:
            visits = db.execute(
                select(m.Attendance, m.Training)
                .join(m.Training)
                .where(m.Attendance.athlete_id == old.athlete_id, m.Training.group_id == old.group_id)
            ).all()
            for _, training in visits:
                if old.starts_on <= training.date <= (old.ends_on or date.max) and not (
                    data["starts_on"] <= training.date <= (data["ends_on"] or date.max)
                ):
                    raise HTTPException(409, "Enrollment would exclude historical attendance")


def register_catalog(path, model, schema):
    output = create_model(model.__name__ + "Out", __base__=schema, id=(str, ...))
    page = create_model(
        model.__name__ + "Page",
        items=(list[output], ...),
        total=(int, ...),
        offset=(int, ...),
        limit=(int, ...),
    )

    def listing(
        db: DB,
        offset: int = Query(0, ge=0),
        limit: int = Query(50, ge=1, le=200),
        archived: bool | None = None,
        group_id: str | None = None,
        branch_id: str | None = None,
        search: str | None = Query(None, max_length=100),
    ):
        stmt = select(model)
        if hasattr(model, "archived"):
            stmt = stmt.where(model.archived == (archived if archived is not None else False))
        if group_id and hasattr(model, "group_id"):
            stmt = stmt.where(model.group_id == group_id)
        if branch_id and hasattr(model, "branch_id"):
            stmt = stmt.where(model.branch_id == branch_id)
        if search and hasattr(model, "name"):
            stmt = stmt.where(model.name.icontains(search, autoescape=True))
        total = db.scalar(select(func.count()).select_from(stmt.subquery()))
        return {
            "items": [snapshot(x) for x in db.scalars(stmt.order_by(model.id).offset(offset).limit(limit))],
            "total": total,
            "offset": offset,
            "limit": limit,
        }

    def detail(entity_id: str, db: DB):
        return snapshot(get(db, model, entity_id))

    def create(body, db: DB, user: User):
        data = body.model_dump()
        validate_catalog(db, model, data)
        obj = model(**data)
        db.add(obj)
        audit(db, user, obj, {}, "Создание")
        return snapshot(obj)

    def update(
        entity_id: str, body, db: DB, user: User, reason: str = Query(..., min_length=3, max_length=500)
    ):
        obj = get(db, model, entity_id, lock=True)
        before = snapshot(obj)
        data = body.model_dump()
        validate_catalog(db, model, data, obj)
        for key, value in data.items():
            setattr(obj, key, value)
        audit(db, user, obj, before, reason)
        return snapshot(obj)

    create.__annotations__["body"] = schema
    update.__annotations__["body"] = schema
    api.add_api_route(
        path, listing, methods=["GET"], response_model=page, tags=[path[1:]], name="list_" + path[1:]
    )
    api.add_api_route(
        path,
        create,
        methods=["POST"],
        response_model=output,
        status_code=201,
        tags=[path[1:]],
        name="create_" + path[1:],
    )
    api.add_api_route(
        path + "/{entity_id}",
        detail,
        methods=["GET"],
        response_model=output,
        tags=[path[1:]],
        name="get_" + path[1:],
    )
    api.add_api_route(
        path + "/{entity_id}",
        update,
        methods=["PUT"],
        response_model=output,
        tags=[path[1:]],
        name="update_" + path[1:],
    )


for path, model, schema in [
    ("/branches", m.Branch, s.BranchInput),
    ("/coaches", m.Coach, s.CoachInput),
    ("/groups", m.Group, s.GroupInput),
    ("/athletes", m.Athlete, s.AthleteInput),
    ("/enrollments", m.Enrollment, s.EnrollmentInput),
    ("/tariffs", m.Tariff, s.TariffInput),
    ("/schedules", m.Schedule, s.ScheduleInput),
]:
    register_catalog(path, model, schema)


@api.post("/payments", status_code=201, response_model=out.Payment, tags=["payments"])
def payment_create(body: s.PaymentInput, db: DB, user: User):
    get(db, m.Athlete, body.athlete_id, lock=True, active=True)
    existing = db.scalar(select(m.Payment).where(m.Payment.request_key == body.request_key))
    if existing:
        same = (
            existing.athlete_id == body.athlete_id
            and existing.group_id == body.group_id
            and existing.tariff_id == body.tariff_id
            and existing.paid_on == body.paid_on
            and existing.method == body.method
            and (body.amount_kopecks is None or body.amount_kopecks == existing.amount_kopecks)
        )
        if not same:
            raise HTTPException(409, "request_key already used for another payment")
        return snapshot(existing)
    group = get(db, m.Group, body.group_id, active=True)
    branch = get(db, m.Branch, group.branch_id, active=True)
    if body.paid_on > datetime.now(ZoneInfo(branch.timezone)).date():
        raise HTTPException(422, "Payment date cannot be in the future")
    tariff = get(db, m.Tariff, body.tariff_id, active=True)
    if not enrolled(db, body.athlete_id, body.group_id, body.paid_on):
        raise HTTPException(409, "Athlete is not enrolled on payment date")
    data = body.model_dump()
    data["amount_kopecks"] = body.amount_kopecks or tariff.amount_kopecks
    obj = m.Payment(**data, lessons=tariff.lessons)
    db.add(obj)
    audit(db, user, obj, {}, "Внесение оплаты")
    recalculate(db, obj.athlete_id)
    return snapshot(obj)


@api.patch("/payments/{payment_id}", response_model=out.Payment, tags=["payments"])
def payment_correct(payment_id: str, body: s.PaymentCorrection, db: DB, user: User):
    obj = get(db, m.Payment, payment_id)
    lock_athletes(db, [obj.athlete_id])
    obj = get(db, m.Payment, payment_id, lock=True)
    before = snapshot(obj)
    group = get(db, m.Group, obj.group_id)
    branch = get(db, m.Branch, group.branch_id)
    if body.paid_on > datetime.now(ZoneInfo(branch.timezone)).date():
        raise HTTPException(422, "Payment date cannot be in the future")
    if not enrolled(db, obj.athlete_id, obj.group_id, body.paid_on):
        raise HTTPException(409, "Athlete is not enrolled on payment date")
    for key, value in body.model_dump(exclude={"reason"}).items():
        setattr(obj, key, value)
    audit(db, user, obj, before, body.reason)
    recalculate(db, obj.athlete_id)
    return snapshot(obj)


@api.get("/payments", response_model=out.Page[out.Payment], tags=["payments"])
def payments(
    db: DB,
    athlete_id: str | None = None,
    group_id: str | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
):
    stmt = select(m.Payment)
    if athlete_id:
        stmt = stmt.where(m.Payment.athlete_id == athlete_id)
    if group_id:
        stmt = stmt.where(m.Payment.group_id == group_id)
    return {
        "items": [
            snapshot(p)
            for p in db.scalars(
                stmt.order_by(m.Payment.paid_on.desc(), m.Payment.id).offset(offset).limit(limit)
            )
        ],
        "total": db.scalar(select(func.count()).select_from(stmt.subquery())),
        "offset": offset,
        "limit": limit,
    }


def training_values(db, body):
    group = get(db, m.Group, body.group_id, active=True)
    get(db, m.Branch, group.branch_id, active=True)
    coach = get(db, m.Coach, body.coach_id, active=True)
    return {**body.model_dump(exclude={"reason"}), "salary_bps": coach.salary_bps}


@api.post("/trainings", status_code=201, response_model=out.Training, tags=["trainings"])
def training_create(body: s.TrainingInput, db: DB, user: User):
    obj = m.Training(**training_values(db, body))
    db.add(obj)
    audit(db, user, obj, {}, "Создание тренировки")
    return snapshot(obj)


@api.post("/trainings/generate", response_model=out.Generated, tags=["trainings"])
def generate(body: s.GenerateInput, db: DB, user: User):
    group = get(db, m.Group, body.group_id, lock=True, active=True)
    get(db, m.Branch, group.branch_id, active=True)
    coach = get(db, m.Coach, group.coach_id, active=True)
    schedules = list(
        db.scalars(select(m.Schedule).where(m.Schedule.group_id == group.id, m.Schedule.archived.is_(False)))
    )
    generated = []
    day = body.starts_on
    while day <= body.ends_on:
        for schedule in schedules:
            if (
                day.weekday() == schedule.weekday
                and schedule.starts_on <= day
                and (schedule.ends_on is None or day <= schedule.ends_on)
            ):
                existing = db.scalar(
                    select(m.Training.id).where(
                        m.Training.group_id == group.id,
                        m.Training.date == day,
                        m.Training.starts_at == schedule.starts_at,
                    )
                )
                if not existing:
                    obj = m.Training(
                        group_id=group.id,
                        coach_id=coach.id,
                        salary_bps=coach.salary_bps,
                        date=day,
                        starts_at=schedule.starts_at,
                        ends_at=schedule.ends_at,
                    )
                    db.add(obj)
                    audit(db, user, obj, {}, "Создание по расписанию")
                    generated.append(snapshot(obj))
        day += timedelta(days=1)
    return {"items": generated, "created": len(generated)}


@api.get("/trainings", response_model=out.Page[out.Training], tags=["trainings"])
def trainings(
    db: DB,
    starts_on: date,
    ends_on: date,
    group_id: str | None = None,
    branch_id: str | None = None,
    coach_id: str | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=200),
):
    valid_period(starts_on, ends_on)
    stmt = select(m.Training).join(m.Group).where(m.Training.date.between(starts_on, ends_on))
    if group_id:
        stmt = stmt.where(m.Training.group_id == group_id)
    if branch_id:
        stmt = stmt.where(m.Group.branch_id == branch_id)
    if coach_id:
        stmt = stmt.where(m.Training.coach_id == coach_id)
    return {
        "items": [
            snapshot(t)
            for t in db.scalars(
                stmt.order_by(m.Training.date, m.Training.starts_at, m.Training.id)
                .offset(offset)
                .limit(limit)
            )
        ],
        "total": db.scalar(select(func.count()).select_from(stmt.subquery())),
        "offset": offset,
        "limit": limit,
    }


@api.put("/trainings/{training_id}", response_model=out.Training, tags=["trainings"])
def training_correct(training_id: str, body: s.CorrectTraining, db: DB, user: User):
    obj = get(db, m.Training, training_id, lock=True)
    if obj.group_id != body.group_id:
        raise HTTPException(409, "Training group cannot change")
    attendees = list(db.scalars(select(m.Attendance).where(m.Attendance.training_id == training_id)))
    lock_athletes(db, [a.athlete_id for a in attendees])
    if any(not enrolled(db, a.athlete_id, obj.group_id, body.date) for a in attendees):
        raise HTTPException(409, "New date is outside athlete enrollment")
    group = get(db, m.Group, obj.group_id)
    branch = get(db, m.Branch, group.branch_id)
    if attendees and body.date > datetime.now(ZoneInfo(branch.timezone)).date():
        raise HTTPException(409, "Cannot move marked training into the future")
    before = snapshot(obj)
    values = training_values(db, body)
    # Preserve historical salary rate unless assigning another coach.
    if obj.coach_id == body.coach_id:
        values["salary_bps"] = obj.salary_bps
    for key, value in values.items():
        setattr(obj, key, value)
    audit(db, user, obj, before, body.reason)
    for athlete_id in sorted({a.athlete_id for a in attendees}):
        recalculate(db, athlete_id)
    return snapshot(obj)


@api.put("/trainings/{training_id}/attendance", response_model=out.Items[out.Attendance], tags=["attendance"])
def mark(training_id: str, body: s.AttendanceBatch, db: DB, user: User):
    training = get(db, m.Training, training_id, lock=True)
    if training.cancelled:
        raise HTTPException(409, "Training is cancelled")
    group = get(db, m.Group, training.group_id, active=True)
    branch = get(db, m.Branch, group.branch_id, active=True)
    if training.date > datetime.now(ZoneInfo(branch.timezone)).date():
        raise HTTPException(409, "Cannot mark future training")
    lock_athletes(db, [i.athlete_id for i in body.items])
    single = db.scalar(
        select(m.Tariff).where(m.Tariff.lessons == 1, m.Tariff.archived.is_(False)).order_by(m.Tariff.id)
    )
    if not single:
        raise HTTPException(409, "Configure an active single-lesson tariff before marking attendance")
    result = []
    for item in body.items:
        get(db, m.Athlete, item.athlete_id, active=True)
        if not enrolled(db, item.athlete_id, training.group_id, training.date):
            raise HTTPException(409, "Athlete is not enrolled on training date")
        obj = db.scalar(
            select(m.Attendance).where(
                m.Attendance.training_id == training.id, m.Attendance.athlete_id == item.athlete_id
            )
        )
        before = snapshot(obj) if obj else {}
        if not obj:
            obj = m.Attendance(
                training_id=training.id,
                athlete_id=item.athlete_id,
                status=item.status,
                single_price_kopecks=single.amount_kopecks,
            )
            db.add(obj)
        else:
            obj.status = item.status
        db.flush()
        recalculate(db, item.athlete_id)
        audit(db, user, obj, before, body.reason)
        result.append(snapshot(obj))
    return {"items": result}


@api.get("/trainings/{training_id}/roster", response_model=out.Roster, tags=["attendance"])
def roster(training_id: str, db: DB):
    training = get(db, m.Training, training_id)
    athletes = db.scalars(
        select(m.Athlete)
        .join(m.Enrollment)
        .where(
            m.Enrollment.group_id == training.group_id,
            m.Enrollment.starts_on <= training.date,
            (m.Enrollment.ends_on.is_(None) | (m.Enrollment.ends_on >= training.date)),
        )
        .distinct()
        .order_by(m.Athlete.name, m.Athlete.id)
    )
    marked = {
        a.athlete_id: a
        for a in db.scalars(select(m.Attendance).where(m.Attendance.training_id == training_id))
    }
    return {
        "training": snapshot(training),
        "items": [
            {
                "athlete": snapshot(a),
                "attendance": snapshot(marked[a.id]) if a.id in marked else None,
                "balance": balance(db, a.id, training.date, training.group_id),
            }
            for a in athletes
        ],
    }


@api.get("/athletes/{athlete_id}/card", response_model=out.AthleteCard, tags=["athletes"])
def card(athlete_id: str, db: DB):
    athlete = get(db, m.Athlete, athlete_id)
    return {
        "athlete": snapshot(athlete),
        "balance": balance(db, athlete_id),
        "enrollments": [
            snapshot(e) for e in db.scalars(select(m.Enrollment).where(m.Enrollment.athlete_id == athlete_id))
        ],
        "payments": [
            snapshot(p)
            for p in db.scalars(
                select(m.Payment)
                .where(m.Payment.athlete_id == athlete_id)
                .order_by(m.Payment.paid_on.desc())
                .limit(50)
            )
        ],
        "attendance": [
            snapshot(a)
            for a in db.scalars(
                select(m.Attendance)
                .join(m.Training)
                .where(m.Attendance.athlete_id == athlete_id)
                .order_by(m.Training.date.desc())
                .limit(100)
            )
        ],
    }


def valid_period(starts_on, ends_on):
    if not 0 <= (ends_on - starts_on).days <= 366:
        raise HTTPException(422, "Period must be between 0 and 366 days")


@api.get("/reports/finance", response_model=out.Finance, tags=["reports"])
def finance(
    db: DB,
    starts_on: date,
    ends_on: date,
    branch_id: str | None = None,
    group_id: str | None = None,
    coach_id: str | None = None,
    athlete_id: str | None = None,
):
    valid_period(starts_on, ends_on)
    stmt = (
        select(m.Attendance, m.Training, m.Group)
        .select_from(m.Attendance)
        .join(m.Training)
        .join(m.Group)
        .where(
            m.Training.date.between(starts_on, ends_on),
            m.Training.cancelled.is_(False),
            m.Attendance.status == "present",
        )
    )
    for value, column in [
        (branch_id, m.Group.branch_id),
        (group_id, m.Group.id),
        (coach_id, m.Training.coach_id),
        (athlete_id, m.Attendance.athlete_id),
    ]:
        if value:
            stmt = stmt.where(column == value)
    rows = db.execute(stmt).all()
    buckets = {}
    for a, t, g in rows:
        key = (g.branch_id, g.id, t.coach_id)
        bucket = buckets.setdefault(
            key,
            {
                "branch_id": g.branch_id,
                "group_id": g.id,
                "coach_id": t.coach_id,
                "attended": 0,
                "earned_kopecks": 0,
                "debt_kopecks": 0,
                "salary_numerator": 0,
                "unconfigured_salary": False,
            },
        )
        bucket["attended"] += 1
        bucket["earned_kopecks"] += a.value_kopecks
        bucket["debt_kopecks"] += a.debt_kopecks
        if t.salary_bps is None:
            bucket["unconfigured_salary"] = True
        else:
            bucket["salary_numerator"] += a.value_kopecks * t.salary_bps
    for b in buckets.values():
        numerator = b.pop("salary_numerator")
        b["salary_kopecks"] = None if b.pop("unconfigured_salary") else (numerator + 5000) // 10000
    receipts = (
        select(m.Payment)
        .join(m.Group)
        .where(m.Payment.paid_on.between(starts_on, ends_on), m.Payment.voided.is_(False))
    )
    for value, column in [
        (branch_id, m.Group.branch_id),
        (group_id, m.Group.id),
        (athlete_id, m.Payment.athlete_id),
    ]:
        if value:
            receipts = receipts.where(column == value)
    # Payments belong to a group, not to a particular training/coach.
    received = None if coach_id else sum(p.amount_kopecks for p in db.scalars(receipts))
    values = list(buckets.values())
    return {
        "starts_on": starts_on,
        "ends_on": ends_on,
        "received_kopecks": received,
        "earned_kopecks": sum(a.value_kopecks for a, _, _ in rows),
        "debt_kopecks": sum(a.debt_kopecks for a, _, _ in rows),
        "attended": len(rows),
        "salary_kopecks": None
        if any(b["salary_kopecks"] is None for b in values)
        else sum(b["salary_kopecks"] for b in values),
        "items": values,
    }


@api.get("/reports/timesheet", response_model=out.Timesheet, tags=["reports"])
def timesheet(db: DB, group_id: str, year: int = Query(ge=2000, le=2100), month: int = Query(ge=1, le=12)):
    import calendar

    get(db, m.Group, group_id)
    start = date(year, month, 1)
    end = date(year, month, calendar.monthrange(year, month)[1])
    lessons = list(
        db.scalars(
            select(m.Training)
            .where(m.Training.group_id == group_id, m.Training.date.between(start, end))
            .order_by(m.Training.date, m.Training.starts_at)
        )
    )
    enrollments = list(
        db.scalars(
            select(m.Enrollment).where(
                m.Enrollment.group_id == group_id,
                m.Enrollment.starts_on <= end,
                (m.Enrollment.ends_on.is_(None) | (m.Enrollment.ends_on >= start)),
            )
        )
    )
    items = []
    for aid in sorted({e.athlete_id for e in enrollments}):
        athlete = get(db, m.Athlete, aid)
        attendance = list(
            db.scalars(
                select(m.Attendance)
                .join(m.Training)
                .where(
                    m.Attendance.athlete_id == aid,
                    m.Training.group_id == group_id,
                    m.Training.date.between(start, end),
                )
            )
        )
        cancelled = {t.id for t in lessons if t.cancelled}
        present = [a for a in attendance if a.status == "present" and a.training_id not in cancelled]
        items.append(
            {
                "athlete": snapshot(athlete),
                "attendance": [snapshot(a) for a in attendance],
                "attended": len(present),
                "earned_kopecks": sum(a.value_kopecks for a in present),
                "debt_kopecks": sum(a.debt_kopecks for a in present),
                "balance": balance(db, aid, end, group_id),
            }
        )
    return {"trainings": [snapshot(t) for t in lessons], "items": items}


@api.get("/notifications", response_model=out.Page[out.Notification], tags=["notifications"])
def notifications(
    db: DB,
    threshold: int = Query(settings().notification_threshold, ge=0, le=100),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
):
    # Report per enrollment group, because subscriptions cannot cross group boundaries.
    items = []
    pairs = db.execute(
        select(m.Athlete, m.Group, m.Branch)
        .select_from(m.Enrollment)
        .join(m.Athlete)
        .join(m.Group)
        .join(m.Branch)
        .where(m.Athlete.archived.is_(False), m.Group.archived.is_(False), m.Branch.archived.is_(False))
        .distinct()
    ).all()
    for athlete, group, branch in pairs:
        today = datetime.now(ZoneInfo(branch.timezone)).date()
        if not enrolled(db, athlete.id, group.id, today):
            continue
        b = balance(db, athlete.id, today, group.id)
        if b["remaining_lessons"] <= threshold or b["debt_lessons"]:
            items.append(
                {
                    "athlete_id": athlete.id,
                    "group_id": group.id,
                    "name": athlete.name,
                    **b,
                    "kind": "debt" if b["debt_lessons"] else "low_balance",
                }
            )
    items.sort(key=lambda item: (item["athlete_id"], item["group_id"]))
    return {"items": items[offset : offset + limit], "total": len(items), "offset": offset, "limit": limit}


@api.get("/audit", response_model=out.Page[out.Audit], tags=["audit"])
def audit_list(
    db: DB,
    entity: str | None = None,
    entity_id: str | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
):
    stmt = select(m.Audit)
    if entity:
        stmt = stmt.where(m.Audit.entity == entity)
    if entity_id:
        stmt = stmt.where(m.Audit.entity_id == entity_id)
    return {
        "items": [
            snapshot(a)
            for a in db.scalars(
                stmt.order_by(m.Audit.created_at.desc(), m.Audit.id).offset(offset).limit(limit)
            )
        ],
        "total": db.scalar(select(func.count()).select_from(stmt.subquery())),
        "offset": offset,
        "limit": limit,
    }


app.include_router(auth)
app.include_router(api)
