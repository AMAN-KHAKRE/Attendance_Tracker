import argparse
import hmac
import os
import secrets
import sqlite3
from datetime import datetime, time, timezone
from math import asin, cos, isfinite, radians, sin, sqrt
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import (
    Flask,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)


app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("ATTENDANCE_SECRET_KEY") or secrets.token_hex(32),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "false").lower()
    == "true",
    ROSTER_FILE=os.environ.get(
        "ATTENDANCE_ROSTER_FILE",
        str(Path(__file__).with_name("roster.txt")),
    ),
    DATABASE=os.environ.get(
        "ATTENDANCE_DATABASE",
        str(Path(__file__).with_name("attendance.sqlite3")),
    ),
)


def get_roster():
    try:
        names = [
            name.strip()
            for name in Path(app.config["ROSTER_FILE"])
            .read_text(encoding="utf-8")
            .splitlines()
            if name.strip()
        ]
    except OSError as error:
        raise RuntimeError(
            f"Could not read the team roster file: {app.config['ROSTER_FILE']}"
        ) from error
    if any(len(name) > 100 for name in names):
        raise RuntimeError("Roster names must be 100 characters or fewer.")
    keys = [name.casefold() for name in names]
    if len(keys) != len(set(keys)):
        raise RuntimeError("The roster file contains duplicate names.")
    return names


def configured_office():
    latitude = os.environ.get("OFFICE_LATITUDE", "12.966912759163794")
    longitude = os.environ.get("OFFICE_LONGITUDE", "77.72264047583364")
    try:
        latitude = float(latitude)
        longitude = float(longitude)
    except ValueError:
        raise RuntimeError("OFFICE_LATITUDE and OFFICE_LONGITUDE must be numbers.")
    if not (
        isfinite(latitude)
        and isfinite(longitude)
        and -90 <= latitude <= 90
        and -180 <= longitude <= 180
    ):
        raise RuntimeError("Office coordinates are outside valid latitude/longitude ranges.")
    return latitude, longitude


def get_timezone():
    try:
        return ZoneInfo(os.environ.get("ATTENDANCE_TIMEZONE", "Asia/Kolkata"))
    except ZoneInfoNotFoundError as error:
        raise RuntimeError("ATTENDANCE_TIMEZONE must be a valid IANA time zone.") from error


def get_radius():
    try:
        radius = float(os.environ.get("GEOFENCE_RADIUS_METERS", "150"))
    except ValueError as error:
        raise RuntimeError("GEOFENCE_RADIUS_METERS must be a positive number.") from error
    if not isfinite(radius) or radius <= 0:
        raise RuntimeError("GEOFENCE_RADIUS_METERS must be a positive number.")
    return radius


def get_cutoff():
    try:
        return time.fromisoformat(os.environ.get("ATTENDANCE_CUTOFF", "09:00"))
    except ValueError as error:
        raise RuntimeError("ATTENDANCE_CUTOFF must be in HH:MM format.") from error


def get_db():
    if "db" not in g:
        database_path = Path(app.config["DATABASE"])
        database_path.parent.mkdir(parents=True, exist_ok=True)
        g.db = sqlite3.connect(database_path, timeout=15)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def initialize_database():
    database_path = Path(app.config["DATABASE"])
    database_path.parent.mkdir(parents=True, exist_ok=True)
    database = sqlite3.connect(database_path, timeout=15)
    try:
        database.execute(
            """
            CREATE TABLE IF NOT EXISTS attendance (
                id INTEGER PRIMARY KEY,
                roster_key TEXT NOT NULL,
                employee_name TEXT NOT NULL,
                work_date TEXT NOT NULL,
                checked_in_at_utc TEXT NOT NULL,
                status TEXT NOT NULL
                    CHECK(status IN ('In Office', 'Outside Radius')),
                distance_meters INTEGER NOT NULL,
                details TEXT NOT NULL DEFAULT '',
                UNIQUE(roster_key, work_date)
            )
            """
        )
        database.commit()
    finally:
        database.close()


@app.teardown_appcontext
def close_db(_error=None):
    database = g.pop("db", None)
    if database is not None:
        database.close()


def csrf_token():
    token = session.get("_csrf_token")
    if token is None:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


app.jinja_env.globals["csrf_token"] = csrf_token


@app.before_request
def protect_post_requests():
    if request.method == "POST" and request.endpoint != "internal_report":
        expected = session.get("_csrf_token", "")
        supplied = request.form.get("_csrf_token", "")
        if not expected or not hmac.compare_digest(expected, supplied):
            abort(400, "Invalid form token. Refresh the page and try again.")


def distance_in_meters(latitude, longitude, office_latitude, office_longitude):
    lat1, lat2 = radians(latitude), radians(office_latitude)
    delta_lat = radians(office_latitude - latitude)
    delta_lon = radians(office_longitude - longitude)
    haversine = (
        sin(delta_lat / 2) ** 2
        + cos(lat1) * cos(lat2) * sin(delta_lon / 2) ** 2
    )
    return 2 * 6_371_000 * asin(sqrt(min(1.0, haversine)))


def build_report(work_date):
    timezone_info = get_timezone()
    cutoff = get_cutoff()
    records = get_db().execute(
        """
        SELECT roster_key, checked_in_at_utc, status
        FROM attendance
        WHERE work_date = ?
        """,
        (work_date.isoformat(),),
    ).fetchall()
    checkins = {row["roster_key"]: row for row in records}
    present = []
    action_required = []
    for name in get_roster():
        entry = checkins.get(name.casefold())
        if entry is None:
            action_required.append({"name": name, "reason": "No check-in"})
            continue
        checked_in = datetime.fromisoformat(entry["checked_in_at_utc"]).astimezone(timezone_info)
        if entry["status"] != "In Office":
            reason = "Outside office radius"
        elif checked_in.date() != work_date or checked_in.time() > cutoff:
            reason = "Checked in after cutoff"
        else:
            present.append(name)
            continue
        action_required.append({"name": name, "reason": reason})
    return {
        "date": work_date.isoformat(),
        "cutoff": cutoff.strftime("%H:%M"),
        "timezone": str(timezone_info),
        "present": present,
        "action_required": action_required,
    }


@app.route("/", methods=["GET"])
def home():
    roster = get_roster()
    return render_template(
        "home.html",
        roster=roster,
        office_configured=configured_office() is not None,
    )


@app.route("/check-in", methods=["POST"])
def check_in():
    office = configured_office()
    if office is None:
        flash("Office coordinates are not configured yet. Check-in is unavailable.", "error")
        return redirect(url_for("home"))
    try:
        latitude = float(request.form.get("latitude", ""))
        longitude = float(request.form.get("longitude", ""))
    except ValueError:
        flash("Your browser did not provide a valid location. Allow location access and retry.", "error")
        return redirect(url_for("home"))
    if not (
        isfinite(latitude)
        and isfinite(longitude)
        and -90 <= latitude <= 90
        and -180 <= longitude <= 180
    ):
        flash("The location values are outside valid coordinate ranges.", "error")
        return redirect(url_for("home"))

    roster = get_roster()
    name_key = request.form.get("name", "").strip().casefold()
    canonical_name = next(
        (name for name in roster if name.casefold() == name_key),
        None,
    )
    if canonical_name is None:
        flash("Choose your name from the team roster.", "error")
        return redirect(url_for("home"))
    details = request.form.get("details", "").strip()
    if len(details) > 300:
        flash("Additional details must be 300 characters or fewer.", "error")
        return redirect(url_for("home"))

    timezone_info = get_timezone()
    now_utc = datetime.now(timezone.utc)
    now = now_utc.astimezone(timezone_info)
    distance = distance_in_meters(latitude, longitude, *office)
    status = "In Office" if distance <= get_radius() else "Outside Radius"
    checkin = {
        "roster_key": canonical_name.casefold(),
        "employee_name": canonical_name,
        "work_date": now.date().isoformat(),
        "checked_in_at_utc": now_utc.isoformat(),
        "status": status,
        "distance_meters": round(distance),
        "details": details,
    }
    try:
        get_db().execute(
            """
            INSERT INTO attendance
                (roster_key, employee_name, work_date, checked_in_at_utc,
                 status, distance_meters, details)
            VALUES
                (:roster_key, :employee_name, :work_date, :checked_in_at_utc,
                 :status, :distance_meters, :details)
            """,
            checkin,
        )
        get_db().commit()
    except sqlite3.IntegrityError:
        get_db().rollback()
        flash("You already checked in today. A second check-in was not recorded.", "error")
        return redirect(url_for("home"))

    if status == "In Office":
        flash(f"Check-in recorded: within the office radius ({round(distance)} m).", "success")
    else:
        flash(f"Check-in recorded: outside the office radius ({round(distance)} m).", "error")
    return redirect(url_for("home"))


@app.route("/internal/report", methods=["POST"])
def internal_report():
    report_key = os.environ.get("REPORT_ACCESS_KEY")
    provided_key = request.headers.get("X-Report-Key", "")
    if not report_key or not hmac.compare_digest(report_key, provided_key):
        abort(403)
    try:
        now = datetime.now(get_timezone())
        if now.time().replace(tzinfo=None) < get_cutoff():
            return jsonify({"error": "The attendance cutoff has not passed yet."}), 409
        report = build_report(now.date())
    except (ValueError, RuntimeError, OSError, sqlite3.Error):
        app.logger.exception("Could not generate the attendance report.")
        abort(500, "The attendance report could not be generated.")
    return jsonify({"report": report})


initialize_database()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the attendance check-in web app.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5000)
    args = parser.parse_args()
    app.run(host=args.host, port=args.port, debug=False)
