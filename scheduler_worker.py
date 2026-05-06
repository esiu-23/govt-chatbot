"""
scheduler_worker.py — run-once entry point for Render cron job.

Render invokes this daily. It runs all three scheduler jobs sequentially
then exits. No APScheduler, no persistent process needed.
"""
import logging

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

from app.resources import load_resources
load_resources()

from app.scheduler import (
    sync_meeting_schedule,
    check_and_send_meeting_emails,
    check_and_send_matter_updates,
)

sync_meeting_schedule()
check_and_send_meeting_emails()
check_and_send_matter_updates()
