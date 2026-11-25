from quart import Blueprint, current_app as app, request, abort
import secrets
import time

import security
import email_verification
import chat_permissions
from database import db


invite_links_bp = Blueprint("invite_links_bp", __name__, url_prefix="/invite-links")


def missing_permission():
    return {"error": True, "type": "missingPermission"}, 403


def get_manageable_chat(chat_id: str):
    """Group chat the requester may create or revoke invite links for, or an error response."""
    if not request.user:
        abort(401)
    if security.ratelimited(f"update_chat:{request.user}"):
        abort(429)
    security.ratelimit(f"update_chat:{request.user}", 10, 5)

    if security.is_restricted(request.user, security.Restrictions.NEW_CHATS):
        return None, ({"error": True, "type": "accountBanned"}, 403)

    chat = db.chats.find_one({"_id": chat_id, "members": request.user, "deleted": False})
    if not chat:
        abort(404)
    if chat["type"] != 0:
        abort(400)

    ban = security.chat_ban(chat, request.user)
    if ban:
        return None, ({"error": True, "type": "chatBanned", "expires": ban["expires"]}, 403)

    chat = chat_permissions.ensure_roles(chat)
    if not chat_permissions.has(chat, request.user, "invite_members"):
        return None, missing_permission()
    if not chat.get("invites_enabled", True) and request.user != chat["owner"] and \
       not chat_permissions.has(chat, request.user, "toggle_invites"):
        return None, ({"error": True, "type": "invitesDisabled"}, 403)
    return chat, None


def get_link_and_chat(code: str):
    link = db.chat_invite_links.find_one({"_id": code})
    if not link:
        abort(404)
    chat = db.chats.find_one({"_id": link["chat_id"], "deleted": False, "type": 0})
    if not chat:
        abort(404)
    if not chat.get("invites_enabled", True):
        abort(404)
    return link, chat


@invite_links_bp.post("/chat/<chat_id>")
async def create_link(chat_id):
    if resp := email_verification.require_verified(request.user):
        return resp
    chat, error = get_manageable_chat(chat_id)
    if error:
        return error

    # One shared link per chat
    link = db.chat_invite_links.find_one({"chat_id": chat_id})
    if not link:
        link = {
            "_id": secrets.token_urlsafe(6),
            "chat_id": chat_id,
            "created_by": request.user,
            "created": int(time.time()),
            "uses": 0
        }
        db.chat_invite_links.insert_one(link)
    return {"error": False, "code": link["_id"]}, 200


@invite_links_bp.delete("/chat/<chat_id>")
async def revoke_link(chat_id):
    chat, error = get_manageable_chat(chat_id)
    if error:
        return error
    db.chat_invite_links.delete_many({"chat_id": chat_id})
    return {"error": False}, 200


@invite_links_bp.get("/<code>")
async def preview_link(code):
    link, chat = get_link_and_chat(code)
    return {
        "error": False,
        "code": code,
        "chat_id": chat["_id"],
        "nickname": chat["nickname"],
        "icon": chat.get("icon", ""),
        "icon_color": chat.get("icon_color", ""),
        "member_count": len(chat["members"]),
        "is_member": bool(request.user and request.user in chat["members"]),
        "invited_by": link["created_by"]
    }, 200


@invite_links_bp.post("/<code>/join")
async def join_link(code):
    if not request.user:
        abort(401)
    if security.ratelimited(f"update_chat:{request.user}"):
        abort(429)
    security.ratelimit(f"update_chat:{request.user}", 10, 5)

    if security.is_restricted(request.user, security.Restrictions.NEW_CHATS):
        return {"error": True, "type": "accountBanned"}, 403

    link, chat = get_link_and_chat(code)

    if request.user in chat["members"]:
        chat["error"] = False
        return chat, 200

    if len(chat["members"]) >= 256:
        return {"error": True, "type": "chatFull"}, 403

    ban = security.chat_ban(chat, request.user)
    if ban:
        return {"error": True, "type": "chatBanned", "expires": ban["expires"]}, 403

    chat = chat_permissions.ensure_roles(chat)
    chat["members"].append(request.user)
    chat["member_roles"][request.user] = ["default"]
    db.chats.update_one({"_id": chat["_id"]}, {
        "$addToSet": {"members": request.user},
        "$set": {f"member_roles.{request.user}": ["default"]}
    })
    db.chat_invite_links.update_one({"_id": code}, {"$inc": {"uses": 1}})

    app.cl.send_event("create_chat", chat, usernames=[request.user])
    app.cl.send_event("update_chat", {
        "_id": chat["_id"],
        "members": chat["members"],
        "member_roles": chat["member_roles"]
    }, usernames=chat["members"])
    app.supporter.create_post(chat["_id"], "Server", f"@{request.user} joined the group chat via an invite link from @{link['created_by']}.", chat_members=chat["members"])

    chat["error"] = False
    return chat, 200
