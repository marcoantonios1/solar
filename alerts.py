import smtplib
from email.mime.text import MIMEText
from datetime import datetime, timedelta
import os
from dotenv import load_dotenv

load_dotenv()

EMAIL_USERNAME = os.getenv("EMAIL_USERNAME")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
EMAIL_FROM = os.getenv("EMAIL_FROM")
EMAIL_TO = os.getenv("EMAIL_TO")
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587

# Per-alert-type cooldown - how long to wait before re-sending the same
# alert while the condition persists, so an ongoing issue doesn't spam
COOLDOWN_MINUTES = {
    "heartbeat_stale": 30,
    "critical_soc": 30,
    "underperformance": 360,  # 6 hours
    "crash": 30,
    "forecast_failures": 60,
    "actuator_write_failure": 30,
}


def _send_email(subject, body):
    if not all([EMAIL_USERNAME, EMAIL_PASSWORD, EMAIL_FROM, EMAIL_TO]):
        print("Missing email configuration in .env - cannot send alert.")
        return False
    try:
        msg = MIMEText(body)
        msg["From"] = EMAIL_FROM
        msg["To"] = EMAIL_TO
        msg["Subject"] = subject
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=30) as server:
            server.starttls()
            server.login(EMAIL_USERNAME, EMAIL_PASSWORD)
            server.send_message(msg)
        return True
    except Exception as e:
        print(f"Failed to send alert email: {e}")
        return False


def send_alert(conn, alert_key, subject, body):
    """
    Sends an alert email, respecting per-type cooldown so a persisting
    condition doesn't spam repeatedly. Tracks state in alert_log so a
    "condition cleared" message can be sent once it resolves.
    """
    now = datetime.now()
    cooldown = timedelta(minutes=COOLDOWN_MINUTES.get(alert_key, 30))

    row = conn.execute(
        "SELECT last_sent, active FROM alert_log WHERE alert_key = ?", (alert_key,)
    ).fetchone()

    if row is not None:
        last_sent = datetime.fromisoformat(row[0])
        if row[1] == 1 and (now - last_sent) < cooldown:
            return False  # still in cooldown, suppress

    sent = _send_email(subject, body)
    if sent:
        conn.execute(
            "INSERT OR REPLACE INTO alert_log (alert_key, last_sent, active) VALUES (?, ?, 1)",
            (alert_key, now.isoformat(timespec="seconds"))
        )
        conn.commit()
    return sent


def clear_alert(conn, alert_key, subject, body):
    """
    Call when a condition that previously alerted has resolved - sends a
    "condition cleared" message (once) and marks the alert inactive, so
    silence afterward stays meaningful.
    """
    row = conn.execute(
        "SELECT active FROM alert_log WHERE alert_key = ?", (alert_key,)
    ).fetchone()

    if row is not None and row[0] == 1:
        _send_email(subject, body)
        conn.execute(
            "UPDATE alert_log SET active = 0 WHERE alert_key = ?", (alert_key,)
        )
        conn.commit()