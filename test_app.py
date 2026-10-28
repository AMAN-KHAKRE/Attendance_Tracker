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
        os.environ["ADMIN_PASSWORD"] = "test-admin-password"
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

    def test_admin_dashboard_requires_password_and_shows_day_summary(self):
        today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
        on_time = datetime.combine(today, time(8, 55), ZoneInfo("Asia/Kolkata"))
        self.add_record("Test Person", on_time, distance=12)
        login_page = self.client.get("/admin")
        self.assertEqual(login_page.status_code, 200)
        self.assertIn(b"Admin password", login_page.data)
        self.assertNotIn(b"12 m", login_page.data)

        failed_login = self.client.post(
            "/admin",
            data={
                "_csrf_token": self.csrf_token(),
                "password": "incorrect",
            },
        )
        self.assertEqual(failed_login.status_code, 200)
        self.assertIn(b"was not correct", failed_login.data)

        successful_login = self.client.post(
            "/admin",
            data={
                "_csrf_token": self.csrf_token(),
                "password": "test-admin-password",
            },
        )
        self.assertEqual(successful_login.status_code, 302)
        dashboard = self.client.get(f"/admin?date={today.isoformat()}")
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn(b"Attendance overview", dashboard.data)
        self.assertIn(b"Email this report", dashboard.data)
        self.assertIn(b"12 m", dashboard.data)
        self.assertIn(b"In office", dashboard.data)
        self.assertIn(b"No check-in", dashboard.data)

    def test_admin_can_email_report_for_selected_date(self):
        selected_date = datetime.now(ZoneInfo("Asia/Kolkata")).date().isoformat()
        with self.client.session_transaction() as session:
            session["admin_authenticated"] = True

        with patch.object(attendance_app, "send_attendance_email") as send_email:
            response = self.client.post(
                "/admin/send-report",
                data={
                    "_csrf_token": self.csrf_token(),
                    "date": selected_date,
                },
            )

        self.assertEqual(response.status_code, 302)
        self.assertIn(f"date={selected_date}", response.location)
        report = send_email.call_args.args[0]
        self.assertEqual(report["date"], selected_date)
        dashboard = self.client.get(response.location)
        self.assertIn(b"was emailed", dashboard.data)

    def test_admin_send_report_requires_login(self):
        with patch.object(attendance_app, "send_attendance_email") as send_email:
            response = self.client.post(
                "/admin/send-report",
                data={
                    "_csrf_token": self.csrf_token(),
                    "date": "2026-10-26",
                },
            )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith("/admin"))
        send_email.assert_not_called()

    def test_admin_send_report_shows_email_failure(self):
        with self.client.session_transaction() as session:
            session["admin_authenticated"] = True
        with patch.object(
            attendance_app,
            "send_attendance_email",
            side_effect=RuntimeError("SMTP is not configured"),
        ):
            response = self.client.post(
                "/admin/send-report",
                data={
                    "_csrf_token": self.csrf_token(),
                    "date": "2026-10-26",
                },
            )
        dashboard = self.client.get(response.location)
        self.assertIn(b"report could not be emailed", dashboard.data)

    def test_send_attendance_email_uses_configured_smtp_settings(self):
        settings = {
            "REPORT_TO": "recipient@example.com",
            "SMTP_FROM": "sender@example.com",
            "SMTP_HOST": "smtp.example.com",
            "SMTP_PORT": "587",
            "SMTP_USERNAME": "sender@example.com",
            "SMTP_PASSWORD": "test-password",
        }
        report = {
            "date": "2026-10-26",
            "cutoff": "09:00",
            "timezone": "Asia/Kolkata",
            "present": ["Test Person"],
            "action_required": [{"name": "Other Person", "reason": "No check-in"}],
        }
        with patch.dict(os.environ, settings), patch.object(
            attendance_app.smtplib, "SMTP"
        ) as smtp_class:
            attendance_app.send_attendance_email(report)

        smtp_class.assert_called_once_with("smtp.example.com", 587, timeout=30)
        smtp = smtp_class.return_value.__enter__.return_value
        smtp.starttls.assert_called_once()
        smtp.login.assert_called_once_with("sender@example.com", "test-password")
        message = smtp.send_message.call_args.args[0]
        self.assertEqual(message["To"], "recipient@example.com")
        self.assertEqual(message["Subject"], "Attendance report - 2026-10-26")
        self.assertIn("Test Person", message.get_content())
        self.assertIn("Other Person (No check-in)", message.get_content())

    def test_admin_logout_relocks_dashboard(self):
        with self.client.session_transaction() as session:
            session["admin_authenticated"] = True
        response = self.client.post(
            "/admin/logout",
            data={"_csrf_token": self.csrf_token()},
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn(b"Admin password", self.client.get("/admin").data)

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

    def test_same_browser_cannot_check_in_as_a_different_name_same_day(self):
        self.assertEqual(self.post_checkin(name="Test Person").status_code, 302)
        response = self.post_checkin(name="Other Person")
        self.assertEqual(response.status_code, 302)
        with self.client:
            page = self.client.get("/")
            self.assertIn(
                b"This browser already checked in today as Test Person.",
                page.data,
            )
        with attendance_app.app.app_context():
            records = attendance_app.get_db().execute(
                "SELECT employee_name, device_key FROM attendance"
            ).fetchall()
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["employee_name"], "Test Person")
            self.assertTrue(records[0]["device_key"])

    def test_different_browser_can_check_in_as_another_name(self):
        self.assertEqual(self.post_checkin(name="Test Person").status_code, 302)
        other_client = attendance_app.app.test_client()
        other_client.get("/")
        with other_client.session_transaction() as session:
            csrf_token = session["_csrf_token"]
        response = other_client.post(
            "/check-in",
            data={
                "_csrf_token": csrf_token,
                "name": "Other Person",
                "latitude": "12.9716",
                "longitude": "77.5946",
                "details": "",
            },
        )
        self.assertEqual(response.status_code, 302)
        with attendance_app.app.app_context():
            count = attendance_app.get_db().execute(
                "SELECT COUNT(*) FROM attendance"
            ).fetchone()[0]
            self.assertEqual(count, 2)

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
