import re, time, secrets, pyotp
from pydantic import BaseModel, Field
from typing import Optional
from quart import Blueprint, request, abort, current_app as app
from quart_schema import validate_request, validate_querystring

import security
from database import db, get_total_pages


bots_bp = Blueprint("bots_bp", __name__, url_prefix="/bots")


class CreateBotBody(BaseModel):
    name: str = Field(min_length=1, max_length=20)
    description: Optional[str] = Field(default="", max_length=250)

    class Config:
        str_strip_whitespace = True


class UpdateBotBody(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=20)
    description: Optional[str] = Field(default=None, max_length=250)

    class Config:
        str_strip_whitespace = True


class GetBotsQueryArgs(BaseModel):
    page: Optional[int] = Field(default=1, ge=1)


def bot_guard():
    """Bots can't manage bots."""
    if request.flags & security.UserFlags.BOT:
        return {"error": True, "type": "botsCannotManageBots"}, 403


def check_mfa(totp_code) -> Optional[tuple]:
    """Returns an error response tuple if MFA fails, else None."""
    if db.authenticators.count_documents({"user": request.user, "type": "totp"}, limit=1):
        if not totp_code:
            return {"error": True, "type": "mfaRequired"}, 403
        for authenticator in db.authenticators.find({"user": request.user, "type": "totp"}):
            if pyotp.TOTP(authenticator["totp_secret"]).verify(totp_code, valid_window=1):
                return None
        return {"error": True, "type": "invalidTOTPCode"}, 401
    return None


def serialize_bot(bot: dict, token: Optional[str] = None) -> dict:
    account = db.usersv0.find_one({"_id": bot["_id"]}, projection={"avatar": 1})
    out = {
        "error": False,
        "id": bot["_id"],
        "name": bot["_id"],
        "description": bot["description"],
        "owner": bot["owner"],
        "icon": (account or {}).get("avatar") or "",
    }
    if token is not None:
        out["token"] = token
    return out


def get_owned_bot(bot_id: str) -> dict:
    bot = db.bots.find_one({"_id": bot_id})
    if not bot:
        abort(404)
    if bot["owner"] != request.user:
        abort(403)
    return bot


@bots_bp.post("/")
@validate_request(CreateBotBody)
async def create_bot(data: CreateBotBody):
    # Check authorization
    if not request.user:
        abort(401)

    # Bots cannot create bots
    if resp := bot_guard():
        return resp

    # Check ratelimit
    if security.ratelimited(f"bots:create:{request.user}"):
        abort(429)
    security.ratelimit(f"bots:create:{request.user}", 5, 60)

    # Make sure the name is legal
    if not re.fullmatch(security.USERNAME_REGEX, data.name):
        return {"error": True, "type": "illegalCharacters"}, 400

    # Make sure the name isn't taken
    if security.account_exists(data.name, ignore_case=True):
        return {"error": True, "type": "usernameExists"}, 409

    # Limit bots per owner
    if db.bots.count_documents({"owner": request.user}) >= 10:
        return {"error": True, "type": "tooManyBots"}, 403

    # Create the bot account and doc
    token = security.create_bot_account(data.name, request.user)
    bot = {
        "_id": data.name,
        "owner": request.user,
        "description": data.description or "",
        "created": int(time.time())
    }
    db.bots.insert_one(bot)

    return serialize_bot(bot, token=token), 200


@bots_bp.get("/")
@validate_querystring(GetBotsQueryArgs)
async def get_bots(query_args: GetBotsQueryArgs):
    # Check authorization
    if not request.user:
        abort(401)

    # Bots cannot manage bots
    if resp := bot_guard():
        return resp

    query = {"owner": request.user}
    bots = []
    for bot in db.bots.find(
        query,
        sort=[("created", -1)],
        skip=(query_args.page - 1) * 25,
        limit=25
    ):
        account = db.usersv0.find_one({"_id": bot["_id"]}, projection={"avatar": 1})
        bots.append({
            "id": bot["_id"],
            "name": bot["_id"],
            "icon": (account or {}).get("avatar") or ""
        })
    return {
        "error": False,
        "bots": bots,
        "page": query_args.page,
        "pages": get_total_pages("bots", query)
    }, 200


@bots_bp.get("/<bot_id>")
async def get_bot(bot_id):
    # Public, no auth required
    bot = db.bots.find_one({"_id": bot_id})
    if not bot:
        return {"error": True, "type": "notFound"}, 404
    return serialize_bot(bot), 200


@bots_bp.patch("/<bot_id>")
@validate_request(UpdateBotBody)
async def update_bot(bot_id, data: UpdateBotBody):
    # Check authorization
    if not request.user:
        abort(401)

    # Bots cannot manage bots
    if resp := bot_guard():
        return resp

    bot = get_owned_bot(bot_id)

    # Usernames are immutable
    if data.name is not None and data.name != bot["_id"]:
        return {"error": True, "type": "nameImmutable"}, 400

    # Update description
    if data.description is not None:
        bot["description"] = data.description
        db.bots.update_one({"_id": bot_id}, {"$set": {"description": data.description}})

    return serialize_bot(bot), 200


@bots_bp.delete("/<bot_id>")
async def delete_bot(bot_id):
    # Check authorization
    if not request.user:
        abort(401)

    # Bots cannot manage bots
    if resp := bot_guard():
        return resp

    bot = get_owned_bot(bot_id)

    # MFA check (required if the owner has TOTP authenticators)
    body = await request.get_json(silent=True) or {}
    if resp := check_mfa(body.get("totp_code")):
        return resp

    # Delete the bot
    security.delete_account(bot["_id"], purge=True)
    db.bots.delete_one({"_id": bot["_id"]})

    return {"error": False}, 200


@bots_bp.post("/<bot_id>/token/regenerate")
async def regenerate_bot_token(bot_id):
    # Check authorization
    if not request.user:
        abort(401)

    # Bots cannot manage bots
    if resp := bot_guard():
        return resp

    bot = get_owned_bot(bot_id)

    # MFA check (required if the owner has TOTP authenticators)
    body = await request.get_json(silent=True) or {}
    if resp := check_mfa(body.get("totp_code")):
        return resp

    # Replace the bot's tokens with a single new one
    token = secrets.token_urlsafe(security.TOKEN_BYTES)
    db.usersv0.update_one({"_id": bot["_id"]}, {"$set": {"tokens": [token]}})

    # Kick existing cloudlink sessions (same mechanism as DELETE /me/tokens)
    for client in app.cl.usernames.get(bot["_id"], []):
        client.kick()

    return {"error": False, "token": token}, 200
