from quart import Blueprint, current_app as app, request, abort
import time

import security
import chat_permissions
from database import db


invites_bp = Blueprint("invites_bp", __name__, url_prefix="/invites")


def serialize_invite(invite: dict, chat: dict) -> dict:
    return {
        "_id": invite["_id"],
        "chat_id": invite["chat_id"],
        "chat_nickname": chat["nickname"] if chat else None,
        "chat_owner": chat["owner"] if chat else None,
        "from": invite["from"],
        "to": invite["to"],
        "created": invite["created"],
        "expires": invite["expires"]
    }


def get_valid_invite(invite_id: str, username: str):
    invite = db.chat_invites.find_one({"_id": invite_id, "to": username})
    if not invite:
        abort(404)
    if invite["expires"] is not None and invite["expires"] < time.time():
        db.chat_invites.delete_one({"_id": invite_id})
        abort(404)
    return invite


@invites_bp.get("/")
async def get_invites():
    # Check authorization
    if not request.user:
        abort(401)

    # Get and return pending invites
    invites = []
    for invite in db.chat_invites.find({"to": request.user}):
        if invite["expires"] is not None and invite["expires"] < time.time():
            db.chat_invites.delete_one({"_id": invite["_id"]})
            continue
        chat = db.chats.find_one({"_id": invite["chat_id"], "deleted": False})
        invites.append(serialize_invite(invite, chat))

    return {
        "error": False,
        "autoget": invites,
        "page#": 1,
        "pages": 1
    }, 200


@invites_bp.post("/<invite_id>/accept")
async def accept_invite(invite_id):
    # Check authorization
    if not request.user:
        abort(401)

    # Get invite
    invite = get_valid_invite(invite_id, request.user)

    # Get chat
    chat = db.chats.find_one({"_id": invite["chat_id"], "deleted": False})
    if not chat:
        db.chat_invites.delete_one({"_id": invite_id})
        abort(404)

    # Make sure the chat isn't full
    if len(chat["members"]) >= 256:
        db.chat_invites.delete_one({"_id": invite_id})
        return {"error": True, "type": "chatFull"}, 403

    # Make sure the user isn't banned from the chat
    ban = security.chat_ban(chat, request.user)
    if ban:
        db.chat_invites.delete_one({"_id": invite_id})
        return {"error": True, "type": "chatBanned", "expires": ban["expires"]}, 403

    # Consume the invite
    db.chat_invites.delete_one({"_id": invite_id})

    # Add member, if not already in the chat
    if request.user in chat["members"]:
        chat["error"] = False
        return chat, 200
    chat = chat_permissions.ensure_roles(chat)
    chat["members"].append(request.user)
    chat["member_roles"][request.user] = ["default"]
    db.chats.update_one({"_id": invite["chat_id"]}, {
        "$addToSet": {"members": request.user},
        "$set": {f"member_roles.{request.user}": ["default"]}
    })

    # Send create chat event
    app.cl.send_event("create_chat", chat, usernames=[request.user])

    # Send update chat event
    app.cl.send_event("update_chat", {
        "_id": invite["chat_id"],
        "members": chat["members"],
        "member_roles": chat["member_roles"]
    }, usernames=chat["members"])

    # Send in-chat notification
    app.supporter.create_post(invite["chat_id"], "Server", f"@{request.user} joined the group chat via an invite from @{invite['from']}.", chat_members=chat["members"])

    # Notify members so pending invite lists update
    app.cl.send_event("chat_invites_update", {
        "chat_id": invite["chat_id"]
    }, usernames=chat["members"])

    # Return chat
    chat["error"] = False
    return chat, 200


@invites_bp.delete("/<invite_id>")
async def delete_invite(invite_id):
    # Check authorization
    if not request.user:
        abort(401)

    # Get invite (invitee declining, or chat owner revoking)
    invite = db.chat_invites.find_one({"_id": invite_id})
    if not invite:
        abort(404)
    if invite["to"] != request.user and invite["from"] != request.user:
        chat = db.chats.find_one({"_id": invite["chat_id"], "deleted": False})
        if not chat or chat["type"] != 0 or \
                not chat_permissions.has(chat_permissions.ensure_roles(chat), request.user, "remove_members"):
            abort(403)

    # Delete invite
    db.chat_invites.delete_one({"_id": invite_id})

    # Notify members so pending invite lists update
    chat = db.chats.find_one({"_id": invite["chat_id"], "deleted": False}, projection={"members": 1})
    if chat:
        app.cl.send_event("chat_invites_update", {
            "chat_id": invite["chat_id"]
        }, usernames=chat["members"])

    return {"error": False}, 200
