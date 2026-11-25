from quart import Blueprint, request, abort
from quart_schema import validate_request
from pydantic import BaseModel, Field
import time

import email_verification as ev
import security
from database import db


email_bp = Blueprint("email_bp", __name__, url_prefix="/me/email")


class SetEmailBody(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=255)


class VerifyEmailBody(BaseModel):
    token: str = Field(min_length=10, max_length=200)


def _human_only():
    if request.flags & security.UserFlags.BOT:
        return {"error": True, "type": "botsCannotManageBots"}, 403


@email_bp.get("/")
async def get_email_status():
    if not request.user:
        abort(401)
    return {"error": False, **ev.account_state(request.user)}, 200


@email_bp.put("/")
@validate_request(SetEmailBody)
async def set_email(data: SetEmailBody):
    if not request.user:
        abort(401)
    if resp := _human_only():
        return resp
    if security.ratelimited(f"email:{request.user}"):
        abort(429)
    security.ratelimit(f"email:{request.user}", 5, 600)

    account = db.usersv0.find_one({"_id": request.user}, projection={"pswd": 1, "email": 1, "email_verified": 1})
    if not security.check_password_hash(data.password, account["pswd"]):
        return {"error": True, "type": "invalidCredentials"}, 401

    email = ev.normalize(data.email)
    if not ev.valid_email(email):
        return {"error": True, "type": "invalidEmail"}, 400
    if ev.domain_blocked(email):
        return {"error": True, "type": "emailBlocked"}, 403
    if ev.email_in_use(email, request.user):
        return {"error": True, "type": "emailInUse"}, 409

    old = account.get("email") if account.get("email_verified") else None
    db.usersv0.update_one({"_id": request.user}, {"$set": {"email": email, "email_verified": False}})
    sent = await ev.send_verification(request.user, email)
    if old and old != email:
        await ev.send_security_email(
            request.user, "Your email address was changed",
            f"The email on your account was changed to {ev.mask(email)}.", to=old)
    return {"error": False, "sent": sent, **ev.account_state(request.user)}, 200


@email_bp.post("/resend")
async def resend_verification():
    if not request.user:
        abort(401)
    if security.ratelimited(f"email_resend:{request.user}"):
        abort(429)
    security.ratelimit(f"email_resend:{request.user}", 3, 600)

    account = db.usersv0.find_one({"_id": request.user}, projection={"email": 1, "email_verified": 1})
    if not account.get("email"):
        return {"error": True, "type": "noEmail"}, 400
    if account.get("email_verified"):
        return {"error": True, "type": "alreadyVerified"}, 400
    return {"error": False, "sent": await ev.send_verification(request.user, account["email"])}, 200


@email_bp.post("/verify")
@validate_request(VerifyEmailBody)
async def verify_email(data: VerifyEmailBody):
    ip_key = f"email_verify:{request.ip}"
    if security.ratelimited(ip_key):
        abort(429)
    security.ratelimit(ip_key, 10, 300)

    doc = ev.consume_token(data.token)
    if not doc:
        return {"error": True, "type": "invalidToken"}, 400
    account = db.usersv0.find_one({"_id": doc["user"]}, projection={"email": 1})
    if not account or account.get("email") != doc["email"]:
        return {"error": True, "type": "invalidToken"}, 400
    if ev.email_in_use(doc["email"], doc["user"]):
        return {"error": True, "type": "emailInUse"}, 409
    db.usersv0.update_one({"_id": doc["user"]}, {"$set": {"email_verified": True, "email_verified_at": int(time.time())}})
    return {"error": False, "user": doc["user"]}, 200
