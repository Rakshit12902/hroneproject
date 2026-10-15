# DECISIONS.md

1. **Indexes.**
On `employees`: unique `{"emp_code": 1}` enforcing business key uniqueness and O(1) lookups; compound `{"department": 1, "emp_code": 1}` for filtered listing; `{"joined_on": 1, "department": 1}` for headcount evaluation.
On `attendance_logs`: unique `{"emp_code": 1, "date": 1}` guaranteeing R1 single daily record and monthly lookups; `{"emp_code": 1, "punch_in": -1}` for punch-out resolution; `{"date": -1, "emp_code": 1}` for paginated listings; `{"date": 1, "late_minutes": 1, "emp_code": 1}` for covered late leaderboard filtering. We considered and rejected a standalone index on `date`, because our compound indexes prefixed by `date` already satisfy equality and range queries per ESR guidelines without redundant storage overhead.

2. **Punch-in race.**
Both concurrent requests validate input and execute `insert_one`. MongoDB's storage engine serializes the writes. The first request succeeds and returns 201 Created. The second encounters the unique index on `("emp_code", "date")` and raises `DuplicateKeyError`. Our application intercepts this exception and returns HTTP 409 Conflict, eliminating race conditions without application-level locking.

3. **Ties.**
The pipeline uses MongoDB `$setWindowFields` with `$rank` to compute standard competition ranking (1, 2, 2, 4). Per specification, pagination `limit` is applied after ranking via `$match: {"rank": {"$lte": limit}}`. If two employees tie at the cutoff rank (e.g., both rank 10), both are returned because both satisfy `<= 10`. Tied rows are deterministically sorted by `emp_code` ascending.

4. **Headcount.**
The aggregation pipeline starts from `employees` rather than `attendance_logs`, filtering by `joined_on <= month_end`. It performs a left-outer `$lookup` into `attendance_logs`. Employees with zero records produce an empty array. Grouping by department counts every employee via `$sum: 1`, preserving headcount integrity while correctly excluding them from record-level average work hours.

5. **One thing you would change for 100x data.**
At 100x scale (~10M attendance records), we would maintain pre-aggregated daily and monthly rollup collections updated asynchronously via MongoDB Change Streams, avoiding costly ad-hoc `$lookup` pipeline joins across collections, alongside range sharding on `(date, emp_code)`.
