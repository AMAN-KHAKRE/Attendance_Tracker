import json
import os
import smtplib
import ssl
import sys
import urllib.error
import urllib.request
from email.message import EmailMessage


def required_setting(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required GitHub Actions secret: {name}")
    return value


def format_report(report):
    present = "\n".join(f"- {name}" for name in report["present"]) or "- None"
    action_required = (
        "\n".join(
            f"- {item['name']} ({item['reason']})"
            for item in report["action_required"]
        )
        or "- None"
    )
    return (
        f"Attendance report for {report['date']} "
        f"(cutoff {report['cutoff']} {report['timezone']})\n\n"
        f"Present in office:\n{present}\n\n"
        f"Action required:\n{action_required}\n"
    )


def main():
    report_url = required_setting("ATTENDANCE_REPORT_URL")
    report_key = required_setting("ATTENDANCE_REPORT_KEY")
    request = urllib.request.Request(
        report_url,
        data=b"{}",
        headers={
            "Content-Type": "application/json",
            "X-Report-Key": report_key,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        details = error.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(
            f"Attendance report endpoint returned HTTP {error.code}: {details}"
        ) from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"Could not reach the attendance app: {error.reason}") from error

    report = payload["report"]
    message = EmailMessage()
    message["Subject"] = f"Attendance report - {report['date']}"
    message["From"] = required_setting("SMTP_FROM")
    message["To"] = required_setting("REPORT_TO")
    message.set_content(format_report(report))

    host = required_setting("SMTP_HOST")
    port = int(os.environ.get("SMTP_PORT", "587"))
    username = required_setting("SMTP_USERNAME")
    password = required_setting("SMTP_PASSWORD")
    context = ssl.create_default_context()
    with smtplib.SMTP(host, port, timeout=30) as smtp:
        smtp.starttls(context=context)
        smtp.login(username, password)
        smtp.send_message(message)
    print(f"Sent attendance report for {report['date']} to the configured recipient.")


if __name__ == "__main__":
    try:
        main()
    except (KeyError, RuntimeError, ValueError, smtplib.SMTPException) as error:
        print(f"Attendance report failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
