# nfty_kit.py
from utils import tool
import os
import uuid
import json
import requests

kit_name        = "Ntfy"
kit_description = "Send push notifications to a self-hosted ntfy instance."
requirements    = ["requests"]
config          = {"NTFY_URL": "https://nfty.koholint.net"}

def _base_url() -> str:
    return os.getenv("NTFY_URL", "https://nfty.koholint.net").rstrip("/")

@tool
def ntfy_send(topic: str, message: str, title: str = "", priority: str = "default", tags: str = "") -> dict:
    """
    WHEN TO USE: Use this to send a push notification to the user's phone or device via ntfy.
    Call this whenever the user asks to be notified, send an alert, ping their phone, or
    push a message. Also use proactively when finishing a long task the user asked to be
    notified about on completion.

    topic: The ntfy topic to publish to (e.g. "phone_notifs", "alerts"). No slashes.
    message: The body of the notification.
    title: Optional title shown above the message. Leave empty to omit.
    priority: Notification priority. One of: "min", "low", "default", "high", "urgent". Default is "default".
    tags: Comma-separated ntfy tag names or emoji shortcodes (e.g. "warning,skull" or "tada").
          Leave empty to omit. See https://docs.ntfy.sh/emojis/ for valid shortcodes.

    Returns {"status": "ok", "topic": str, "url": str} on success or {"error": str} on failure.
    """
    url = f"{_base_url()}/{topic}"
    headers = {}
    if title:
        headers["Title"] = title
    if priority and priority != "default":
        headers["Priority"] = priority
    if tags:
        headers["Tags"] = tags

    try:
        resp = requests.post(url, data=message.encode("utf-8"), headers=headers, timeout=10)
        resp.raise_for_status()
        return {"status": "ok", "topic": topic, "url": url}
    except requests.exceptions.HTTPError as e:
        return {"error": f"HTTP {resp.status_code}: {resp.text}"}
    except requests.exceptions.ConnectionError:
        return {"error": f"Could not connect to ntfy at {_base_url()}. Check NTFY_URL config."}
    except Exception as e:
        return {"error": str(e)}


@tool
def ntfy_send_link(topic: str, message: str, link_url: str, title: str = "", priority: str = "default") -> dict:
    """
    WHEN TO USE: Use this to send a push notification that includes a clickable action button
    linking to a URL. Good for "check this out", "view result", or "open page" scenarios.

    topic: The ntfy topic to publish to (e.g. "phone_notifs").
    message: The body of the notification.
    link_url: A URL to attach as a "View" action button on the notification.
    title: Optional title shown above the message. Leave empty to omit.
    priority: One of: "min", "low", "default", "high", "urgent".

    Returns {"status": "ok", "topic": str} on success or {"error": str} on failure.
    """
    url = f"{_base_url()}/{topic}"
    headers = {
        "Actions": f"view, Open, {link_url}"
    }
    if title:
        headers["Title"] = title
    if priority and priority != "default":
        headers["Priority"] = priority

    try:
        resp = requests.post(url, data=message.encode("utf-8"), headers=headers, timeout=10)
        resp.raise_for_status()
        return {"status": "ok", "topic": topic, "url": url}
    except requests.exceptions.HTTPError:
        return {"error": f"HTTP {resp.status_code}: {resp.text}"}
    except requests.exceptions.ConnectionError:
        return {"error": f"Could not connect to ntfy at {_base_url()}. Check NTFY_URL config."}
    except Exception as e:
        return {"error": str(e)}


@tool
def ntfy_prompt(topic: str, message: str, choices: str, title: str = "", timeout: int = 300) -> dict:
    """
    WHEN TO USE: Use this to ask the user a question and wait for their answer via their phone.
    Sends a notification with up to 3 labeled action buttons. The tool blocks until the user
    taps one of the buttons, then returns which choice they made. The chat will appear to hang
    while waiting — that is intentional. Use this when you need the user's input before
    proceeding and they may not be at their computer (e.g. "Should I delete these files? Yes/No",
    "Which environment should I deploy to? staging/prod/cancel").

    topic: The ntfy topic to publish to (e.g. "phone_notifs"). No slashes.
    message: The question or prompt body shown in the notification.
    choices: Comma-separated list of 2 or 3 button labels (e.g. "Yes,No" or "Deploy,Skip,Cancel").
             Labels must not contain commas or semicolons. Max 3 choices.
    title: Optional title for the notification. Leave empty to omit.
    timeout: How many seconds to wait for a response before giving up (default 300 = 5 minutes).

    Returns {"choice": str, "topic": str} with the label of the button the user tapped,
    or {"error": str} if the send failed or timed out.
    """
    base = _base_url()

    # Generate a unique one-time callback topic for this prompt
    callback_topic = f"_cb_{uuid.uuid4().hex[:16]}"
    callback_url = f"{base}/{callback_topic}"

    # Parse choices — max 3, strip whitespace
    labels = [c.strip() for c in choices.split(",")][:3]
    if len(labels) < 2:
        return {"error": "choices must contain at least 2 comma-separated labels"}

    # Build http action buttons — each POSTs its own label to the callback topic
    # ntfy http action format: http, <label>, <url>[, body=<body>]
    actions = "; ".join(
        f"http, {label}, {callback_url}, body={label}, clear=true"
        for label in labels
    )

    pub_url = f"{base}/{topic}"
    headers = {"Actions": actions}
    if title:
        headers["Title"] = title

    try:
        resp = requests.post(pub_url, data=message.encode("utf-8"), headers=headers, timeout=10)
        resp.raise_for_status()
    except requests.exceptions.HTTPError:
        return {"error": f"Failed to send prompt: HTTP {resp.status_code}: {resp.text}"}
    except Exception as e:
        return {"error": f"Failed to send prompt: {e}"}

    # Subscribe to the callback topic via SSE and block until a message arrives
    sse_url = f"{callback_url}/sse"
    try:
        with requests.get(sse_url, stream=True, timeout=timeout) as stream:
            stream.raise_for_status()
            for line in stream.iter_lines():
                if not line:
                    continue
                decoded = line.decode("utf-8") if isinstance(line, bytes) else line
                if not decoded.startswith("data:"):
                    continue
                raw = decoded[len("data:"):].strip()
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                # Skip the keepalive "open" event
                if event.get("event") == "open":
                    continue
                # The message body is the label the user tapped
                choice = event.get("message", "").strip()
                if choice in labels:
                    return {"choice": choice, "topic": topic}
                # Got a message but it wasn't one of our labels — keep waiting
    except requests.exceptions.Timeout:
        return {"error": f"Timed out after {timeout}s waiting for a response"}
    except Exception as e:
        return {"error": f"Error while waiting for response: {e}"}

    return {"error": "SSE stream ended without a valid response"}
