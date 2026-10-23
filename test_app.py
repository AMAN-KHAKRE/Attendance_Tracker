import os
import tempfile
import unittest
from datetime import datetime, time, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import app as attendance_app


class AttendanceAppTests(unittest.TestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        directory = Path(self.temp_directory.name)
        roster_file = directory / "roster.txt"
        roster_file.write_text("Test Person\nOther Person\n", encoding="utf-8")
        attendance_app.app.config.update(
            TESTING=True,
            SECRET_KEY="test-secret-key",
            ROSTER_FILE=str(roster_file),
            DATABASE=str(directory / "attendance.sqlite3"),
        )
        attendance_app.initialize_database()
        os.environ["OFFICE_LATITUDE"] = "12.9716"
        os.environ["OFFICE_LONGITUDE"] = "77.5946"
        os.environ["GEOFENCE_RADIUS_METERS"] = "150"
        os.environ["ATTENDANCE_TIMEZONE"] = "Asia/Kolkata"
        os.environ["ATTENDANCE_CUTOFF"] = "09:00"
        os.environ["REPORT_ACCESS_KEY"] = "test-report-key"
        self.client = attendance_app.app.test_client()

    def tearDown(self):
        self.temp_directory.cleanup()

    def csrf_token(self):
        with self.client.session_transaction() as session:
            session["_csrf_token"] = "test-csrf-token"
        return "test-csrf-token"

    def post_checkin(self, name="Test Person", latitude="12.9716", longitude="77.5946"):
        return self.client.post(
            "/check-in",
            data={
                "_csrf_token": self.csrf_token(),
                "name": name,
                "latitude": latitude,
                "longitude": longitude,
                "details": "Engineering",
            },
        )

    def add_record(self, name, checked_in, status="In Office", distance=5):
        with attendance_app.app.app_context():
            attendance_app.get_db().execute(
                """
                INSERT INTO attendance
                    (roster_key, employee_name, work_date, checked_in_at_utc,
                     status, distance_meters, details)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name.casefold(),
                    name,
                    checked_in.date().isoformat(),
                    checked_in.astimezone(timezone.utc).isoformat(),
                    status,
                    distance,
                    "",
                ),
            )
            attendance_app.get_db().commit()

    def test_public_home_shows_checkin_without_login(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Select your name", response.data)
        self.assertIn(b"Test Person", response.data)
        self.assertNotIn(b"Sign in", response.data)

    def test_team_roster_has_24_names_and_excludes_requested_people(self):
        previous_roster_file = attendance_app.app.config["ROSTER_FILE"]
        attendance_app.app.config["ROSTER_FILE"] = str(
            Path(__file__).with_name("roster.txt")
        )
        try:
            roster = attendance_app.get_roster()
        finally:
            attendance_app.app.config["ROSTER_FILE"] = previous_roster_file
        self.assertEqual(len(roster), 24)
        self.assertFalse(any("Lavanya" in name for name in roster))
        self.assertFalse(any("Aishwarya" in name for name in roster))
        self.assertIn("Sireesha, C", roster)

    def test_office_coordinates_default_to_user_provided_location(self):
        previous_latitude = os.environ.pop("OFFICE_LATITUDE", None)
        previous_longitude = os.environ.pop("OFFICE_LONGITUDE", None)
        try:
            self.assertEqual(
                attendance_app.configured_office(),
                (12.966912759163794, 77.72264047583364),
            )
        finally:
            if previous_latitude is not None:
                os.environ["OFFICE_LATITUDE"] = previous_latitude
            if previous_longitude is not None:
                os.environ["OFFICE_LONGITUDE"] = previous_longitude

    def test_checkin_records_details_and_only_once_per_day(self):
        response = self.post_checkin()
        self.assertEqual(response.status_code, 302)
        with attendance_app.app.app_context():
            records = attendance_app.get_db().execute(
                "SELECT status, distance_meters, details FROM attendance"
            ).fetchall()
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["status"], "In Office")
            self.assertEqual(records[0]["distance_meters"], 0)
            self.assertEqual(records[0]["details"], "Engineering")

        self.assertEqual(self.post_checkin().status_code, 302)
        with attendance_app.app.app_context():
            count = attendance_app.get_db().execute(
                "SELECT COUNT(*) FROM attendance"
            ).fetchone()[0]
            self.assertEqual(count, 1)

    def test_checkin_is_stored_in_database_across_app_clients(self):
        self.assertEqual(self.post_checkin().status_code, 302)
        with attendance_app.app.app_context():
            count = attendance_app.get_db().execute(
                "SELECT COUNT(*) FROM attendance"
            ).fetchone()[0]
            self.assertEqual(count, 1)
        attendance_app.initialize_database()
        with attendance_app.app.app_context():
            count = attendance_app.get_db().execute(
                "SELECT COUNT(*) FROM attendance"
            ).fetchone()[0]
            self.assertEqual(count, 1)

    def test_outside_radius_checkin_is_recorded_as_outside(self):
        self.assertEqual(self.post_checkin(latitude="13.0000").status_code, 302)
        with attendance_app.app.app_context():
            record = attendance_app.get_db().execute(
                "SELECT status FROM attendance"
            ).fetchone()
            self.assertEqual(record["status"], "Outside Radius")

    def test_checkin_rejects_invalid_coordinates_and_unknown_names(self):
        self.assertEqual(self.post_checkin(latitude="91").status_code, 302)
        self.assertEqual(self.post_checkin(name="Not on roster").status_code, 302)
        with attendance_app.app.app_context():
            count = attendance_app.get_db().execute(
                "SELECT COUNT(*) FROM attendance"
            ).fetchone()[0]
            self.assertEqual(count, 0)

    def test_checkin_requires_csrf_token(self):
        response = self.client.post(
            "/check-in",
            data={"name": "Test Person", "latitude": "12.9716", "longitude": "77.5946"},
        )
        self.assertEqual(response.status_code, 400)

    def test_report_counts_checkin_at_cutoff_as_present(self):
        today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
        checked_in = datetime.combine(today, time(9, 0), ZoneInfo("Asia/Kolkata"))
        self.add_record("Test Person", checked_in)
        with attendance_app.app.app_context():
            report = attendance_app.build_report(today)
        self.assertEqual(report["present"], ["Test Person"])
        self.assertEqual(
            report["action_required"],
            [{"name": "Other Person", "reason": "No check-in"}],
        )

    def test_report_marks_late_and_outside_checkins_for_action(self):
        today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
        late = datetime.combine(today, time(9, 1), ZoneInfo("Asia/Kolkata"))
        self.add_record("Test Person", late)
        self.add_record("Other Person", late, "Outside Radius", 200)
        with attendance_app.app.app_context():
            report = attendance_app.build_report(today)
        self.assertEqual(report["present"], [])
        self.assertEqual(
            report["action_required"],
            [
                {"name": "Test Person", "reason": "Checked in after cutoff"},
                {"name": "Other Person", "reason": "Outside office radius"},
            ],
        )

    def test_internal_report_requires_key_and_returns_persistent_records(self):
        self.assertEqual(self.client.post("/internal/report").status_code, 403)
        with patch.object(attendance_app, "get_cutoff", return_value=time.min):
            response = self.client.post(
                "/internal/report",
                headers={"X-Report-Key": "test-report-key"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn("report", response.json)


if __name__ == "__main__":
    unittest.main()
