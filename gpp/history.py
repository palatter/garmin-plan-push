"""Training history in SQLite: activities, daily metrics, what was planned,
and the two things only the runner can log -- session RPE and pain.

stdlib sqlite3, one file under ~/.config/gpp/, no ORM. The analyses in
analysis.py take plain records, and this module hands them exactly that.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from . import analysis

STATUS_KINDS = ("illness", "injury", "cycle", "note")
DB_PATH = Path.home() / ".config" / "gpp" / "history.sqlite"

SCHEMA = """
CREATE TABLE IF NOT EXISTS activities (
    id TEXT PRIMARY KEY,
    date TEXT NOT NULL,
    name TEXT,
    sport TEXT DEFAULT 'running',
    distance_m REAL DEFAULT 0,
    seconds REAL DEFAULT 0,
    avg_hr INTEGER,
    source TEXT DEFAULT 'garmin'
);
CREATE INDEX IF NOT EXISTS activities_date ON activities(date);
CREATE TABLE IF NOT EXISTS metrics (
    date TEXT PRIMARY KEY,
    readiness INTEGER,
    hrv_status TEXT,
    hrv_weekly_avg INTEGER,
    sleep_score INTEGER,
    resting_hr INTEGER,
    vo2max REAL,
    training_status TEXT,
    running_tolerance INTEGER,
    body_battery INTEGER
);
CREATE TABLE IF NOT EXISTS planned (
    date TEXT NOT NULL,
    name TEXT NOT NULL,
    plan TEXT,
    tag TEXT,
    seconds REAL DEFAULT 0,
    metres REAL DEFAULT 0,
    role TEXT,
    PRIMARY KEY (date, name)
);
CREATE TABLE IF NOT EXISTS rpe (
    date TEXT NOT NULL,
    name TEXT NOT NULL,
    rpe INTEGER NOT NULL,
    note TEXT,
    PRIMARY KEY (date, name)
);
CREATE TABLE IF NOT EXISTS status (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    kind TEXT NOT NULL,
    note TEXT
);
CREATE TABLE IF NOT EXISTS pain (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    location TEXT NOT NULL,
    level INTEGER NOT NULL,
    pattern TEXT,
    note TEXT
);
"""


class History:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else DB_PATH
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self):
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # --- writes ---

    def upsert_activity(self, record: dict) -> None:
        with self.tx() as c:
            c.execute(
                """INSERT INTO activities (id, date, name, sport, distance_m, seconds, avg_hr, source)
                   VALUES (:id, :date, :name, :sport, :distance_m, :seconds, :avg_hr, :source)
                   ON CONFLICT(id) DO UPDATE SET date=excluded.date, name=excluded.name,
                   sport=excluded.sport, distance_m=excluded.distance_m, seconds=excluded.seconds,
                   avg_hr=excluded.avg_hr, source=excluded.source""",
                {
                    "id": str(record["id"]),
                    "date": record["date"],
                    "name": record.get("name"),
                    "sport": record.get("sport", "running"),
                    "distance_m": float(record.get("distance_m") or 0),
                    "seconds": float(record.get("seconds") or 0),
                    "avg_hr": record.get("avg_hr"),
                    "source": record.get("source", "garmin"),
                },
            )

    def upsert_metrics(self, date: str, **fields) -> None:
        cols = [
            k
            for k in fields
            if k
            in (
                "readiness",
                "hrv_status",
                "hrv_weekly_avg",
                "sleep_score",
                "resting_hr",
                "vo2max",
                "training_status",
                "running_tolerance",
                "body_battery",
            )
            and fields[k] is not None
        ]
        with self.tx() as c:
            c.execute("INSERT OR IGNORE INTO metrics (date) VALUES (?)", (date,))
            if cols:
                assignments = ", ".join(f"{k} = ?" for k in cols)
                c.execute(
                    f"UPDATE metrics SET {assignments} WHERE date = ?",  # noqa: S608 - columns whitelisted above
                    [fields[k] for k in cols] + [date],
                )

    def record_planned(self, plan_name: str, rows: list[dict]) -> None:
        with self.tx() as c:
            for r in rows:
                c.execute(
                    """INSERT INTO planned (date, name, plan, tag, seconds, metres, role)
                       VALUES (?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(date, name) DO UPDATE SET plan=excluded.plan, tag=excluded.tag,
                       seconds=excluded.seconds, metres=excluded.metres, role=excluded.role""",
                    (
                        r["date"],
                        r["name"],
                        plan_name,
                        r.get("tag"),
                        r.get("seconds", 0),
                        r.get("metres", 0),
                        r.get("role"),
                    ),
                )

    def log_rpe(self, date: str, name: str, rpe: int, note: str | None = None) -> None:
        if not 1 <= rpe <= 10:
            raise ValueError("RPE is 1-10")
        with self.tx() as c:
            c.execute(
                "INSERT INTO rpe (date, name, rpe, note) VALUES (?, ?, ?, ?) ON CONFLICT(date, name) DO UPDATE SET rpe=excluded.rpe, note=excluded.note",
                (date, name, rpe, note),
            )

    def log_pain(
        self,
        date: str,
        location: str,
        level: int,
        pattern: str | None = None,
        note: str | None = None,
    ) -> None:
        if not 0 <= level <= 10:
            raise ValueError("pain level is 0-10")
        with self.tx() as c:
            c.execute(
                "INSERT INTO pain (date, location, level, pattern, note) VALUES (?, ?, ?, ?, ?)",
                (date, location, level, pattern, note),
            )

    def log_status(self, date: str, kind: str, note: str | None = None) -> None:
        """Illness, injury, or a cycle note (#152): logged, never used to periodize."""
        if kind not in STATUS_KINDS:
            raise ValueError(f"status kind must be one of {STATUS_KINDS}")
        with self.tx() as c:
            c.execute("INSERT INTO status (date, kind, note) VALUES (?, ?, ?)", (date, kind, note))

    def status_log(self, since: dt.date | None = None) -> list[dict]:
        sql, args = "SELECT date, kind, note FROM status", ()
        if since:
            sql, args = sql + " WHERE date >= ?", (since.isoformat(),)
        return [dict(r) for r in self.conn.execute(sql + " ORDER BY date", args)]

    def recent_status(self, day: dt.date | None = None, days: int = 3) -> list[dict]:
        """Illness or injury logged within the last `days`, for the daily advice."""
        day = day or dt.date.today()
        since = (day - dt.timedelta(days=days)).isoformat()
        rows = self.conn.execute(
            "SELECT date, kind, note FROM status WHERE date >= ? AND date <= ? AND kind IN ('illness', 'injury') ORDER BY date",
            (since, day.isoformat()),
        )
        return [dict(r) for r in rows]

    # --- reads ---

    def activities(self, since: dt.date | None = None, sport: str | None = "running") -> list[dict]:
        sql = "SELECT * FROM activities WHERE 1=1"
        args: list = []
        if since:
            sql += " AND date >= ?"
            args.append(since.isoformat())
        if sport:
            sql += " AND sport = ?"
            args.append(sport)
        sql += " ORDER BY date"
        return [dict(r) for r in self.conn.execute(sql, args)]

    def metrics(self, date: dt.date) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM metrics WHERE date = ?", (date.isoformat(),)
        ).fetchone()
        return dict(row) if row else None

    def resting_hr_baseline(self, as_of: dt.date, days: int = 28) -> int | None:
        since = (as_of - dt.timedelta(days=days)).isoformat()
        row = self.conn.execute(
            "SELECT AVG(resting_hr) AS avg FROM metrics WHERE date >= ? AND date < ? AND resting_hr IS NOT NULL",
            (since, as_of.isoformat()),
        ).fetchone()
        return round(row["avg"]) if row and row["avg"] else None

    def planned(self, start: dt.date | None = None, end: dt.date | None = None) -> list[dict]:
        sql = "SELECT * FROM planned WHERE 1=1"
        args: list = []
        if start:
            sql += " AND date >= ?"
            args.append(start.isoformat())
        if end:
            sql += " AND date <= ?"
            args.append(end.isoformat())
        sql += " ORDER BY date"
        return [dict(r) for r in self.conn.execute(sql, args)]

    def rpe_log(self, since: dt.date | None = None) -> list[dict]:
        sql, args = "SELECT * FROM rpe", []
        if since:
            sql += " WHERE date >= ?"
            args.append(since.isoformat())
        return [dict(r) for r in self.conn.execute(sql + " ORDER BY date", args)]

    def pain_log(self, since: dt.date | None = None) -> list[dict]:
        sql, args = "SELECT * FROM pain", []
        if since:
            sql += " WHERE date >= ?"
            args.append(since.isoformat())
        return [dict(r) for r in self.conn.execute(sql + " ORDER BY date", args)]

    # --- analyses over the store ---

    def compliance(self, start: dt.date, end: dt.date) -> list[dict]:
        return analysis.match_planned_to_actual(self.planned(start, end), self.activities(start))

    def weekly(self, since: dt.date) -> list[dict]:
        return analysis.weekly_from_activities(self.activities(since))

    def reestimate_threshold(self, as_of: dt.date | None = None):
        as_of = as_of or dt.date.today()
        return analysis.reestimate_threshold(self.activities(as_of - dt.timedelta(days=60)), as_of)

    def marathon_shape(self, goal_km: float, as_of: dt.date | None = None) -> analysis.Shape:
        as_of = as_of or dt.date.today()
        weeks = self.weekly(as_of - dt.timedelta(weeks=26))
        recent = [w["longest_km"] for w in weeks[-10:]]
        return analysis.marathon_shape([w["km"] for w in weeks], recent, goal_km)

    def today_advice(self, planned_role: str | None, day: dt.date | None = None) -> analysis.Advice:
        day = day or dt.date.today()
        m = self.metrics(day) or {}
        return analysis.today_advice(
            planned_role,
            readiness=m.get("readiness"),
            hrv_status=m.get("hrv_status"),
            sleep_score=m.get("sleep_score"),
            resting_hr=m.get("resting_hr"),
            resting_hr_baseline=self.resting_hr_baseline(day),
        )

    def pain_trend(self, since: dt.date) -> dict[str, list[tuple[str, int]]]:
        """Pain level by location over time -- the soft injury signal."""
        out: dict[str, list[tuple[str, int]]] = {}
        for row in self.pain_log(since):
            out.setdefault(row["location"].lower(), []).append((row["date"], row["level"]))
        return out
