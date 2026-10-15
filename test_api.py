import calendar
from datetime import datetime, timezone, timedelta
import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient

from app.main import app, db, ensure_indexes

client = TestClient(app)


@pytest.fixture(autouse=True)
def seed_data():
    ensure_indexes()


# --------------------------------------------------------------------------- #
# 1. Health Probe
# --------------------------------------------------------------------------- #
def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# --------------------------------------------------------------------------- #
# 2. Employees Endpoints
# --------------------------------------------------------------------------- #
def test_create_and_list_employees():
    emp_payload = {
        "emp_code": "EMP9999",
        "name": "Test User",
        "email": "test@example.com",
        "department": "QA",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-01",
    }
    db.employees.delete_one({"emp_code": "EMP9999"})
    db.attendance_logs.delete_many({"emp_code": "EMP9999"})

    res = client.post("/employees", json=emp_payload)
    assert res.status_code == 201
    data = res.json()
    assert data["emp_code"] == "EMP9999"
    assert isinstance(data["created_at"], int)
    assert "_id" not in data
    assert "id" not in data

    # Duplicate emp_code must return 409
    res_dup = client.post("/employees", json=emp_payload)
    assert res_dup.status_code == 409

    # Shift start == shift end must return 422
    bad_payload = emp_payload.copy()
    bad_payload["emp_code"] = "EMP9998"
    bad_payload["shift_end"] = "09:30"
    res_bad = client.post("/employees", json=bad_payload)
    assert res_bad.status_code == 422

    # Pagination: page 1 must NOT skip first items
    res_page1 = client.get("/employees?page=1&page_size=2")
    assert res_page1.status_code == 200
    p1_data = res_page1.json()
    assert len(p1_data["items"]) == 2
    assert p1_data["total"] >= 6
    assert p1_data["page"] == 1

    # Filter by department
    res_dept = client.get("/employees?department=QA")
    assert res_dept.status_code == 200
    dept_data = res_dept.json()
    assert dept_data["total"] == 1
    assert dept_data["items"][0]["emp_code"] == "EMP9999"


# --------------------------------------------------------------------------- #
# 3. Punch-In Endpoints & Business Rules R1, R2
# --------------------------------------------------------------------------- #
def test_punch_in():
    # Nonexistent employee -> 404
    res_404 = client.post("/attendance/punch-in", json={"emp_code": "EMP0000"})
    assert res_404.status_code == 404

    # Invalid timestamp (seconds-looking) -> 422
    res_422 = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": 1783312500})
    assert res_422.status_code == 422

    # Invalid status -> 422
    res_bad_status = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "status": "ABSENT"})
    assert res_bad_status.status_code == 422

    # Test R2 Late calculations:
    # EMP0001 has shift_start 09:30 IST.
    # 2026-07-20 09:40:00 IST = 2026-07-20 04:10:00 UTC
    dt_on_time = datetime(2026, 7, 20, 4, 10, 0, tzinfo=timezone.utc)
    ms_on_time = int(dt_on_time.timestamp() * 1000)

    db.attendance_logs.delete_one({"emp_code": "EMP0001", "date": "2026-07-20"})

    res_in = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms_on_time})
    assert res_in.status_code == 201
    rec = res_in.json()
    assert rec["date"] == "2026-07-20"
    assert rec["late_minutes"] == 0
    assert rec["work_hours"] is None
    assert rec["overtime_minutes"] == 0
    assert rec["half_day"] is False
    assert rec["history"] == []

    # Duplicate punch in on same day -> 409
    res_dup = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms_on_time})
    assert res_dup.status_code == 409

    # Test late strictly > 600s: 09:40:01 IST = 04:10:01 UTC -> 10 minutes late
    db.attendance_logs.delete_one({"emp_code": "EMP0001", "date": "2026-07-21"})
    dt_late = datetime(2026, 7, 21, 4, 10, 1, tzinfo=timezone.utc)
    ms_late = int(dt_late.timestamp() * 1000)
    res_late = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms_late})
    assert res_late.status_code == 201
    assert res_late.json()["late_minutes"] == 10

    # Test 10:15:59 IST = 04:45:59 UTC -> 45 minutes late
    db.attendance_logs.delete_one({"emp_code": "EMP0001", "date": "2026-07-22"})
    dt_late45 = datetime(2026, 7, 22, 4, 45, 59, tzinfo=timezone.utc)
    ms_late45 = int(dt_late45.timestamp() * 1000)
    res_late45 = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms_late45})
    assert res_late45.status_code == 201
    assert res_late45.json()["late_minutes"] == 45


# --------------------------------------------------------------------------- #
# 4. Overnight Shift Punch-In and Punch-Out (R1, R5)
# --------------------------------------------------------------------------- #
def test_overnight_shift():
    # EMP0005: shift 22:00 - 06:00 (Support)
    # Punch in on 2026-07-25 at 21:55 IST = 16:25 UTC
    db.attendance_logs.delete_one({"emp_code": "EMP0005", "date": "2026-07-25"})
    dt_in = datetime(2026, 7, 25, 16, 25, 0, tzinfo=timezone.utc)
    res_in = client.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": int(dt_in.timestamp() * 1000)})
    assert res_in.status_code == 201
    assert res_in.json()["date"] == "2026-07-25"

    # Punch out on next calendar day at 06:40 IST (01:10 UTC on 2026-07-26)
    # Shift end is 06:00 IST on 2026-07-26. 40 minutes overtime >= 30 min -> 40
    # Duration: 8 hours 45 minutes = 8.75 hours
    dt_out = datetime(2026, 7, 26, 1, 10, 0, tzinfo=timezone.utc)
    res_out = client.post("/attendance/punch-out", json={"emp_code": "EMP0005", "punched_at": int(dt_out.timestamp() * 1000)})
    assert res_out.status_code == 200
    rec = res_out.json()
    assert rec["date"] == "2026-07-25"
    assert rec["work_hours"] == 8.75
    assert rec["overtime_minutes"] == 40
    assert rec["half_day"] is False

    # Also test overnight shift punch-in after midnight:
    # Punch in at 00:05 IST on 2026-07-28 (18:35 UTC on 2026-07-27).
    # Since 00:05 IST < 06:00 IST shift_end, R1 dictates it belongs to shift of previous day: 2026-07-27!
    db.attendance_logs.delete_one({"emp_code": "EMP0005", "date": "2026-07-27"})
    dt_post_midnight = datetime(2026, 7, 27, 18, 35, 0, tzinfo=timezone.utc)
    res_post = client.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": int(dt_post_midnight.timestamp() * 1000)})
    assert res_post.status_code == 201
    assert res_post.json()["date"] == "2026-07-27"
    # Shift start was 2026-07-27 22:00 IST. 00:05 IST is 125 minutes late!
    assert res_post.json()["late_minutes"] == 125


# --------------------------------------------------------------------------- #
# 5. Punch-Out Endpoints & Edge Cases
# --------------------------------------------------------------------------- #
def test_punch_out():
    dt_out = datetime(2026, 7, 20, 13, 5, 0, tzinfo=timezone.utc)
    ms_out = int(dt_out.timestamp() * 1000)

    res_out = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms_out})
    assert res_out.status_code == 200
    out_rec = res_out.json()
    assert out_rec["work_hours"] == 8.92
    assert out_rec["overtime_minutes"] == 0  # 5 min < 30 min minimum
    assert out_rec["half_day"] is False

    # Second punch out -> 409 already punched out
    res_out_dup = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms_out})
    assert res_out_dup.status_code == 409

    # Timestamp before any punch-in for employee -> 404
    dt_ancient = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    res_404_time = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": int(dt_ancient.timestamp() * 1000)})
    assert res_404_time.status_code == 404

    # Exact same timestamp as punch_in -> 422
    db.attendance_logs.delete_one({"emp_code": "EMP0001", "date": "2026-07-23"})
    dt_in = datetime(2026, 7, 23, 4, 0, 0, tzinfo=timezone.utc)
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": int(dt_in.timestamp() * 1000)})

    res_same = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": int(dt_in.timestamp() * 1000)})
    assert res_same.status_code == 422  # punched_at <= punch_in -> 422

    # More than 24 hours after punch-in -> 422
    dt_too_late = dt_in + timedelta(hours=25)
    res_too_late = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": int(dt_too_late.timestamp() * 1000)})
    assert res_too_late.status_code == 422


# --------------------------------------------------------------------------- #
# 6. List Attendance
# --------------------------------------------------------------------------- #
def test_list_attendance():
    # date_from > date_to -> 422
    res_422 = client.get("/attendance?date_from=2026-07-10&date_to=2026-07-01")
    assert res_422.status_code == 422

    res = client.get("/attendance?page=1&page_size=10")
    assert res.status_code == 200
    data = res.json()
    assert "items" in data
    assert data["total"] > 0
    # Check sorted date desc
    dates = [item["date"] for item in data["items"]]
    assert dates == sorted(dates, reverse=True)


# --------------------------------------------------------------------------- #
# 7. Regularize Attendance (PATCH) & History Audit Trail
# --------------------------------------------------------------------------- #
def test_regularize_attendance():
    # EMP0001 on 2026-07-20
    rec = db.attendance_logs.find_one({"emp_code": "EMP0001", "date": "2026-07-20"})
    assert rec is not None

    # Change punch-in to 09:28 IST (03:58 UTC)
    new_in_dt = datetime(2026, 7, 20, 3, 58, 0, tzinfo=timezone.utc)
    new_in_ms = int(new_in_dt.timestamp() * 1000)

    patch_payload = {
        "punch_in": new_in_ms,
        "reason": "biometric glitch",
        "regularized_by": "hr.admin",
    }
    res_patch = client.patch("/attendance/EMP0001/2026-07-20", json=patch_payload)
    assert res_patch.status_code == 200
    p_data = res_patch.json()
    assert len(p_data["history"]) >= 1
    h = p_data["history"][-1]
    assert h["by"] == "hr.admin"
    assert h["reason"] == "biometric glitch"
    assert "punch_in" in h["changes"]
    assert "work_hours" in h["changes"]
    assert isinstance(h["at"], int)

    # Request that changes nothing -> 422
    res_noop = client.patch("/attendance/EMP0001/2026-07-20", json=patch_payload)
    assert res_noop.status_code == 422

    # Regularize to LEAVE with punch times supplied -> 422
    res_bad_leave = client.patch("/attendance/EMP0001/2026-07-20", json={
        "status": "LEAVE",
        "punch_in": new_in_ms,
        "reason": "approved leave",
        "regularized_by": "hr.admin"
    })
    assert res_bad_leave.status_code == 422


# --------------------------------------------------------------------------- #
# 8. Analytics: Employee Monthly, Mid-Month Joiners & Weekends
# --------------------------------------------------------------------------- #
def test_employee_monthly():
    res = client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["emp_code"] == "EMP0001"
    assert data["month"] == "2026-07"
    assert data["working_days"] == 23
    assert data["present_days"] >= 2.0
    assert "attendance_pct" in data

    # Mid-month joiner R7: EMP0002 joined on 2026-07-13.
    # July 2026 weekdays from 2026-07-13 to 2026-07-31:
    # 13-17 (5) + 20-24 (5) + 27-31 (5) = 15 working days!
    res_mid = client.get("/analytics/employees/EMP0002/monthly?month=2026-07")
    assert res_mid.status_code == 200
    assert res_mid.json()["working_days"] == 15

    # Unknown employee -> 404
    res_404 = client.get("/analytics/employees/EMP9990/monthly?month=2026-07")
    assert res_404.status_code == 404


# --------------------------------------------------------------------------- #
# 9. Analytics: Department Summary
# --------------------------------------------------------------------------- #
def test_department_summary():
    res = client.get("/analytics/departments/summary?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["month"] == "2026-07"
    depts = {item["department"]: item for item in data["items"]}
    assert "Engineering" in depts
    assert "Sales" in depts
    assert "Support" in depts
    assert depts["Engineering"]["headcount"] >= 2
    assert depts["Engineering"]["avg_work_hours"] is not None


# --------------------------------------------------------------------------- #
# 10. Analytics: Late Leaderboard & Ties
# --------------------------------------------------------------------------- #
def test_late_leaderboard():
    res = client.get("/analytics/leaderboard/late?month=2026-07&limit=5")
    assert res.status_code == 200
    data = res.json()
    assert data["month"] == "2026-07"
    items = data["items"]
    if items:
        assert items[0]["rank"] == 1
        assert items[0]["total_late_minutes"] > 0


# --------------------------------------------------------------------------- #
# 11. Analytics: Department Trend
# --------------------------------------------------------------------------- #
def test_department_trend():
    res = client.get("/analytics/departments/Sales/trend?from=2026-07-06&to=2026-07-09")
    assert res.status_code == 200
    data = res.json()
    assert data["department"] == "Sales"
    items = data["items"]
    assert len(items) == 4
    dates = [i["date"] for i in items]
    assert dates == ["2026-07-06", "2026-07-07", "2026-07-08", "2026-07-09"]
    assert items[0]["attendance_rate"] == 0.5
    assert items[0]["moving_avg_7d"] == 0.5


# --------------------------------------------------------------------------- #
# 12. Admin Explain Endpoint & Index Scan Verification
# --------------------------------------------------------------------------- #
def test_admin_explain():
    endpoints = [
        ("attendance_list", {"page": 1, "page_size": 10}),
        ("employee_monthly", {"emp_code": "EMP0001", "month": "2026-07"}),
        ("department_summary", {"month": "2026-07"}),
        ("late_leaderboard", {"month": "2026-07"}),
        ("department_trend", {"department": "Sales", "from": "2026-07-06", "to": "2026-07-09"}),
    ]
    for ep, params in endpoints:
        res = client.get(f"/admin/explain/{ep}", params=params)
        assert res.status_code == 200, f"Explain failed for {ep}: {res.text}"
        data = res.json()
        assert data["endpoint"] == ep
        assert "collection" in data
        assert "explain" in data
        raw_str = str(data["explain"])
        # Verify IXSCAN is utilized
        assert "IXSCAN" in raw_str, f"IXSCAN not found in explain for {ep}"
