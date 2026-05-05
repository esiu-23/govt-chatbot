"""Email delivery via Resend. Set RESEND_API_KEY in environment."""

import logging
import os

logger = logging.getLogger(__name__)

FROM_ADDRESS = os.environ.get("EMAIL_FROM", "Chicago Civic Alerts <alerts@thegovernmentandme.tools>")


def send_email(to: str, subject: str, html: str) -> bool:
    """Send an HTML email via Resend. Returns True on success."""
    api_key = os.environ.get("RESEND_API_KEY")
    if not api_key:
        logger.warning("[email] RESEND_API_KEY not set — skipping send to %s", to)
        return False
    try:
        import resend
        resend.api_key = api_key
        resend.Emails.send({
            "from": FROM_ADDRESS,
            "to": [to],
            "subject": subject,
            "html": html,
        })
        logger.info("[email] sent '%s' to %s", subject, to)
        return True
    except Exception as e:
        logger.error("[email] failed to send to %s: %s", to, e)
        return False
