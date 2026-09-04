"""
notify.py
---------
Render-complete email notification via SES.

Called from aws_handler after a job reaches COMPLETE or FAILED.
Never raises — a notification failure must not affect job status.

Environment variables:
    SES_FROM_EMAIL         — verified SES sender address (required)
    COGNITO_USER_POOL_ID   — Cognito user pool ID for email lookup (required)
    AWS_REGION             — AWS region (auto-set by Lambda runtime)
    NEXT_PUBLIC_APP_URL    — app base URL for the download link
"""

import os
import logging

import boto3
from botocore.exceptions import ClientError

log = logging.getLogger(__name__)

_region        = os.environ.get("AWS_REGION", "us-east-1")
_from_email    = os.environ.get("SES_FROM_EMAIL", "")
_user_pool_id  = os.environ.get("COGNITO_USER_POOL_ID", "")
_app_url       = os.environ.get("NEXT_PUBLIC_APP_URL", "https://razorvid.com").rstrip("/")

_ses      = boto3.client("ses",           region_name=_region)
_cognito  = boto3.client("cognito-idp",   region_name=_region)


def _get_owner_email(owner_id: str) -> str | None:
    """Look up the owner's email from Cognito by their sub (user_id)."""
    if not _user_pool_id or not owner_id:
        return None
    try:
        resp = _cognito.admin_get_user(UserPoolId=_user_pool_id, Username=owner_id)
        for attr in resp.get("UserAttributes", []):
            if attr["Name"] == "email":
                return attr["Value"]
    except ClientError as exc:
        # User not found or pool misconfigured — not fatal
        log.warning("[notify] Could not look up owner email for %s: %s", owner_id, exc)
    return None


def send_project_notification(owner_id: str, subject: str, text: str, html: str) -> None:
    """Send a best-effort project notification to its owner."""
    if not _from_email:
        log.info("[notify] SES_FROM_EMAIL not set — skipping notification")
        return
    try:
        email = _get_owner_email(owner_id)
        if not email:
            return
        _ses.send_email(
            Source=_from_email,
            Destination={"ToAddresses": [email]},
            Message={
                "Subject": {"Data": subject},
                "Body": {"Text": {"Data": text}, "Html": {"Data": html}},
            },
        )
        log.info("[notify] Project notification sent to %s", email)
    except Exception as exc:
        log.warning("[notify] Failed to send project notification: %s", exc)


def send_member_joined(project_name: str, owner_id: str, member_name: str) -> None:
    send_project_notification(
        owner_id,
        f"New member joined — {project_name}",
        f"{member_name} joined your RazorVid project \"{project_name}\".",
        f"<p><strong>{member_name}</strong> joined your RazorVid project <strong>{project_name}</strong>.</p>",
    )


def send_angle_uploaded(project_name: str, owner_id: str, contributor_name: str, file_count: int) -> None:
    noun = "angle" if file_count == 1 else "angles"
    send_project_notification(
        owner_id,
        f"New video angle added — {project_name}",
        f"{contributor_name} added {file_count} video {noun} to your RazorVid project \"{project_name}\".",
        f"<p><strong>{contributor_name}</strong> added {file_count} video {noun} to your RazorVid project <strong>{project_name}</strong>.</p>",
    )


def send_join_request_received(project_name: str, owner_id: str, requester_name: str) -> None:
    send_project_notification(
        owner_id,
        f"New join request — {project_name}",
        f"{requester_name} requested to join your RazorVid project \"{project_name}\".",
        f"<p><strong>{requester_name}</strong> requested to join your RazorVid project <strong>{project_name}</strong>.</p>",
    )


def send_render_complete(job_id: str, project_name: str, owner_id: str) -> None:
    """Send a render-complete email to the project owner. Never raises."""
    if not _from_email:
        log.info("[notify] SES_FROM_EMAIL not set — skipping notification for job %s", job_id)
        return

    try:
        email = _get_owner_email(owner_id)
        if not email:
            log.info("[notify] No email found for owner %s — skipping notification", owner_id)
            return

        render_url = f"{_app_url}/render/{job_id}"

        _ses.send_email(
            Source=_from_email,
            Destination={"ToAddresses": [email]},
            Message={
                "Subject": {"Data": f"Your RazorVid render is ready — {project_name}"},
                "Body": {
                    "Text": {
                        "Data": (
                            f"Your multicam render for \"{project_name}\" is complete.\n\n"
                            f"Download your video here:\n{render_url}\n\n"
                            "— The RazorVid Team"
                        )
                    },
                    "Html": {
                        "Data": (
                            f"<p>Your multicam render for <strong>{project_name}</strong> is complete.</p>"
                            f"<p><a href=\"{render_url}\">Download your video</a></p>"
                            "<p>— The RazorVid Team</p>"
                        )
                    },
                },
            },
        )
        log.info("[notify] Render-complete email sent to %s for job %s", email, job_id)

    except Exception as exc:
        # Notification failure must never affect job status
        log.warning("[notify] Failed to send render-complete email for job %s: %s", job_id, exc)


def send_render_failed(job_id: str, project_name: str, owner_id: str) -> None:
    """Send a render-failed email to the project owner. Never raises."""
    if not _from_email:
        return

    try:
        email = _get_owner_email(owner_id)
        if not email:
            return

        render_url = f"{_app_url}/render/{job_id}"

        _ses.send_email(
            Source=_from_email,
            Destination={"ToAddresses": [email]},
            Message={
                "Subject": {"Data": f"RazorVid render failed — {project_name}"},
                "Body": {
                    "Text": {
                        "Data": (
                            f"Your multicam render for \"{project_name}\" encountered an error.\n\n"
                            f"View details and resubmit here:\n{render_url}\n\n"
                            "— The RazorVid Team"
                        )
                    },
                    "Html": {
                        "Data": (
                            f"<p>Your multicam render for <strong>{project_name}</strong> encountered an error.</p>"
                            f"<p><a href=\"{render_url}\">View details and resubmit</a></p>"
                            "<p>— The RazorVid Team</p>"
                        )
                    },
                },
            },
        )
        log.info("[notify] Render-failed email sent to %s for job %s", email, job_id)

    except Exception as exc:
        log.warning("[notify] Failed to send render-failed email for job %s: %s", job_id, exc)
