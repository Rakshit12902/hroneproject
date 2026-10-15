"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import calendar
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import json
import os
import re
from typing import Any, Dict, List, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path, status
from pydantic import BaseModel, Field, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


def ensure_indexes():
    """Create all required database indexes idempotently at startup."""
    # Employees collection
    db.employees.create_index([("emp_code", 1)], unique=True)
    db.employees.create_index([("department", 1), ("emp_code", 1)])
    db.employees.create_index([("joined_on", 1), ("department", 1)])

    # Attendance logs collection
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("date", 1), ("late_minutes", 1), ("emp_code", 1)])
    db.attendance_logs.create_index([("status", 1), ("date", -1)])


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Helpers: Time, Rounding, Conversions & Calculations
# --------------------------------------------------------------------------- #
def truncate_ms_to_seconds(ms: int) -> int:
    """R1: Before any calculation, instants are truncated to whole seconds."""
    return (ms // 1000) * 1000


def ms_to_datetime_utc(ms: int) -> datetime:
    """Convert integer epoch ms (truncated to seconds) to UTC datetime."""
    sec = ms // 1000
    return datetime.fromtimestamp(sec, tz=timezone.utc)


def datetime_to_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert BSON datetime to integer epoch ms."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def ensure_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Ensure datetime is UTC aware (handles naive datetimes returned by PyMongo)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def round_half_up(val: Optional[float], places: int = 2) -> Optional[float]:
    """R8: Round reported numbers half-up (not banker's / float rounding)."""
    if val is None:
        return None
    d = Decimal(str(val))
    q = Decimal(10) ** -places
    return float(d.quantize(q, rounding=ROUND_HALF_UP))


def derive_attendance_date(punch_in_dt: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: Attendance date is IST calendar date of punch_in.
    Overnight shift exception (shift_end <= shift_start):
    If punch-in IST time is strictly earlier than shift_end, it belongs to the shift that started the previous day.
    """
    punch_in_ist = punch_in_dt.astimezone(IST)
    is_overnight = shift_end <= shift_start
    h_end, m_end = map(int, shift_end.split(":"))
    if is_overnight:
        punch_minutes = punch_in_ist.hour * 60 + punch_in_ist.minute + (punch_in_ist.second / 60.0)
        end_minutes = h_end * 60 + m_end
        if punch_minutes < end_minutes:
            prev_day = (punch_in_ist - timedelta(days=1)).date()
            return prev_day.isoformat()
    return punch_in_ist.date().isoformat()


def compute_late_minutes(punch_in_dt: datetime, shift_start: str, attendance_date_str: str) -> int:
    """
    R2: Grace period 10 minutes. Late only if strictly > 10:00 (600 seconds) after shift_start.
    When late, whole minutes (floor) since shift_start, not since grace period ended.
    """
    punch_in_ist = punch_in_dt.astimezone(IST)
    h_start, m_start = map(int, shift_start.split(":"))
    d = datetime.fromisoformat(attendance_date_str).date()
    shift_start_dt = datetime(d.year, d.month, d.day, h_start, m_start, 0, tzinfo=IST)

    elapsed_seconds = int((punch_in_ist - shift_start_dt).total_seconds())
    if elapsed_seconds > 600:
        return elapsed_seconds // 60
    return 0


def compute_overtime(punch_out_dt: datetime, shift_start: str, shift_end: str, attendance_date_str: str) -> int:
    """
    R3: Overtime is whole minutes (floor) between shift_end and punch_out,
    counted only if that is >= 30; otherwise 0.
    For overnight shifts (shift_end <= shift_start), shift_end falls on the next calendar day.
    """
    punch_out_ist = punch_out_dt.astimezone(IST)
    h_end, m_end = map(int, shift_end.split(":"))
    d = datetime.fromisoformat(attendance_date_str).date()
    if shift_end <= shift_start:
        d = d + timedelta(days=1)
    shift_end_dt = datetime(d.year, d.month, d.day, h_end, m_end, 0, tzinfo=IST)

    diff_seconds = int((punch_out_ist - shift_end_dt).total_seconds())
    if diff_seconds < 0:
        return 0
    diff_minutes = diff_seconds // 60
    return diff_minutes if diff_minutes >= 30 else 0


def compute_work_hours(punch_in_dt: datetime, punch_out_dt: datetime) -> float:
    """R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up."""
    sec = int((punch_out_dt - punch_in_dt).total_seconds())
    val = Decimal(sec) / Decimal(3600)
    rounded = val.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(rounded)


def format_employee_doc(doc: dict) -> dict:
    """Format employee MongoDB document to Employee schema."""
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": datetime_to_ms(doc.get("created_at")),
    }


def format_attendance_doc(doc: dict) -> dict:
    """Format attendance_logs MongoDB document to AttendanceRecord schema."""
    history_items = []
    for h in doc.get("history", []):
        changes = {}
        for k, v in h.get("changes", {}).items():
            if k in ("punch_in", "punch_out"):
                changes[k] = {
                    "from": datetime_to_ms(v.get("from")) if v.get("from") else None,
                    "to": datetime_to_ms(v.get("to")) if v.get("to") else None,
                }
            else:
                changes[k] = {
                    "from": v.get("from"),
                    "to": v.get("to"),
                }
        history_items.append({
            "at": datetime_to_ms(h["at"]),
            "by": h["by"],
            "reason": h["reason"],
            "changes": changes,
        })

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": datetime_to_ms(doc.get("punch_in")),
        "punch_out": datetime_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history_items,
    }


# --------------------------------------------------------------------------- #
# Pydantic Schemas
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")

    @model_validator(mode="after")
    def validate_rules(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        try:
            datetime.fromisoformat(self.joined_on)
        except ValueError:
            raise ValueError("joined_on must be a valid calendar date")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)


class RegularizeRequest(BaseModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    """Readiness probe: returns 200 only if MongoDB answers a ping, otherwise 503."""
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    """Create an employee. Enforces uniqueness via unique index."""
    now_ms = truncate_ms_to_seconds(int(datetime.now(timezone.utc).timestamp() * 1000))
    created_at_dt = ms_to_datetime_utc(now_ms)
    doc = body.model_dump()
    doc["created_at"] = created_at_dt

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="emp_code already exists")

    return format_employee_doc(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """List employees sorted by emp_code ascending. Total reflects filters."""
    q: Dict[str, Any] = {}
    if department:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q).sort("emp_code", 1).skip(skip).limit(page_size)
    items = [format_employee_doc(d) for d in cursor]

    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    """Record a punch-in. Race-safe via unique compound index."""
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    if body.punched_at is not None:
        ts_ms = truncate_ms_to_seconds(body.punched_at)
    else:
        ts_ms = truncate_ms_to_seconds(int(datetime.now(timezone.utc).timestamp() * 1000))

    punch_in_dt = ms_to_datetime_utc(ts_ms)
    attendance_date = derive_attendance_date(punch_in_dt, emp["shift_start"], emp["shift_end"])
    late_minutes = compute_late_minutes(punch_in_dt, emp["shift_start"], attendance_date)

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": punch_in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="already punched in for this date")

    return format_attendance_doc(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    """
    Record a punch-out. Closes most recent record whose punch_in is at or before punched_at.
    Works across midnight for overnight shifts. Atomic conditional update prevents race conditions.
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        ts_ms = truncate_ms_to_seconds(body.punched_at)
    else:
        ts_ms = truncate_ms_to_seconds(int(datetime.now(timezone.utc).timestamp() * 1000))

    punch_out_dt = ms_to_datetime_utc(ts_ms)

    # Find the most recent record with punch_in <= punched_at
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_dt}},
        sort=[("punch_in", -1)],
    )
    if not record:
        raise HTTPException(status_code=404, detail="No punch-in found")

    punch_in_dt = ensure_utc(record["punch_in"])
    if punch_out_dt <= punch_in_dt:
        raise HTTPException(status_code=422, detail="punched_at must be strictly after punch_in")

    duration_seconds = (punch_out_dt - punch_in_dt).total_seconds()
    if duration_seconds > 24 * 3600:
        raise HTTPException(
            status_code=422,
            detail="punched_at cannot be more than 24 hours after punch_in",
        )

    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="already punched out")

    work_hours = compute_work_hours(punch_in_dt, punch_out_dt)
    overtime_minutes = compute_overtime(punch_out_dt, emp["shift_start"], emp["shift_end"], record["date"])
    half_day = bool(work_hours < 4.50)

    # Atomic conditional update: only update if punch_out is still None
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(status_code=409, detail="already punched out")

    return format_attendance_doc(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """List attendance records sorted by date desc, emp_code asc. Pagination and filtering in DB."""
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from cannot be greater than date_to")

    valid_statuses = {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"}
    if status and status not in valid_statuses:
        raise HTTPException(status_code=422, detail="Invalid status")

    q: Dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.attendance_logs.find(q).sort([("date", -1), ("emp_code", 1)]).skip(skip).limit(page_size)
    items = [format_attendance_doc(d) for d in cursor]

    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    body: RegularizeRequest = ...,
):
    """
    Manual regularisation. Recomputes derived fields and atomically appends to history.
    Optimistic locking prevents concurrent edits from losing history.
    """
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attendance record not found")

    # Target fields determination
    target_status = body.status if body.status is not None else record["status"]
    target_punch_in = ms_to_datetime_utc(truncate_ms_to_seconds(body.punch_in)) if body.punch_in is not None else ensure_utc(record.get("punch_in"))
    target_punch_out = ms_to_datetime_utc(truncate_ms_to_seconds(body.punch_out)) if body.punch_out is not None else ensure_utc(record.get("punch_out"))

    # Business rule validations
    if body.status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Cannot supply punch times when setting status to ABSENT or LEAVE",
            )
        target_punch_in = None
        target_punch_out = None

    if target_status in ("PRESENT", "WFH", "ON_DUTY"):
        if target_punch_in is None:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Presence status requires a punch_in")

        derived_date = derive_attendance_date(target_punch_in, emp["shift_start"], emp["shift_end"])
        if derived_date != date:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"punch_in derives attendance date {derived_date}, which does not match record date {date}",
            )

        if target_punch_out is not None:
            if target_punch_out <= target_punch_in:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="punch_out must be after punch_in")
            if (target_punch_out - target_punch_in).total_seconds() > 24 * 3600:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="punch_out cannot be more than 24 hours after punch_in",
                )

    # Recompute derived fields
    if target_status in ("ABSENT", "LEAVE"):
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        new_late_minutes = compute_late_minutes(target_punch_in, emp["shift_start"], date)
        if target_punch_out is not None:
            new_work_hours = compute_work_hours(target_punch_in, target_punch_out)
            new_overtime_minutes = compute_overtime(target_punch_out, emp["shift_start"], emp["shift_end"], date)
            new_half_day = bool(new_work_hours < 4.50)
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

    # Calculate diff
    changes_for_db: Dict[str, dict] = {}
    if target_status != record.get("status"):
        changes_for_db["status"] = {"from": record.get("status"), "to": target_status}
    if target_punch_in != ensure_utc(record.get("punch_in")):
        changes_for_db["punch_in"] = {"from": record.get("punch_in"), "to": target_punch_in}
    if target_punch_out != ensure_utc(record.get("punch_out")):
        changes_for_db["punch_out"] = {"from": record.get("punch_out"), "to": target_punch_out}
    if new_work_hours != record.get("work_hours"):
        changes_for_db["work_hours"] = {"from": record.get("work_hours"), "to": new_work_hours}
    if new_late_minutes != record.get("late_minutes", 0):
        changes_for_db["late_minutes"] = {"from": record.get("late_minutes", 0), "to": new_late_minutes}
    if new_overtime_minutes != record.get("overtime_minutes", 0):
        changes_for_db["overtime_minutes"] = {"from": record.get("overtime_minutes", 0), "to": new_overtime_minutes}
    if new_half_day != record.get("half_day", False):
        changes_for_db["half_day"] = {"from": record.get("half_day", False), "to": new_half_day}

    if not changes_for_db:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="No fields changed")

    now_ms = truncate_ms_to_seconds(int(datetime.now(timezone.utc).timestamp() * 1000))
    history_entry = {
        "at": ms_to_datetime_utc(now_ms),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes_for_db,
    }

    # Optimistic locking: check current history array size to avoid concurrent race conditions
    current_history_len = len(record.get("history", []))
    update_filter = {
        "_id": record["_id"],
        f"history.{current_history_len}": {"$exists": False},
    }

    set_fields = {
        "status": target_status,
        "punch_in": target_punch_in,
        "punch_out": target_punch_out,
        "work_hours": new_work_hours,
        "late_minutes": new_late_minutes,
        "overtime_minutes": new_overtime_minutes,
        "half_day": new_half_day,
    }

    updated = db.attendance_logs.find_one_and_update(
        update_filter,
        {
            "$set": set_fields,
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Conflict: record was concurrently modified",
        )

    return format_attendance_doc(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    """
    Monthly attendance summary for one employee via MongoDB aggregation pipeline.
    R6/R7 working days, mid-month joiner handling, present_days Mon-Fri only.
    """
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{num_days:02d}"

    pipeline = [
        {"$match": {"emp_code": emp_code}},
        # Generate all calendar days of the month
        {"$addFields": {
            "all_days": {
                "$map": {
                    "input": {"$range": [1, num_days + 1]},
                    "as": "d",
                    "in": {
                        "$dateFromString": {
                            "dateString": {
                                "$concat": [
                                    month,
                                    "-",
                                    {"$cond": [{"$lt": ["$$d", 10]}, {"$concat": ["0", {"$toString": "$$d"}]}, {"$toString": "$$d"}]}
                                ]
                            }
                        }
                    }
                }
            }
        }},
        # R7: working_days = Mon-Fri calendar days >= joined_on
        {"$addFields": {
            "working_days": {
                "$size": {
                    "$filter": {
                        "input": "$all_days",
                        "as": "dt",
                        "cond": {
                            "$and": [
                                {"$lte": [{"$isoDayOfWeek": "$$dt"}, 5]},
                                {"$gte": [{"$dateToString": {"format": "%Y-%m-%d", "date": "$$dt"}}, "$joined_on"]}
                            ]
                        }
                    }
                }
            }
        }},
        # Lookup attendance logs for this month
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"emp": "$emp_code"},
            "pipeline": [
                {"$match": {
                    "$expr": {
                        "$and": [
                            {"$eq": ["$emp_code", "$$emp"]},
                            {"$gte": ["$date", month_start]},
                            {"$lte": ["$date", month_end]}
                        ]
                    }
                }},
                {"$addFields": {
                    "dow": {"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}}
                }}
            ],
            "as": "logs"
        }},
        # Aggregate metrics
        {"$project": {
            "_id": 0,
            "emp_code": 1,
            "month": {"$literal": month},
            "working_days": 1,
            # R7: present_days only counts Mon-Fri records
            "present_days": {
                "$reduce": {
                    "input": "$logs",
                    "initialValue": 0.0,
                    "in": {
                        "$add": [
                            "$$value",
                            {"$cond": [
                                {"$and": [
                                    {"$in": ["$$this.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$lte": ["$$this.dow", 5]}
                                ]},
                                {"$cond": [{"$eq": ["$$this.half_day", True]}, 0.5, 1.0]},
                                0.0
                            ]}
                        ]
                    }
                }
            },
            "leave_days": {
                "$size": {
                    "$filter": {
                        "input": "$logs",
                        "cond": {"$eq": ["$$this.status", "LEAVE"]}
                    }
                }
            },
            "late_count": {
                "$size": {
                    "$filter": {
                        "input": "$logs",
                        "cond": {"$gt": ["$$this.late_minutes", 0]}
                    }
                }
            },
            "total_late_minutes": {"$sum": "$logs.late_minutes"},
            "total_overtime_minutes": {"$sum": "$logs.overtime_minutes"}
        }},
        # Attendance percentage: R8 half-up rounding
        {"$addFields": {
            "attendance_pct": {
                "$cond": [
                    {"$gt": ["$working_days", 0]},
                    {
                        "$divide": [
                            {
                                "$floor": {
                                    "$add": [
                                        {"$multiply": [{"$divide": ["$present_days", "$working_days"]}, 10000]},
                                        0.5
                                    ]
                                }
                            },
                            100.0
                        ]
                    },
                    None
                ]
            }
        }}
    ]

    res = list(db.employees.aggregate(pipeline))
    if not res:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    return res[0]


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    """
    Department-level attendance summary for a month via MongoDB aggregation.
    R9: Headcount includes zero-log employees, excludes future joiners.
    avg_work_hours is record-level average.
    """
    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{num_days:02d}"

    match_stage: Dict[str, Any] = {"joined_on": {"$lte": month_end}}
    if department:
        match_stage["department"] = department

    pipeline = [
        {"$match": match_stage},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"emp": "$emp_code"},
            "pipeline": [
                {"$match": {
                    "$expr": {
                        "$and": [
                            {"$eq": ["$emp_code", "$$emp"]},
                            {"$gte": ["$date", month_start]},
                            {"$lte": ["$date", month_end]}
                        ]
                    }
                }},
                {"$addFields": {
                    "dow": {"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}}
                }}
            ],
            "as": "logs"
        }},
        {"$project": {
            "department": 1,
            "emp_present_days": {
                "$reduce": {
                    "input": "$logs",
                    "initialValue": 0.0,
                    "in": {
                        "$add": [
                            "$$value",
                            {"$cond": [
                                {"$and": [
                                    {"$in": ["$$this.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$lte": ["$$this.dow", 5]}
                                ]},
                                {"$cond": [{"$eq": ["$$this.half_day", True]}, 0.5, 1.0]},
                                0.0
                            ]}
                        ]
                    }
                }
            },
            "emp_work_hours_sum": {
                "$reduce": {
                    "input": "$logs",
                    "initialValue": 0.0,
                    "in": {
                        "$add": [
                            "$$value",
                            {"$cond": [
                                {"$and": [
                                    {"$in": ["$$this.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$$this.work_hours", None]}
                                ]},
                                "$$this.work_hours",
                                0.0
                            ]}
                        ]
                    }
                }
            },
            "emp_work_hours_count": {
                "$size": {
                    "$filter": {
                        "input": "$logs",
                        "cond": {
                            "$and": [
                                {"$in": ["$$this.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$ne": ["$$this.work_hours", None]}
                            ]
                        }
                    }
                }
            },
            "emp_late_count": {
                "$size": {
                    "$filter": {
                        "input": "$logs",
                        "cond": {"$gt": ["$$this.late_minutes", 0]}
                    }
                }
            },
            "emp_total_late_minutes": {"$sum": "$logs.late_minutes"},
            "emp_leave_count": {
                "$size": {
                    "$filter": {
                        "input": "$logs",
                        "cond": {"$eq": ["$$this.status", "LEAVE"]}
                    }
                }
            },
            "emp_on_duty_count": {
                "$size": {
                    "$filter": {
                        "input": "$logs",
                        "cond": {"$eq": ["$$this.status", "ON_DUTY"]}
                    }
                }
            }
        }},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": "$emp_present_days"},
            "total_work_hours": {"$sum": "$emp_work_hours_sum"},
            "total_work_hours_count": {"$sum": "$emp_work_hours_count"},
            "late_count": {"$sum": "$emp_late_count"},
            "total_late_minutes": {"$sum": "$emp_total_late_minutes"},
            "leave_count": {"$sum": "$emp_leave_count"},
            "on_duty_count": {"$sum": "$emp_on_duty_count"}
        }},
        {"$sort": {"_id": 1}},
        {"$project": {
            "_id": 0,
            "department": "$_id",
            "headcount": 1,
            "present_days": 1,
            "avg_work_hours": {
                "$cond": [
                    {"$gt": ["$total_work_hours_count", 0]},
                    {
                        "$divide": [
                            {
                                "$floor": {
                                    "$add": [
                                        {"$multiply": [{"$divide": ["$total_work_hours", "$total_work_hours_count"]}, 100]},
                                        0.5
                                    ]
                                }
                            },
                            100.0
                        ]
                    },
                    None
                ]
            },
            "late_count": 1,
            "total_late_minutes": 1,
            "leave_count": 1,
            "on_duty_count": 1
        }}
    ]

    items = list(db.employees.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    """
    Late-comers leaderboard for a month. Standard competition ranking (1, 2, 2, 4).
    limit applied after ranking. Tied rows ordered by emp_code.
    """
    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{num_days:02d}"

    pipeline: List[Dict[str, Any]] = [
        {"$match": {
            "date": {"$gte": month_start, "$lte": month_end},
            "late_minutes": {"$gt": 0}
        }},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {"$sum": 1}
        }},
        {"$lookup": {
            "from": "employees",
            "localField": "_id",
            "foreignField": "emp_code",
            "as": "emp"
        }},
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {
                "rank": {"$rank": {}}
            }
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        {"$project": {
            "_id": 0,
            "rank": 1,
            "emp_code": "$_id",
            "name": "$emp.name",
            "department": "$emp.department",
            "total_late_minutes": 1,
            "late_count": 1
        }}
    ])

    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str = Path(...),
    from_: str = Query(..., alias="from", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    to: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
):
    """
    Daily attendance trend for a department via $densify gap-filling and 7-day moving average.
    Zero Python loop for calendar days.
    """
    # 404 if department has no employees
    dept_emps = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1}))
    if not dept_emps:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Department not found")

    try:
        from_d = datetime.fromisoformat(from_).date()
        to_d = datetime.fromisoformat(to).date()
    except ValueError:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid date format")

    if to_d < from_d:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="'to' date must be >= 'from' date")

    day_count = (to_d - from_d).days + 1
    if day_count > 92:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Date range cannot exceed 92 days")

    dept_emp_codes = [e["emp_code"] for e in dept_emps]
    dept_joined_on_list = [e["joined_on"] for e in dept_emps]

    from_dt = datetime(from_d.year, from_d.month, from_d.day, tzinfo=timezone.utc)
    to_dt = datetime(to_d.year, to_d.month, to_d.day, tzinfo=timezone.utc)
    bounds = [from_dt, to_dt + timedelta(days=1)]

    pipeline = [
        {"$match": {
            "emp_code": {"$in": dept_emp_codes},
            "date": {"$gte": from_, "$lte": to}
        }},
        {"$project": {
            "date_dt": {"$dateFromString": {"dateString": "$date"}},
            "status": 1,
            "half_day": 1,
            "late_minutes": 1,
            "is_seed": {"$literal": False}
        }},
        # Ensure at least 1 document exists so $densify always generates all days from to
        {"$unionWith": {
            "pipeline": [
                {"$documents": [{"date_dt": from_dt, "is_seed": True}]}
            ]
        }},
        {"$densify": {
            "field": "date_dt",
            "range": {
                "step": 1,
                "unit": "day",
                "bounds": bounds
            }
        }},
        {"$project": {
            "date_str": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}},
            "dow": {"$isoDayOfWeek": "$date_dt"},
            "is_seed": {"$ifNull": ["$is_seed", False]},
            "status": 1,
            "half_day": 1,
            "late_minutes": 1
        }},
        {"$group": {
            "_id": "$date_str",
            "dow": {"$first": "$dow"},
            "present_count": {
                "$sum": {
                    "$cond": [
                        {"$and": [
                            {"$ne": ["$is_seed", True]},
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}
                        ]},
                        {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                        0.0
                    ]
                }
            },
            "late_count": {
                "$sum": {
                    "$cond": [
                        {"$and": [
                            {"$ne": ["$is_seed", True]},
                            {"$gt": ["$late_minutes", 0]}
                        ]},
                        1,
                        0
                    ]
                }
            }
        }},
        {"$sort": {"_id": 1}},
        {"$addFields": {
            "date": "$_id",
            "is_working_day": {"$lte": ["$dow", 5]},
            "headcount": {
                "$size": {
                    "$filter": {
                        "input": dept_joined_on_list,
                        "cond": {"$lte": ["$$this", "$_id"]}
                    }
                }
            }
        }},
        {"$addFields": {
            "attendance_rate": {
                "$cond": [
                    {"$and": [
                        {"$eq": ["$is_working_day", True]},
                        {"$gt": ["$headcount", 0]}
                    ]},
                    {
                        "$divide": [
                            {
                                "$floor": {
                                    "$add": [
                                        {"$multiply": [{"$divide": ["$present_count", "$headcount"]}, 10000]},
                                        0.5
                                    ]
                                }
                            },
                            10000.0
                        ]
                    },
                    None
                ]
            }
        }},
        {"$setWindowFields": {
            "sortBy": {"date": 1},
            "output": {
                "raw_moving_avg": {
                    "$avg": "$attendance_rate",
                    "window": {
                        "documents": [-6, "current"]
                    }
                }
            }
        }},
        {"$addFields": {
            "moving_avg_7d": {
                "$cond": [
                    {"$ne": ["$raw_moving_avg", None]},
                    {
                        "$divide": [
                            {
                                "$floor": {
                                    "$add": [
                                        {"$multiply": ["$raw_moving_avg", 10000]},
                                        0.5
                                    ]
                                }
                            },
                            10000.0
                        ]
                    },
                    None
                ]
            }
        }},
        {"$project": {
            "_id": 0,
            "date": 1,
            "is_working_day": 1,
            "headcount": 1,
            "present_count": 1,
            "late_count": 1,
            "attendance_rate": 1,
            "moving_avg_7d": 1
        }}
    ]

    items = list(db.attendance_logs.aggregate(pipeline))
    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str = Path(...),
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = 10,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_: Optional[str] = Query(default=None, alias="from"),
    to: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
):
    """
    MongoDB explain for an endpoint's main query with executionStats verbosity.
    Accepts the same query parameters as the explained endpoint.
    """
    allowed_endpoints = {
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    }
    if endpoint not in allowed_endpoints:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown endpoint '{endpoint}'. Allowed: {sorted(allowed_endpoints)}",
        )

    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from > date_to")
        q: Dict[str, Any] = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status:
            q["status"] = status

        skip = (page - 1) * page_size
        find_cmd = {
            "find": "attendance_logs",
            "filter": q,
            "sort": {"date": -1, "emp_code": 1},
            "skip": skip,
            "limit": page_size,
        }
        explain_doc = db.command("explain", find_cmd, verbosity="executionStats")
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": json.loads(json_util.dumps(explain_doc)),
        }

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month are required for employee_monthly")
        if not re.match(r"^\d{4}-(0[1-9]|1[0-2])$", month):
            raise HTTPException(status_code=422, detail="Invalid month format (YYYY-MM)")

        year, m = map(int, month.split("-"))
        num_days = calendar.monthrange(year, m)[1]
        month_start = f"{month}-01"
        month_end = f"{month}-{num_days:02d}"

        pipeline = [
            {"$match": {"emp_code": emp_code}},
            {"$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {"$match": {
                        "$expr": {
                            "$and": [
                                {"$eq": ["$emp_code", "$$emp"]},
                                {"$gte": ["$date", month_start]},
                                {"$lte": ["$date", month_end]}
                            ]
                        }
                    }}
                ],
                "as": "logs"
            }}
        ]
        explain_doc = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": json.loads(json_util.dumps(explain_doc)),
        }

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for department_summary")
        if not re.match(r"^\d{4}-(0[1-9]|1[0-2])$", month):
            raise HTTPException(status_code=422, detail="Invalid month format (YYYY-MM)")

        year, m = map(int, month.split("-"))
        num_days = calendar.monthrange(year, m)[1]
        month_end = f"{month}-{num_days:02d}"

        match_stage: Dict[str, Any] = {"joined_on": {"$lte": month_end}}
        if department:
            match_stage["department"] = department

        pipeline = [
            {"$match": match_stage},
            {"$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "logs"
            }}
        ]
        explain_doc = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": json.loads(json_util.dumps(explain_doc)),
        }

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for late_leaderboard")
        if not re.match(r"^\d{4}-(0[1-9]|1[0-2])$", month):
            raise HTTPException(status_code=422, detail="Invalid month format (YYYY-MM)")

        year, m = map(int, month.split("-"))
        num_days = calendar.monthrange(year, m)[1]
        month_start = f"{month}-01"
        month_end = f"{month}-{num_days:02d}"

        pipeline = [
            {"$match": {
                "date": {"$gte": month_start, "$lte": month_end},
                "late_minutes": {"$gt": 0}
            }},
            {"$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1}
            }},
            {"$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp"
            }}
        ]
        explain_doc = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": json.loads(json_util.dumps(explain_doc)),
        }

    elif endpoint == "department_trend":
        if not department or not from_ or not to:
            raise HTTPException(status_code=422, detail="department, from, and to are required for department_trend")

        dept_emps = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1}))
        dept_emp_codes = [e["emp_code"] for e in dept_emps]

        pipeline = [
            {"$match": {
                "emp_code": {"$in": dept_emp_codes},
                "date": {"$gte": from_, "$lte": to}
            }}
        ]
        explain_doc = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": json.loads(json_util.dumps(explain_doc)),
        }
