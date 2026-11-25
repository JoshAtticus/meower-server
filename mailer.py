import asyncio, os, smtplib, ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from html import escape
from pathlib import Path
import re

TEMPLATE = (Path(__file__).parent / "email_templates" / "base.html").read_text(encoding="utf-8")

# Accent colours of the built-in client themes (client/src/lib/themes.js)
THEME_ACCENTS = {
    "red": "#ff3838", "orange": "#f9a636", "green": "#3eca6f", "blue": "#4d97ff",
    "pink": "#ff8af7", "purple": "#731bc4", "magenta": "#a85ff5", "gray": "#575656",
}


def _rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))


def _mix(a, b, amount):
    """Mix colour a into b by amount (0-1)."""
    ra, rb = _rgb(a), _rgb(b)
    return "#%02x%02x%02x" % tuple(round(rb[i] + (ra[i]-rb[i]) * amount) for i in range(3))


def _luminance(h):
    r, g, b = [c / 255 for c in _rgb(h)]
    return 0.2126*r + 0.7152*g + 0.0722*b


def palette(theme=None, dark=False):
    theme_id = theme if theme in THEME_ACCENTS else "orange"
    accent = THEME_ACCENTS[theme_id]
    if dark:
        pal = {"page_bg": "#202020", "card_bg": _mix(accent, "#222222", 0.12), "border": _mix(accent, "#2c2c2c", 0.45),
               "text": "#f2f2f2", "muted": "#a3a9a5", "callout_bg": _mix(accent, "#222222", 0.22)}
    else:
        pal = {"page_bg": _mix(accent, "#ffffff", 0.14), "card_bg": "#ffffff", "border": _mix(accent, "#ffffff", 0.45),
               "text": "#1f1f1f", "muted": "#6b6b6b", "callout_bg": _mix(accent, "#ffffff", 0.14)}
    pal.update(accent=accent, button_text="#eeeeff" if dark else "#ffffff", theme_id=theme_id)
    return pal


def render_email(subject, heading, body_html, *, preheader="", button=None, link=None, footer="", callout=None, theme=None, dark=False):
    """HTML for a Meower email, themed to the recipient's accent colour and mode."""
    pal = palette(theme, dark)
    base = os.getenv("CLIENT_URL", "https://meower.joshattic.us").rstrip("/")
    button_row = ""
    if button and link:
        button_row = (
            f'<table role="presentation" cellpadding="0" cellspacing="0" style="margin:26px 0 0 0;"><tr>'
            f'<td style="background:{pal["accent"]};border:2px solid {pal["button_text"]};border-radius:8px;"><a href="{escape(link)}" style="display:inline-block;padding:10px 24px;'
            f'color:{pal["button_text"]};text-decoration:none;font-weight:700;font-size:16px;font-family:simvoni,Segoe UI,Roboto,Helvetica,Arial,sans-serif;">{escape(button)}</a></td></tr></table>'
            f'<p style="margin:16px 0 0 0;font-size:13px;line-height:1.55;color:{pal["muted"]};">Button not working? Paste this link into your browser:<br>'
            f'<a href="{escape(link)}" style="color:{pal["accent"] if dark else pal["text"]};word-break:break-all;">{escape(link)}</a></p>'
        )
    callout_row = ""
    if callout:
        callout_row = (
            f'<div style="margin:22px 0 0 0;padding:16px 18px;background:{pal["callout_bg"]};border:1px solid {pal["border"]};'
            f'border-radius:14px;font-size:15px;line-height:1.55;color:{pal["text"]};">{callout}</div>'
        )
    values = dict(pal, callout_row=callout_row, subject=escape(subject), preheader=escape(preheader), heading=escape(heading), body=body_html,
                  footer=footer, button_row=button_row, wordmark=f"{base}/email/wordmark-{pal['theme_id']}.png", base=base)
    return re.sub(r"\{\{(\w+)\}\}", lambda m: str(values.get(m.group(1), "")), TEMPLATE)


def html_to_text(html):
    text = re.sub(r"<(br|/p|/h1|/div|/tr)[^>]*>", "\n", html)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\n{3,}", "\n\n", text.replace("&amp;", "&")).strip()


def _send_blocking(to, subject, html):
    host = os.getenv("SMTP_HOST")
    user = os.getenv("SMTP_USER")
    if not host or not user:
        return False
    sender = os.getenv("SMTP_FROM", user)
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr(("Meower", sender))
    msg["To"] = to
    msg["Message-ID"] = make_msgid(domain=sender.split("@")[-1])
    msg.set_content(html_to_text(html))
    msg.add_alternative(html, subtype="html")
    port = int(os.getenv("SMTP_PORT", "587"))
    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=15, context=ctx) as s:
            s.login(user, os.getenv("SMTP_PASSWORD", ""))
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=15) as s:
            s.starttls(context=ctx)
            s.login(user, os.getenv("SMTP_PASSWORD", ""))
            s.send_message(msg)
    return True


async def send_email(to, subject, html):
    """Sends an email without blocking the event loop; returns False if SMTP isn't configured or fails."""
    try:
        return await asyncio.to_thread(_send_blocking, to, subject, html)
    except Exception:
        return False
