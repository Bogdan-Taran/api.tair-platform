from datetime import date

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from sqlalchemy import inspect, select

from app.models import Athlete, Attendance, Audit, Enrollment, Payment, Training


def snapshot(obj):
    return jsonable_encoder(
        {c.key: getattr(obj, c.key) for c in inspect(obj).mapper.column_attrs if not c.columns[0].computed}
    )


def get(db, model, entity_id, lock=False, active=False):
    statement = select(model).where(model.id == entity_id)
    if lock:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    obj = db.scalar(statement)
    if obj is None:
        raise HTTPException(404, f"{model.__name__} not found")
    if active and getattr(obj, "archived", False):
        raise HTTPException(409, f"{model.__name__} is archived")
    return obj


def audit(db, user, obj, before, reason):
    db.flush()
    db.add(
        Audit(
            user_id=user.id,
            entity=obj.__tablename__,
            entity_id=obj.id,
            reason=reason,
            before=before,
            after=snapshot(obj),
        )
    )


def enrolled(db, athlete_id, group_id, on):
    return (
        db.scalar(
            select(Enrollment.id).where(
                Enrollment.athlete_id == athlete_id,
                Enrollment.group_id == group_id,
                Enrollment.starts_on <= on,
                (Enrollment.ends_on.is_(None) | (Enrollment.ends_on >= on)),
            )
        )
        is not None
    )


def recalculate(db, athlete_id):
    """Caller holds athlete row lock. Rebuild allocations in chronological FIFO order.

    Purchases cover only attendance on/after paid_on in the same group. Old debt
    remains explicit. Cumulative integer division allocates every last kopeck.
    """
    db.flush()
    payments = list(
        db.scalars(
            select(Payment)
            .where(Payment.athlete_id == athlete_id, Payment.voided.is_(False))
            .order_by(Payment.paid_on, Payment.created_at, Payment.id)
        )
    )
    used = {p.id: 0 for p in payments}
    rows = db.execute(
        select(Attendance, Training)
        .join(Training)
        .where(Attendance.athlete_id == athlete_id)
        .order_by(Training.date, Training.starts_at, Training.id)
    ).all()
    for att, training in rows:
        att.payment_id, att.value_kopecks, att.debt_kopecks = None, 0, 0
        if att.status != "present" or training.cancelled:
            continue
        payment = next(
            (
                p
                for p in payments
                if p.group_id == training.group_id and p.paid_on <= training.date and used[p.id] < p.lessons
            ),
            None,
        )
        if payment:
            n = used[payment.id]
            att.payment_id = payment.id
            att.value_kopecks = (
                n + 1
            ) * payment.amount_kopecks // payment.lessons - n * payment.amount_kopecks // payment.lessons
            used[payment.id] += 1
        else:
            att.debt_kopecks = att.single_price_kopecks
    db.flush()


def balance(db, athlete_id, on=None, group_id=None):
    on = on or date.max
    payments = list(
        db.scalars(
            select(Payment).where(
                Payment.athlete_id == athlete_id, Payment.voided.is_(False), Payment.paid_on <= on
            )
        )
    )
    if group_id:
        payments = [p for p in payments if p.group_id == group_id]
    rows = db.execute(
        select(Attendance, Training)
        .join(Training)
        .where(
            Attendance.athlete_id == athlete_id,
            Training.date <= on,
            Training.cancelled.is_(False),
            Attendance.status == "present",
        )
    ).all()
    if group_id:
        rows = [(a, t) for a, t in rows if t.group_id == group_id]
    spent = sum(a.payment_id is not None for a, _ in rows)
    debt = [a for a, _ in rows if a.payment_id is None]
    return {
        "remaining_lessons": sum(p.lessons for p in payments) - spent,
        "debt_lessons": len(debt),
        "debt_kopecks": sum(a.debt_kopecks for a in debt),
        "attended": len(rows),
        "earned_kopecks": sum(a.value_kopecks for a, _ in rows),
    }


def lock_athletes(db, ids):
    for athlete_id in sorted(set(ids)):
        get(db, Athlete, athlete_id, lock=True)
