import re
from quart import Blueprint, current_app as app, request, abort, send_file
import os
from quart_schema import validate_request, validate_querystring
from pydantic import BaseModel, Field
from typing import Optional, List, Literal
from copy import copy
import pymongo
import uuid
import time
import pyotp
import qrcode, qrcode.image.svg
import uuid
import secrets

import security
import username_change
import data_export as data_export_builder
from database import db, rdb, get_total_pages
from uploads import claim_file, unclaim_file
from utils import log


me_bp = Blueprint("me_bp", __name__, url_prefix="/me")


class DeleteAccountBody(BaseModel):
    password: str = Field(min_length=1, max_length=255)  # change in API v1

class UpdateConfigBody(BaseModel):
    pfp_data: Optional[int] = Field(default=None)
    avatar: Optional[str] = Field(default=None, max_length=24)
    avatar_color: Optional[str] = Field(default=None, min_length=6, max_length=6)  # hex code without the #
    quote: Optional[str] = Field(default=None, max_length=360)
    display_name: Optional[str] = Field(default=None)
    unread_inbox: Optional[bool] = Field(default=None)
    theme: Optional[str] = Field(default=None, min_length=1, max_length=256)
    mode: Optional[bool] = Field(default=None)
    layout: Optional[str] = Field(default=None, min_length=1, max_length=256)
    sfx: Optional[bool] = Field(default=None)
    bgm: Optional[bool] = Field(default=None)
    bgm_song: Optional[int] = Field(default=None)
    debug: Optional[bool] = Field(default=None)
    hide_blocked_users: Optional[bool] = Field(default=None)
    hide_presence: Optional[bool] = Field(default=None)
    profile_theme: Optional[bool] = Field(default=None)
    favorited_chats: Optional[List[str]] = Field(default=None)

    class Config:
        validate_assignment = True
        str_strip_whitespace = True

class ChangePasswordBody(BaseModel):
    old: str = Field(min_length=1, max_length=255)  # change in API v1
    new: str = Field(min_length=8, max_length=72)

class AddAuthenticatorBody(BaseModel):
    password: str = Field(min_length=1, max_length=255)  # change in API v1
    type: Literal["totp"] = Field()
    nickname: str = Field(default="", max_length=32)
    totp_secret: Optional[str] = Field(default=None, min_length=32, max_length=32)
    totp_code: Optional[str] = Field(default=None, min_length=6, max_length=6)

class UpdateAuthenticatorBody(BaseModel):
    nickname: str = Field(default="", max_length=32)

class RemoveAuthenticatorBody(BaseModel):
    password: str = Field(min_length=1, max_length=255)  # change in API v1

class ResetMFARecoveryCodeBody(BaseModel):
    password: str = Field(min_length=1, max_length=255)  # change in API v1

class GetReportsQueryArgs(BaseModel):
    page: Optional[int] = Field(default=1, ge=1)


def bot_guard():
    """Bots can't use self-service account-management endpoints."""
    if request.flags & security.UserFlags.BOT:
        return {"error": True, "type": "botsCannotDoThis"}, 403


@me_bp.get("/")
async def get_me():
    # Check authorization
    if not request.user:
        abort(401)
        
    # Get and return account
    return {"error": False, **security.get_account(request.user, include_config=True)}, 200


@me_bp.delete("/")
@validate_request(DeleteAccountBody)
async def delete_account(data: DeleteAccountBody):
    # Check authorization
    if not request.user:
        abort(401)

    if resp := bot_guard():
        return resp

    # Check ratelimit
    if security.ratelimited(f"login:u:{request.user}"):
        abort(429)

    # Check password
    account = db.usersv0.find_one({"_id": request.user}, projection={"pswd": 1})
    if not security.check_password_hash(data.password, account["pswd"]):
        security.ratelimit(f"login:u:{request.user}", 5, 60)
        return {"error": True, "type": "invalidCredentials"}, 401
    
    # Schedule account for deletion
    db.usersv0.update_one({"_id": request.user}, {"$set": {
        "tokens": [],
        "delete_after": int(time.time())+604800  # 7 days
    }})

    # Disconnect clients
    for client in app.cl.usernames.get(request.user, []):
        client.kick()

    return {"error": False}, 200


@me_bp.patch("/config")
@validate_request(UpdateConfigBody)
async def update_config(data: UpdateConfigBody):
    # Check authorization
    if not request.user:
        abort(401)

    # Check ratelimit
    if security.ratelimited(f"config:{request.user}"):
        abort(429)
    
    # Ratelimit
    security.ratelimit(f"config:{request.user}", 10, 5)

    # Get new config
    new_config = data.model_dump()

    # An explicit null display_name clears it
    if "display_name" in new_config and new_config["display_name"] is None:
        new_config["display_name"] = ""

    # Filter values that are set to None
    for k, v in copy(new_config).items():
        if v is None:
            del new_config[k]

    # Validate display name: trimmed, 1-32 chars when set, no control chars
    if "display_name" in new_config:
        display_name = new_config["display_name"].strip()
        if len(display_name) > 32 or any(
            ord(c) < 0x20 or ord(c) == 0x7F for c in display_name
        ):
            return {"error": True, "type": "invalidDisplayName"}, 400
        new_config["display_name"] = display_name

    # Delete updated profile data if account is restricted
    if security.is_restricted(request.user, security.Restrictions.EDITING_PROFILE):
        if "pfp_data" in new_config:
            del new_config["pfp_data"]
        if "avatar" in new_config:
            del new_config["avatar"]
        if "avatar_color" in new_config:
            del new_config["avatar_color"]
        if "quote" in new_config:
            del new_config["quote"]
        if "display_name" in new_config:
            del new_config["display_name"]

    # Claim avatar (and delete old one)
    if "avatar" in new_config:
        cur_avatar = db.usersv0.find_one({"_id": request.user}, projection={"avatar": 1})["avatar"]
        if new_config["avatar"] != "":
            try:
                claim_file(new_config["avatar"], "icons", request.user)
            except Exception as e:
                log(f"Unable to claim avatar: {e}")
                return {"error": True, "type": "unableToClaimAvatar"}, 500
        if cur_avatar:
            try:
                unclaim_file(cur_avatar)
            except Exception as e:
                log(f"Unable to delete avatar: {e}")

    # Update config
    security.update_settings(request.user, new_config)

    # Sync config between sessions
    app.cl.send_event("update_config", new_config, usernames=[request.user])

    # Update presence visibility and broadcast the new ulist
    if "hide_presence" in new_config:
        if new_config["hide_presence"]:
            app.cl.hidden_usernames.add(request.user)
        else:
            app.cl.hidden_usernames.discard(request.user)
        app.cl.send_ulist()

    # Send updated pfp and quote to other clients
    updated_profile_data = {"_id": request.user}
    if "pfp_data" in new_config:
        updated_profile_data["pfp_data"] = new_config["pfp_data"]
    if "avatar" in new_config:
        updated_profile_data["avatar"] = new_config["avatar"]
    if "avatar_color" in new_config:
        updated_profile_data["avatar_color"] = new_config["avatar_color"]
    if "quote" in new_config:
        updated_profile_data["quote"] = new_config["quote"]
    if "display_name" in new_config:
        updated_profile_data["display_name"] = new_config["display_name"]
    if len(updated_profile_data) > 1:
        app.cl.send_event("update_profile", updated_profile_data)

    return {"error": False}, 200


@me_bp.get("/relationships")
async def get_relationships():
    # Check authorization
    if not request.user:
        abort(401)

    return {
        "error": False,
        "autoget": [{
            "username": r["_id"]["to"],
            "state": r["state"],
            "updated_at": r["updated_at"]
        } for r in db.relationships.find({"_id.from": request.user})],
        "page#": 1,
        "pages": 1
    }, 200


@me_bp.get("/friends")
async def get_friends():
    # Check authorization
    if not request.user:
        abort(401)

    # Relationships I have toward others
    mine = {r["_id"]["to"]: r["state"] for r in db.relationships.find({"_id.from": request.user})}

    # Users who have sent me a friend request (state 1 toward me)
    incoming_requests = {r["_id"]["from"] for r in db.relationships.find({
        "_id.to": request.user,
        "state": 1
    })}

    outgoing = {u for u, state in mine.items() if state == 1}
    friends = sorted(outgoing & incoming_requests)
    return {
        "error": False,
        "friends": friends,
        "incoming": sorted(incoming_requests - outgoing - {u for u, s in mine.items() if s == 2}),
        "outgoing": sorted(outgoing - incoming_requests)
    }, 200


class ChangeUsernameBody(BaseModel):
    username: str = Field(min_length=1, max_length=20)
    password: str = Field(min_length=1, max_length=255)
    totp_code: Optional[str] = Field(default=None, min_length=6, max_length=6)

    class Config:
        str_strip_whitespace = True


@me_bp.post("/username")
@validate_request(ChangeUsernameBody)
async def change_username(data: ChangeUsernameBody):
    # Check authorization
    if not request.user:
        abort(401)

    if resp := bot_guard():
        return resp

    # Check ratelimit
    if security.ratelimited(f"login:u:{request.user}") or security.ratelimited(f"username:{request.user}"):
        abort(429)

    account = db.usersv0.find_one({"_id": request.user})

    # Check password
    if not security.check_password_hash(data.password, account["pswd"]):
        security.ratelimit(f"login:u:{request.user}", 5, 60)
        return {"error": True, "type": "invalidCredentials"}, 401

    # Check 2FA
    authenticators = list(db.authenticators.find({"user": request.user, "type": "totp"}))
    if authenticators:
        if not data.totp_code:
            return {"error": True, "type": "mfaRequired"}, 403
        if not any(pyotp.TOTP(a["totp_secret"]).verify(data.totp_code, valid_window=1) for a in authenticators):
            security.ratelimit(f"login:u:{request.user}", 5, 60)
            return {"error": True, "type": "invalidTOTPCode"}, 401

    security.ratelimit(f"username:{request.user}", 10, 300)

    # Validate the new name
    if not re.fullmatch(security.USERNAME_REGEX, data.username):
        return {"error": True, "type": "illegalCharacters"}, 400
    if data.username == request.user:
        return {"error": True, "type": "sameUsername"}, 400

    # One change every 72 hours
    wait = username_change.cooldown_remaining(account)
    if wait:
        return {"error": True, "type": "usernameChangeCooldown", "retry_after": wait}, 429

    # Taken by someone else? (changing only the capitalisation of your own name is fine)
    if data.username.lower() != request.user.lower() and security.account_exists(data.username, ignore_case=True):
        return {"error": True, "type": "usernameExists"}, 409

    # Held for someone else after a rename? (it is still yours to take back)
    if username_change.name_blocked_for(data.username.lower(), account["uuid"]):
        return {"error": True, "type": "usernameReserved"}, 409

    old = request.user
    username_change.change_username(old, data.username)

    # Log everyone out of their live connections: they reconnect under the new name
    for client in list(app.cl.usernames.get(old, [])):
        client.kick()

    # Tell everyone who shares a chat or relationship with the account, so open
    # pages switch to the new name instead of showing the old one as offline
    for chat in db.chats.find({"members": data.username, "deleted": False}, projection={"members": 1, "owner": 1, "member_roles": 1, "type": 1, "dm_request": 1}):
        payload = {"_id": chat["_id"], "members": chat["members"]}
        if chat.get("type") == 0:
            payload["owner"] = chat.get("owner")
            payload["member_roles"] = chat.get("member_roles", {})
        if chat.get("dm_request"):
            payload["dm_request"] = chat["dm_request"]
        app.cl.send_event("update_chat", payload, usernames=[m for m in chat["members"] if m != data.username])
    related = set()
    for rel in db.relationships.find({"$or": [{"_id.from": data.username}, {"_id.to": data.username}]}):
        related.add(rel["_id"]["to"] if rel["_id"]["from"] == data.username else rel["_id"]["from"])
    if related:
        app.cl.send_event("update_friends", {}, usernames=list(related))

    app.supporter.create_post("inbox", data.username, f"Your username was changed from @{old} to @{data.username}. If this wasn't you, please secure your account immediately.", category="security")

    return {"error": False, "username": data.username}, 200


@me_bp.patch("/password")
@validate_request(ChangePasswordBody)
async def change_password(data: ChangePasswordBody):
    # Check authorization
    if not request.user:
        abort(401)

    if resp := bot_guard():
        return resp

    # Check ratelimit
    if security.ratelimited(f"login:u:{request.user}"):
        abort(429)

    # Check password
    account = db.usersv0.find_one({"_id": request.user}, projection={"pswd": 1})
    if not security.check_password_hash(data.old, account["pswd"]):
        security.ratelimit(f"login:u:{request.user}", 5, 60)
        return {"error": True, "type": "invalidCredentials"}, 401

    # Update password and remember the previous hash for 14 days
    db.usersv0.update_one({"_id": request.user}, {"$set": {
        "pswd": security.hash_password(data.new),
        "prev_pswd": {
            "hash": account["pswd"],
            "changed_at": int(time.time()),
            "ip": request.ip
        }
    }})

    # Send alert
    app.supporter.create_post("inbox", account["_id"], "Your account password has been changed. If this wasn't requested by you, please secure your account immediately.", category="security")

    return {"error": False}, 200


@me_bp.get("/authenticators")
async def get_authenticators():
    return {
        "error": False,
        "autoget": list(db.authenticators.find({"user": request.user}, projection={
            "_id": 1,
            "type": 1,
            "nickname": 1,
            "registered_at": 1,
        })),
        "page#": 1,
        "pages": 1
    }, 200


@me_bp.post("/authenticators")
@validate_request(AddAuthenticatorBody)
async def add_authenticator(data: AddAuthenticatorBody):
    # Check authorization
    if not request.user:
        abort(401)

    if resp := bot_guard():
        return resp

    # Validate
    if data.type == "totp" and data.totp_secret and data.totp_code:
        if not pyotp.TOTP(data.totp_secret).verify(data.totp_code, valid_window=1):
            return {"error": True, "type": "invalidTOTPCode"}, 401
    else:
        abort(400)

    # Check ratelimit
    if security.ratelimited(f"login:u:{request.user}"):
        abort(429)

    # Check password
    account = db.usersv0.find_one({"_id": request.user}, projection={"pswd": 1, "mfa_recovery_code": 1})
    if not security.check_password_hash(data.password, account["pswd"]):
        security.ratelimit(f"login:u:{request.user}", 5, 60)
        return {"error": True, "type": "invalidCredentials"}, 401
    
    # Register
    authenticator = {
        "_id": str(uuid.uuid4()),
        "user": request.user,
        "type": data.type,
        "nickname": data.nickname,
        "totp_secret": data.totp_secret,
        "registered_at": int(time.time())
    }
    db.authenticators.insert_one(authenticator)

    # Send alert
    app.supporter.create_post("inbox", account["_id"], "A multi-factor authenticator has been added to your account. If this wasn't requested by you, please secure your account immediately.", category="security")

    # Return authenticator and MFA recovery code
    del authenticator["user"]
    del authenticator["totp_secret"]
    return {
        "error": False,
        "authenticator": authenticator,
        "mfa_recovery_code": account["mfa_recovery_code"]
    }


@me_bp.patch("/authenticators/<authenticator_id>")
@validate_request(UpdateAuthenticatorBody)
async def update_authenticator(authenticator_id: str, data: UpdateAuthenticatorBody):
    # Check authorization
    if not request.user:
        abort(401)

    if resp := bot_guard():
        return resp

    # Get authenticator
    authenticator = db.authenticators.find_one({
        "_id": authenticator_id,
        "user": request.user
    }, projection={
        "_id": 1,
        "type": 1,
        "nickname": 1,
        "registered_at": 1,
    })
    if not authenticator:
        abort(404)

    # Update
    updated = {}
    if data.nickname:
        updated["nickname"] = data.nickname
    authenticator.update(updated)
    db.authenticators.update_one({
        "_id": authenticator_id,
        "user": request.user
    }, {"$set": updated})

    return {"error": False, **authenticator}


@me_bp.delete("/authenticators/<authenticator_id>")
@validate_request(RemoveAuthenticatorBody)
async def remove_authenticator(authenticator_id: str, data: RemoveAuthenticatorBody):
    # Check authorization
    if not request.user:
        abort(401)

    if resp := bot_guard():
        return resp

    # Check ratelimit
    if security.ratelimited(f"login:u:{request.user}"):
        abort(429)

    # Check password
    account = db.usersv0.find_one({"_id": request.user}, projection={"pswd": 1, "mfa_recovery_code": 1})
    if not security.check_password_hash(data.password, account["pswd"]):
        security.ratelimit(f"login:u:{request.user}", 5, 60)
        return {"error": True, "type": "invalidCredentials"}, 401

    # Unregister
    result = db.authenticators.delete_one({
        "_id": authenticator_id,
        "user": request.user
    })
    if result.deleted_count < 1:
        abort(404)

    # Send alert
    app.supporter.create_post("inbox", account["_id"], "A multi-factor authenticator has been removed from your account. If this wasn't requested by you, please secure your account immediately.", category="security")

    return {"error": False}


@me_bp.get("/authenticators/totp-secret")
async def get_new_totp_secret():
    # Check authorization
    if not request.user:
        abort(401)

    if resp := bot_guard():
        return resp

    # Create secret and provisioning URI
    secret = pyotp.random_base32()
    provisioning_uri = pyotp.TOTP(secret).provisioning_uri(name=request.user, issuer_name="Meower")

    # Create QR code
    qr = qrcode.make(provisioning_uri, image_factory=qrcode.image.svg.SvgPathImage)

    return {
        "error": False,
        "secret": secret,
        "provisioning_uri": provisioning_uri,
        "qr_code_svg": qr.to_string(encoding='unicode')
    }


@me_bp.post("/reset-mfa-recovery-code")
@validate_request(ResetMFARecoveryCodeBody)
async def reset_mfa_recovery_code(data: ResetMFARecoveryCodeBody):
    # Check authorization
    if not request.user:
        abort(401)

    if resp := bot_guard():
        return resp

    # Check ratelimit
    if security.ratelimited(f"login:u:{request.user}"):
        abort(429)

    # Check password
    account = db.usersv0.find_one({"_id": request.user}, projection={"pswd": 1})
    if not security.check_password_hash(data.password, account["pswd"]):
        security.ratelimit(f"login:u:{request.user}", 5, 60)
        return {"error": True, "type": "invalidCredentials"}, 401
    
    # Reset MFA recovery code
    mfa_recovery_code = secrets.token_hex(5)
    db.usersv0.update_one({"_id": request.user}, {"$set": {"mfa_recovery_code": mfa_recovery_code}})

    # Send alert
    app.supporter.create_post("inbox", account["_id"], "Your multi-factor authentication recovery code has been reset. If this wasn't requested by you, please secure your account immediately.", category="security")

    return {"error": False, "mfa_recovery_code": mfa_recovery_code}


@me_bp.delete("/tokens")
async def delete_tokens():
    # Check authorization
    if not request.user:
        abort(401)
    
    # Revoke tokens
    db.usersv0.update_one({"_id": request.user}, {"$set": {"tokens": []}})

    # Disconnect clients
    for client in app.cl.usernames.get(request.user, []):
        client.kick()

    return {"error": False}, 200


@me_bp.get("/reports")
@validate_querystring(GetReportsQueryArgs)
async def get_report_history(query_args: GetReportsQueryArgs):
    # Check authorization
    if not request.user:
        abort(401)

    # Get reports
    reports = list(
        db.reports.find(
            {"reports.user": request.user},
            projection={"escalated": 0},
            sort=[("reports.time", pymongo.DESCENDING)],
            skip=(query_args.page - 1) * 25,
            limit=25,
        )
    )

    # Get reason, comment, and time
    for report in reports:
        for _report in report["reports"]:
            if _report["user"] == request.user:
                report.update({
                    "reason": _report["reason"],
                    "comment": _report["comment"],
                    "time": _report["time"]
                })
        del report["reports"]

    # Get content
    for report in reports:
        if report["type"] == "post":
            report["content"] = db.posts.find_one(
                {"_id": report.get("content_id")},
                projection={"_id": 1, "u": 1, "isDeleted": 1}
            )
        elif report["type"] == "user":
            report["content"] = security.get_account(report.get("content_id"))

    # Return reports
    return {
        "error": False,
        "autoget": reports,
        "page#": query_args.page,
        "pages": get_total_pages("reports", {"reports.user": request.user}),
    }, 200


@me_bp.get("/export")
async def get_current_data_export():
    # Check authorization
    if not request.user:
        abort(401)

    if resp := bot_guard():
        return resp

    # Get current data export request from the database
    data_export = db.data_exports.find_one({
        "user": request.user,
        "$or": [
            {"status": "pending"},
            {"completed_at": {"$gt": int(time.time())-604800}}
        ]
    }, projection={"error": 0})
    if not data_export:
        abort(404)

    # Include the zip size once it's built
    if data_export.get("status") == "completed":
        path = data_export_builder.export_path(data_export["_id"])
        if os.path.exists(path):
            data_export["size"] = os.path.getsize(path)
        data_export["expires_at"] = data_export["completed_at"] + data_export_builder.EXPORT_TTL

    # Return data export request
    return data_export, 200


@me_bp.get("/export/download")
async def download_data_export():
    if not request.user:
        abort(401)

    if resp := bot_guard():
        return resp

    data_export = db.data_exports.find_one({
        "user": request.user,
        "status": "completed",
        "completed_at": {"$gt": int(time.time()) - data_export_builder.EXPORT_TTL}
    }, sort=[("completed_at", pymongo.DESCENDING)])
    if not data_export or not os.path.exists(data_export_builder.export_path(data_export["_id"])):
        abort(404)

    return await send_file(
        data_export_builder.export_path(data_export["_id"]),
        mimetype="application/zip",
        as_attachment=True,
        attachment_filename=f"meower-data-{request.user}.zip"
    )


@me_bp.post("/export")
async def request_data_export():
    # Check authorization
    if not request.user:
        abort(401)

    if resp := bot_guard():
        return resp

    # Make sure a current data export request doesn't already exist
    if db.data_exports.count_documents({
        "user": request.user,
        "$or": [
            {"status": "pending"},
            {"status": "completed", "completed_at": {"$gt": int(time.time())-604800}}
        ]
    }) > 0:
        abort(429)

    # Create data export request
    data_export = {
        "_id": str(uuid.uuid4()),
        "user": request.user,
        "status": "pending",
        "created_at": int(time.time())
    }
    db.data_exports.insert_one(data_export)

    # Build the export in the background
    data_export_builder.start_export(data_export["_id"], request.user)

    # Return data export request
    return data_export, 200
