import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from datetime import datetime, timedelta
from dotenv import load_dotenv

from reports.monthly_pdf import build_monthly_pdf

load_dotenv()

EMAIL_USERNAME = os.getenv("EMAIL_USERNAME")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
EMAIL_FROM = os.getenv("EMAIL_FROM")
EMAIL_TO = os.getenv("EMAIL_TO")

SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587


def send_report_email(pdf_path, period_label):
    msg = MIMEMultipart()
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    msg["Subject"] = f"EDL Solar Automation — Monthly Report ({period_label})"

    body = f"Attached: your EDL Solar Automation report for {period_label}.\nGenerated automatically."
    msg.attach(MIMEText(body, "plain"))

    with open(pdf_path, "rb") as f:
        attachment = MIMEApplication(f.read(), _subtype="pdf")
        attachment.add_header("Content-Disposition", "attachment", filename=os.path.basename(pdf_path))
        msg.attach(attachment)

    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
        server.starttls()
        server.login(EMAIL_USERNAME, EMAIL_PASSWORD)
        server.send_message(msg)


def get_previous_month_range():
    today = datetime.now()
    first_of_this_month = today.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last_of_previous_month = first_of_this_month - timedelta(seconds=1)
    first_of_previous_month = last_of_previous_month.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return first_of_previous_month, last_of_previous_month


def main():
    if not all([EMAIL_USERNAME, EMAIL_PASSWORD, EMAIL_FROM, EMAIL_TO]):
        print("Missing email configuration in .env - aborting.")
        return

    start_dt, end_dt = get_previous_month_range()
    period_label = start_dt.strftime("%B %Y")
    start_str = start_dt.isoformat(timespec="seconds")
    end_str = end_dt.isoformat(timespec="seconds")

    output_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "reports_archive")
    output_path = os.path.join(output_dir, f"report_{start_dt.strftime('%Y-%m')}.pdf")

    os.makedirs(output_dir, exist_ok=True)

    print(f"Generating monthly report for {period_label} ({start_str} to {end_str})...")
    build_monthly_pdf(start_str=start_str, end_str=end_str, output_path=output_path)

    print("Sending email...")
    send_report_email(output_path, period_label)

    print(f"Report sent successfully to {EMAIL_TO}.")


if __name__ == "__main__":
    main()