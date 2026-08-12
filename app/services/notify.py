"""Discord notifications for events that need attention.

Deliberately fire-and-forget: a failing webhook must never turn a working sync
into a failed one, so every error here is logged and swallowed. Notifications
are disabled unless DISCORD_WEBHOOK_URL is set.
"""

import logging

from app.core.config import settings

log = logging.getLogger(__name__)

# Discord rejects messages above 2000 characters; leave room for the title.
_MAX_CONTENT = 1900


def notifications_enabled() -> bool:
    return bool((settings.discord_webhook_url or "").strip())


def notify(title: str, message: str = "") -> bool:
    """Post a message to the configured Discord webhook.

    Returns True if Discord accepted it. Never raises.
    """
    url = (settings.discord_webhook_url or "").strip()
    if not url:
        return False

    content = f"**{title}**"
    if message:
        content = f"{content}\n{message}"
    if len(content) > _MAX_CONTENT:
        content = content[: _MAX_CONTENT - 1] + "…"

    try:
        import httpx

        response = httpx.post(url, json={"content": content}, timeout=10)
        response.raise_for_status()
        return True
    except Exception as exc:
        # Never propagate: the caller is usually in the middle of a sync.
        log.warning("Discord notification failed: %s", exc)
        return False


def notify_session_expired(session_id: str | None, message: str) -> None:
    if not settings.notify_on_session_expired:
        return
    notify(
        "Trade Republic session expired",
        f"tr-sync can no longer reach Trade Republic ({message}).\n"
        "Scheduled syncs will fail until you log in again via the web UI.",
    )


def notify_sync_failure(kind: str, error: str) -> None:
    if not settings.notify_on_sync_failure:
        return
    notify(f"tr-sync: {kind} failed", str(error))