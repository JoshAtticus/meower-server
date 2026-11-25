"""Email addresses, verification and the feature gate that depends on them."""
import hashlib, os, re, secrets, time
from pymongo import ReturnDocument

import mailer
import security
from database import db

GRACE_SECONDS = 14 * 86400
TOKEN_TTL = 24 * 3600
EMAIL_REGEX = re.compile(r"[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9\-]{1,63}(\.[A-Za-z0-9\-]{1,63})*\.[A-Za-z]{2,24}")

_rollout = None


def rollout_ts() -> int:
    """When verification was introduced; accounts older than this get a grace period."""
    global _rollout
    if _rollout is None:
        doc = db.server_config.find_one_and_update(
            {"_id": "email_rollout"}, {"$setOnInsert": {"ts": int(time.time())}},
            upsert=True, return_document=ReturnDocument.AFTER)
        _rollout = doc["ts"]
    return _rollout


def grace_deadline() -> int:
    return rollout_ts() + GRACE_SECONDS


def normalize(email: str) -> str:
    return (email or "").strip().lower()


def valid_email(email: str) -> bool:
    return len(email) <= 254 and bool(EMAIL_REGEX.fullmatch(email))


def domain_blocked(email: str) -> bool:
    domain = email.rsplit("@", 1)[-1].lower()
    parts = domain.split(".")
    candidates = [".".join(parts[i:]) for i in range(len(parts) - 1)]
    return db.email_blacklist.count_documents({"_id": {"$in": candidates}}, limit=1) > 0


def email_in_use(email: str, except_user: str = None) -> bool:
    query = {"email": email, "email_verified": True}
    if except_user:
        query["_id"] = {"$ne": except_user}
    return db.usersv0.count_documents(query, limit=1) > 0


def mask(email: str) -> str:
    if not email or "@" not in email:
        return ""
    name, domain = email.split("@", 1)
    return (name[:2] + "***" if len(name) > 2 else name[:1] + "***") + "@" + domain


def state(user_doc: dict) -> dict:
    verified = bool(user_doc.get("email_verified"))
    bot = bool((user_doc.get("flags") or 0) & security.UserFlags.BOT)
    legacy = (user_doc.get("created") or 0) < rollout_ts()
    deadline = grace_deadline() if legacy else None
    in_grace = bool(deadline and time.time() < deadline)
    return {
        "verified": verified,
        "email": mask(user_doc.get("email", "")),
        "has_email": bool(user_doc.get("email")),
        "deadline": None if (verified or bot) else deadline,
        "required": not (verified or bot or in_grace),
        "exempt": bot,
    }


PROJECTION = {"email": 1, "email_verified": 1, "created": 1, "flags": 1}


def account_state(username: str) -> dict:
    doc = db.usersv0.find_one({"_id": username}, projection=PROJECTION) or {}
    return state(doc)


def require_verified(username: str):
    """None when allowed, otherwise an (error, status) response tuple."""
    if account_state(username)["required"]:
        return {"error": True, "type": "emailNotVerified"}, 403
    return None


def uses_custom_emoji(content: str) -> bool:
    return bool(content and re.search(r"<:[A-Za-z0-9]{24}>", content))


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_token(username: str, email: str) -> str:
    token = secrets.token_urlsafe(32)
    db.email_tokens.delete_many({"user": username})
    db.email_tokens.insert_one({"_id": _hash(token), "user": username, "email": email,
                                "expires": int(time.time()) + TOKEN_TTL})
    return token


def consume_token(token: str):
    doc = db.email_tokens.find_one_and_delete({"_id": _hash(token or "")})
    if not doc or doc["expires"] < time.time():
        return None
    return doc


def _look(username: str):
    settings = db.user_settings.find_one({"_id": username}, projection={"theme": 1, "mode": 1}) or {}
    return settings.get("theme", "orange"), settings.get("mode") is False


def client_url() -> str:
    return os.getenv("CLIENT_URL", "https://meower.joshattic.us").rstrip("/")


async def send_verification(username: str, email: str) -> bool:
    token = create_token(username, email)
    link = f"{client_url()}/verify-email?token={token}"
    theme, dark = _look(username)
    html = mailer.render_email(
        "Verify your Meower email", "Verify your email",
        f"<p style='margin:0 0 14px 0;'>Hi <b>{username}</b>,</p>"
        "<p style='margin:0;'>Confirm this email address to finish setting up your account.</p>",
        preheader="Confirm your email address to unlock everything on Meower.",
        button="Verify email", link=link,
        callout="Until it's verified you can't send messages, upload files, react, use custom emoji, or send friend requests, message requests and chat invites.",
        footer="This link expires in 24 hours. If you didn't add this email to a Meower account, you can ignore this message.",
        theme=theme, dark=dark)
    return await mailer.send_email(email, "Verify your Meower email", html)


async def send_security_email(username: str, heading: str, message: str, to: str = None) -> bool:
    """Security notice to the account's verified email (or an explicit address)."""
    if not to:
        doc = db.usersv0.find_one({"_id": username}, projection={"email": 1, "email_verified": 1}) or {}
        if not doc.get("email_verified"):
            return False
        to = doc["email"]
    theme, dark = _look(username)
    html = mailer.render_email(
        heading, heading,
        f"<p style='margin:0 0 14px 0;'>Hi <b>{username}</b>,</p><p style='margin:0;'>{message}</p>",
        preheader=heading, button="Review my account", link=f"{client_url()}/settings",
        callout="If this wasn't you, reset your password and log out of all devices right away.",
        footer="You get this email because it's the verified address on your Meower account.",
        theme=theme, dark=dark)
    return await mailer.send_email(to, f"Meower security alert: {heading}", html)


def send_legacy_notice_if_needed(username: str):
    """One-off inbox message telling existing users to add and verify an email."""
    doc = db.usersv0.find_one({"_id": username}, projection={**PROJECTION, "email_notice_sent": 1}) or {}
    st = state(doc)
    if doc.get("email_notice_sent") or st["verified"] or st["exempt"] or not st["deadline"]:
        return False
    db.usersv0.update_one({"_id": username}, {"$set": {"email_notice_sent": True}})
    date = time.strftime("%d %B %Y", time.gmtime(st["deadline"]))
    return date
