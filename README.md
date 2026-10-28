# Team Attendance Check-in

A Python Flask app with a public check-in page. Team members choose their name,
optionally add a note, and tap **Check in**. The browser requests location only
after the button is tapped. The app checks the location against the configured
office radius and stores the result in a persistent SQLite database.

The report separates people into:

- **Present in office:** within the radius and checked in by 9:00 AM India
  time.
- **Action required:** no check-in, outside the radius, or checked in after
  9:00 AM.

The public form does not verify identity. Someone can choose another person's
name, and GPS can be inaccurate or spoofed. Tell the team how this works and
do not treat it as proof of identity or physical presence.

## Current app configuration

- Office latitude: `12.966912759163794`
- Office longitude: `77.72264047583364`
- Radius: 150 meters
- Time zone: `Asia/Kolkata`
- Cutoff: 9:00 AM
- Roster: [roster.txt](./roster.txt), one name per line

Lavanya and Aishwarya are excluded as requested. Four names are abbreviated
with `...` because the screenshots cut them off; edit `roster.txt` to correct
them. Keep this repository private because it contains employee names and
office coordinates.

## Local run and tests (Windows)

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
$env:ATTENDANCE_SECRET_KEY = (python -c "import secrets; print(secrets.token_hex(32))")
python app.py
```

Open `http://127.0.0.1:5000`. For location access from phones, the app must be
served over HTTPS.

Run tests:

```powershell
python -m unittest -v
```

## Free trial deployment: PythonAnywhere

This keeps the Python app and SQLite database on PythonAnywhere, which supplies
an HTTPS link. An administrator can email the report for the selected date
from the protected admin dashboard.

**Free-plan caveat:** PythonAnywhere currently says new free web apps expire
after one month. This is a free trial arrangement, not dependable long-term
attendance infrastructure.

### 1. Put the code in a private GitHub repository

Create a **private** repository and upload this project, including:

- `app.py`
- `requirements.txt`
- `roster.txt`
- the `templates` folder

Do not upload `.venv`, `attendance.sqlite3`, `attendance.db`, or secrets.

### 2. Create the PythonAnywhere web app

1. Create a PythonAnywhere account. In the **Web** tab choose **Add a new web
   app**, then **Manual configuration** and the Python version offered by your
   account. The account does not need a domain; it provides a
   `YOUR_USERNAME.pythonanywhere.com` HTTPS address.
2. Upload the project files to a folder such as
   `/home/YOUR_USERNAME/attendance-tracker`.
3. In a PythonAnywhere Bash console, install the requirements in a virtual
   environment:

   ```bash
   cd /home/YOUR_USERNAME/attendance-tracker
   python3 -m venv /home/YOUR_USERNAME/.virtualenvs/attendance
   /home/YOUR_USERNAME/.virtualenvs/attendance/bin/pip install -r requirements.txt
   ```

4. In the PythonAnywhere **Web** tab, set the virtualenv to
   `/home/YOUR_USERNAME/.virtualenvs/attendance`.
5. Edit the WSGI configuration file linked from the Web tab. Replace
   `YOUR_USERNAME` and the example values, then use:

   ```python
   import os
   import sys

   project_home = "/home/YOUR_USERNAME/attendance-tracker"
   if project_home not in sys.path:
       sys.path.insert(0, project_home)

   os.environ["ATTENDANCE_DATABASE"] = "/home/YOUR_USERNAME/attendance.sqlite3"
   os.environ["ATTENDANCE_SECRET_KEY"] = "GENERATE_A_DIFFERENT_LONG_RANDOM_SECRET"
   os.environ["ADMIN_PASSWORD"] = "GENERATE_ANOTHER_LONG_RANDOM_ADMIN_PASSWORD"
   os.environ["SESSION_COOKIE_SECURE"] = "true"
   os.environ["REPORT_TO"] = "recipient@example.com"
   os.environ["SMTP_HOST"] = "smtp.gmail.com"
   os.environ["SMTP_PORT"] = "587"
   os.environ["SMTP_FROM"] = "sender@gmail.com"
   os.environ["SMTP_USERNAME"] = "sender@gmail.com"
   os.environ["SMTP_PASSWORD"] = "YOUR_EMAIL_PROVIDER_APP_PASSWORD"

   from app import app as application
   ```

   Generate random secrets in a local Python shell using
   `python -c "import secrets; print(secrets.token_hex(32))"`. Keep these
   secrets private. `ADMIN_PASSWORD` protects the attendance dashboard at
   `https://YOUR_USERNAME.pythonanywhere.com/admin`; share it only with the
   administrator. The public check-in page remains available without login.
   The database is placed in your home folder, outside the uploaded code, so
   code updates will not overwrite it.
6. Save the WSGI file and press **Reload** on the Web tab. Open the HTTPS site
   shown on the Web tab and submit a test check-in. The SQLite database is
   created automatically.

For Gmail, use `smtp.gmail.com`, port `587`, your full Gmail address as
username, and a Google **App Password** (not your normal Google password).
Follow your email provider's security policy and keep the app password private.
After changing the WSGI settings, save the file and press **Reload** on the
PythonAnywhere Web tab. Confirm your hosting plan allows outbound SMTP
connections to your email provider.

### 3. Email a report from the admin dashboard

Open `https://YOUR_USERNAME.pythonanywhere.com/admin`, sign in, choose the
report date, and select **Email this report**. The message goes to `REPORT_TO`
and includes the in-office and action-required lists for that date. The
application sends no scheduled or automatic emails.

The report reads check-ins from SQLite, so an app reload or process restart
does not erase them. Back up `attendance.sqlite3` from your PythonAnywhere home
folder periodically; do not add it to GitHub.

## Limitations

- PythonAnywhere free accounts have restrictions and a one-month web-app
  expiry according to its current documentation. Recheck the plan before
  relying on it.
- Without login, a user can check in under a teammate's name.
- GPS may be inaccurate or spoofed.
- SQLite is appropriate for a small team on one app instance. Do not run
  multiple app workers against this SQLite file.
