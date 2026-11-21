from quart import Blueprint, current_app as app, request, abort
from quart_schema import validate_querystring, validate_request
from pydantic import BaseModel, Field
from typing import Optional, Literal
import pymongo, uuid, time, re, os

import security
import chat_permissions
from database import db, get_total_pages
from uploads import claim_file, unclaim_file
from utils import log

chats_bp = Blueprint("chats_bp", __name__, url_prefix="/chats")


class GetPostsQueryArgs(BaseModel):
    page: Optional[int] = Field(default=1, ge=1)

class ChatBody(BaseModel):
    nickname: str = Field(default=None, min_length=1, max_length=32)
    icon: str = Field(default=None, max_length=24)
    icon_color: str = Field(default=None, min_length=6, max_length=6)  # hex code without the #
    announce_deletes: bool = Field(default=None)
    announce_deleter: bool = Field(default=None)
    invites_enabled: bool = Field(default=None)

    class Config:
        validate_assignment = True
        str_strip_whitespace = True

class RoleBody(BaseModel):
    name: str = Field(min_length=1, max_length=32)
    tier: int = Field(ge=0, le=3)
    permissions: dict[str, bool] = Field(default_factory=dict)
    colors: Optional[list[str]] = Field(default=None)

class RolePatchBody(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=32)
    tier: Optional[int] = Field(default=None)
    permissions: Optional[dict[str, Optional[bool]]] = Field(default=None)
    colors: Optional[list[str]] = Field(default=None)


ROLE_COLOR_RE = re.compile(r"^[0-9a-fA-F]{6}$")

def clean_role_colors(colors):
    """0 colours = none, 1 = solid, 2 = gradient. Returns None if invalid."""
    if colors is None:
        return []
    if len(colors) > 2 or not all(ROLE_COLOR_RE.match(c) for c in colors):
        return None
    return [c.lower() for c in colors]

class EmoteBody(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=32, pattern=r"^[A-Za-z0-9_\-]+$")

class InviteBody(BaseModel):
    username: str = Field(min_length=1, max_length=20)
    expires_in: Optional[int] = Field(default=None, ge=60, le=2592000)

class BanBody(BaseModel):
    expires_in: Optional[int] = Field(default=None, ge=60, le=2592000)


def missing_permission():
    return {"error": True, "type": "missingPermission"}, 403


def insufficient_rank():
    return {"error": True, "type": "insufficientRank"}, 403


@chats_bp.get("/")
async def get_chats():
    # Check authorization
    if not request.user:
        abort(401)

    # Get chats
    chats = app.supporter.get_chats(request.user)

    # Add emotes
    [chat.update({
        "emojis": list(db.chat_emojis.find({
            "chat_id": chat["_id"]
        }, projection={"chat_id": 0, "created_at": 0, "created_by": 0})),
        "stickers": list(db.chat_stickers.find({
            "chat_id": chat["_id"]
        }, projection={"chat_id": 0, "created_at": 0, "created_by": 0}))
    }) for chat in chats]

    # Get and return chats
    return {
        "error": False,
        "autoget": chats,
        "page#": 1,
        "pages": 1
    }, 200


@chats_bp.post("/")
@validate_request(ChatBody)
async def create_chat(data: ChatBody):
    # Check authorization
    if not request.user:
        abort(401)

    # Check ratelimit
    if security.ratelimited(f"create_chat:{request.user}"):
        abort(429)

    # Ratelimit
    security.ratelimit(f"create_chat:{request.user}", 5, 30)

    # Check restrictions
    if security.is_restricted(request.user, security.Restrictions.NEW_CHATS):
        return {"error": True, "type": "accountBanned"}, 403
    
    # Make sure the requester isn't in too many chats
    if db.chats.count_documents({"type": 0, "members": request.user}, limit=150) >= 150:
        return {"error": True, "type": "tooManyChats"}, 403

    # Claim icon
    if data.icon:
        try:
            claim_file(data.icon, "icons", request.user)
        except Exception as e:
            log(f"Unable to claim icon: {e}")
            return {"error": True, "type": "unableToClaimIcon"}, 500

    # Create chat
    if data.icon is None:
        data.icon = ""
    if data.icon_color is None:
        data.icon_color = "000000"
    chat = {
        "_id": str(uuid.uuid4()),
        "type": 0,
        "nickname": data.nickname,
        "icon": data.icon,
        "icon_color": data.icon_color,
        "owner": request.user,
        "members": [request.user],
        "created": int(time.time()),
        "last_active": int(time.time()),
        "deleted": False,
        "announce_deletes": False,
        "announce_deleter": False,
        "roles": chat_permissions.default_roles(),
        "member_roles": {request.user: ["default", "administrator"]},
        "invites_enabled": True,
        "bans": []
    }
    db.chats.insert_one(chat)

    # Add emotes
    chat.update({
        "emojis": list(db.chat_emojis.find({
            "chat_id": chat["_id"]
        }, projection={"chat_id": 0, "created_at": 0, "created_by": 0})),
        "stickers": list(db.chat_stickers.find({
            "chat_id": chat["_id"]
        }, projection={"chat_id": 0, "created_at": 0, "created_by": 0}))
    })


    # Tell the requester the chat was created
    app.cl.send_event("create_chat", chat, usernames=[request.user])

    # Return chat
    chat["error"] = False
    return chat, 200


@chats_bp.get("/<chat_id>")
async def get_chat(chat_id):
    # Check authorization
    if not request.user:
        abort(401)

    # Get chat
    chat = db.chats.find_one({"_id": chat_id, "members": request.user, "deleted": False})
    if not chat:
        abort(404)

    # Migrate in roles for legacy group chats
    chat = chat_permissions.ensure_roles(chat)

    # Normalize any expired DM request and only expose the public shape
    chat = security.normalize_dm_request(chat)
    if "dm_request" in chat:
        chat["dm_request"] = security.public_dm_request(chat["dm_request"])

    # Return chat
    chat.update({
        "error": False,
        "emojis": list(db.chat_emojis.find({
            "chat_id": chat["_id"]
        }, projection={"chat_id": 0, "created_at": 0, "created_by": 0})),
        "stickers": list(db.chat_stickers.find({
            "chat_id": chat["_id"]
        }, projection={"chat_id": 0, "created_at": 0, "created_by": 0}))
    })
    return chat, 200


@chats_bp.patch("/<chat_id>")
@validate_request(ChatBody)
async def update_chat(chat_id, data: ChatBody):
    # Check authorization
    if not request.user:
        abort(401)

    # Check ratelimit
    if security.ratelimited(f"update_chat:{request.user}"):
        abort(429)

    # Ratelimit
    security.ratelimit(f"update_chat:{request.user}", 5, 5)

    # Check restrictions
    if security.is_restricted(request.user, security.Restrictions.EDITING_CHAT_DETAILS):
        return {"error": True, "type": "accountBanned"}, 403

    # Get chat
    chat = db.chats.find_one({"_id": chat_id, "members": request.user, "deleted": False})
    if not chat:
        abort(404)

    # Check permissions for each field being changed
    if chat["type"] == 0:
        chat = chat_permissions.ensure_roles(chat)
        required = set()
        if data.nickname is not None and chat["nickname"] != data.nickname:
            required.add("change_name")
        if data.icon is not None and chat["icon"] != data.icon:
            required.add("change_icon" if data.icon else "delete_icon")
        if data.icon_color is not None and chat["icon_color"] != data.icon_color:
            required.add("change_color")
        if (data.announce_deletes is not None and data.announce_deletes != chat.get("announce_deletes", False)) or \
           (data.announce_deleter is not None and data.announce_deleter != chat.get("announce_deleter", False)):
            required.add("change_settings")
        if data.invites_enabled is not None and data.invites_enabled != chat.get("invites_enabled", True):
            required.add("toggle_invites")
        for perm in required:
            if not chat_permissions.has(chat, request.user, perm):
                return missing_permission()
    elif chat["owner"] != request.user:
        abort(403)

    # Get updated values
    updated_vals = {"_id": chat_id}
    if data.nickname is not None and chat["nickname"] != data.nickname:
        updated_vals["nickname"] = data.nickname
        app.supporter.create_post(chat_id, "Server", f"@{request.user} changed the nickname of the group chat to '{data.nickname}'.", chat_members=chat["members"])
    if data.icon is not None and chat["icon"] != data.icon:
        # Claim icon (and delete old one)
        if data.icon != "":
            try:
                claim_file(data.icon, "icons", request.user)
            except Exception as e:
                log(f"Unable to claim icon: {e}")
                return {"error": True, "type": "unableToClaimIcon"}, 500
            else:
                updated_vals["icon"] = data.icon
        else:
            updated_vals["icon"] = ""
        if chat["icon"]:
            try:
                unclaim_file(chat["icon"])
            except Exception as e:
                log(f"Unable to delete icon: {e}")
        if data.icon:
            app.supporter.create_post(chat_id, "Server", f"@{request.user} changed the icon of the group chat.", chat_members=chat["members"])
        else:
            app.supporter.create_post(chat_id, "Server", f"@{request.user} removed the icon of the group chat.", chat_members=chat["members"])
    if data.icon_color is not None and chat["icon_color"] != data.icon_color:
        updated_vals["icon_color"] = data.icon_color
        if data.icon is None or chat["icon"] == data.icon:
            app.supporter.create_post(chat_id, "Server", f"@{request.user} changed the icon of the group chat.", chat_members=chat["members"])
    if chat["type"] == 0:
        if data.announce_deletes is not None:
            updated_vals["announce_deletes"] = data.announce_deletes
        if data.announce_deleter is not None:
            updated_vals["announce_deleter"] = data.announce_deleter
        if data.invites_enabled is not None:
            updated_vals["invites_enabled"] = data.invites_enabled
    
    # Update chat
    db.chats.update_one({"_id": chat_id}, {"$set": updated_vals})

    # Send update chat event
    app.cl.send_event("update_chat", updated_vals, usernames=chat["members"])

    # Return chat
    chat.update(updated_vals)
    chat.update({
        "error": False,
        "emojis": list(db.chat_emojis.find({
            "chat_id": chat["_id"]
        }, projection={"chat_id": 0, "created_at": 0, "created_by": 0})),
        "stickers": list(db.chat_stickers.find({
            "chat_id": chat["_id"]
        }, projection={"chat_id": 0, "created_at": 0, "created_by": 0}))
    })
    return chat, 200


@chats_bp.delete("/<chat_id>")
async def leave_chat(chat_id):
    # Check authorization
    if not request.user:
        abort(401)

    # Check ratelimit
    if security.ratelimited(f"update_chat:{request.user}"):
        abort(429)

    # Ratelimit
    security.ratelimit(f"update_chat:{request.user}", 5, 5)

    # Get chat
    chat = db.chats.find_one({"_id": chat_id, "members": request.user, "deleted": False})
    if not chat:
        abort(404)

    if chat["type"] == 0:
        chat = chat_permissions.ensure_roles(chat)

        # Remove member
        chat["members"].remove(request.user)

        # Update chat if it's not empty, otherwise delete the chat
        if len(chat["members"]) > 0:
            # Remove the leaver's roles
            chat["member_roles"].pop(request.user, None)

            # Transfer ownership, if owner
            if chat["owner"] == request.user:
                chat["owner"] = chat["members"][0]
                new_owner_roles = chat["member_roles"].setdefault(chat["owner"], ["default"])
                if "administrator" not in new_owner_roles:
                    new_owner_roles.append("administrator")

            # Update chat
            db.chats.update_one({"_id": chat_id}, {
                "$set": {"owner": chat["owner"], "member_roles": chat["member_roles"]},
                "$pull": {"members": request.user}
            })

            # Send update chat event
            app.cl.send_event("update_chat", {
                "_id": chat_id,
                "owner": chat["owner"],
                "members": chat["members"],
                "member_roles": chat["member_roles"]
            }, usernames=chat["members"])

            # Send in-chat notification
            app.supporter.create_post(chat_id, "Server", f"@{request.user} has left the group chat.", chat_members=chat["members"])
        else:
            if chat["icon"]:
                try:
                    unclaim_file(chat["icon"])
                except Exception as e:
                    log(f"Unable to delete icon: {e}")
            db.posts.delete_many({"post_origin": chat_id, "isDeleted": False})
            db.chats.delete_one({"_id": chat_id})
    elif chat["type"] == 1:
        # Remove chat from requester's active DMs list
        db.user_settings.update_one({"_id": request.user}, {
            "$pull": {"active_dms": chat_id}
        })
    else:
        abort(500)

    # Send delete event to client
    app.cl.send_event("delete_chat", {"chat_id": chat_id}, usernames=[request.user])

    return {"error": False}, 200


@chats_bp.post("/<chat_id>/typing")
async def emit_typing(chat_id):
    # Check authorization
    if not request.user:
        abort(401)

    # Check ratelimit
    if security.ratelimited(f"typing:{request.user}"):
        abort(429)

    # Ratelimit
    security.ratelimit(f"typing:{request.user}", 6, 5)

    # Check restrictions
    if security.is_restricted(request.user, security.Restrictions.CHAT_POSTS):
        return {"error": True, "type": "accountBanned"}, 403

    # Get chat
    if chat_id != "livechat":
        chat = db.chats.find_one({
            "_id": chat_id,
            "members": request.user,
            "deleted": False
        }, projection={"members": 1})
        if not chat:
            abort(404)

    # Send typing event
    app.cl.send_event("typing", {
        "chat_id": chat_id, "username": request.user
    }, usernames=(None if chat_id == "livechat" else chat["members"]))

    return {"error": False}, 200


def send_chat_invite(chat: dict, username: str, expires_in: Optional[int] = None):
    # Clean up expired invites and make sure there's no pending invite already
    db.chat_invites.delete_many({"chat_id": chat["_id"], "expires": {"$lt": int(time.time())}})
    if db.chat_invites.count_documents({"chat_id": chat["_id"], "to": username}, limit=1):
        return {"error": True, "type": "inviteAlreadyExists"}, 409

    # Create invite
    invite = {
        "_id": str(uuid.uuid4()),
        "chat_id": chat["_id"],
        "from": request.user,
        "to": username,
        "created": int(time.time()),
        "expires": (int(time.time()) + expires_in) if expires_in else None
    }
    db.chat_invites.insert_one(invite)

    # Send invite event to the invitee
    app.cl.send_event("chat_invite", {
        **invite,
        "chat_nickname": chat["nickname"]
    }, usernames=[username])

    # Send inbox message to user
    app.supporter.create_post("inbox", username, f"@{request.user} invited you to join the group chat '{chat['nickname']}'! You can accept or decline the invite from your chats page.")

    # Notify chat members so pending invite lists update
    app.cl.send_event("chat_invites_update", {
        "chat_id": chat["_id"]
    }, usernames=chat["members"])

    invite["error"] = False
    return invite, 200


@chats_bp.put("/<chat_id>/members/<username>")
async def add_chat_member(chat_id, username):
    # Check authorization
    if not request.user:
        abort(401)

    # Check ratelimit
    if security.ratelimited(f"update_chat:{request.user}"):
        abort(429)

    # Ratelimit
    security.ratelimit(f"update_chat:{request.user}", 5, 5)

    # Check restrictions
    if security.is_restricted(request.user, security.Restrictions.NEW_CHATS):
        return {"error": True, "type": "accountBanned"}, 403

    # Get chat
    chat = db.chats.find_one({"_id": chat_id, "members": request.user, "deleted": False})
    if not chat:
        abort(404)

    # Make sure the requester isn't banned from the chat
    ban = security.chat_ban(chat, request.user)
    if ban:
        return {"error": True, "type": "chatBanned", "expires": ban["expires"]}, 403

    # Group chats require permission to add/invite members
    if chat["type"] == 0:
        chat = chat_permissions.ensure_roles(chat)
        if not chat_permissions.has(chat, request.user, "invite_members"):
            return missing_permission()
        if not chat.get("invites_enabled", True) and request.user != chat["owner"] and \
           not chat_permissions.has(chat, request.user, "toggle_invites"):
            return {"error": True, "type": "invitesDisabled"}, 403

    # Make sure the chat isn't full
    if chat["type"] == 1 or len(chat["members"]) >= 256:
        return {"error": True, "type": "chatFull"}, 403

    # Make sure the user isn't already in the chat
    if username in chat["members"]:
        return {"error": True, "type": "chatMemberAlreadyExists"}, 409

    # Make sure requested user exists and isn't deleted
    user = db.usersv0.find_one({"_id": username}, projection={"permissions": 1})
    if (not user) or (user["permissions"] is None):
        abort(404)

    # Make sure requested user isn't banned from the chat
    if security.chat_ban(chat, username):
        return {"error": True, "type": "chatBanned"}, 403

    # Make sure requested user isn't blocked or is blocking client
    if db.relationships.count_documents({"$or": [
        {
            "_id": {"from": request.user, "to": username},
            "state": 2
        },
        {
            "_id": {"from": username, "to": request.user},
            "state": 2
        }
    ]}, limit=1) > 0:
        abort(403)

    # Only mutual friends can be added directly, everyone else gets an invite
    if not security.are_friends(request.user, username):
        return send_chat_invite(chat, username)

    # Update chat
    chat["members"].append(username)
    if chat["type"] == 0:
        chat["member_roles"][username] = ["default"]
    db.chats.update_one({"_id": chat_id}, {
        "$addToSet": {"members": username},
        "$set": ({f"member_roles.{username}": ["default"]} if chat["type"] == 0 else {})
    })

    # Send create chat event
    app.cl.send_event("create_chat", chat, usernames=[username])

    # Send update chat event
    app.cl.send_event("update_chat", {
        "_id": chat_id,
        "members": chat["members"],
        "member_roles": chat.get("member_roles")
    }, usernames=chat["members"])

    # Send inbox message to user
    app.supporter.create_post("inbox", username, f"You have been added to the group chat '{chat['nickname']}' by @{request.user}!")

    # Send in-chat notification
    app.supporter.create_post(chat_id, "Server", f"@{request.user} added @{username} to the group chat.", chat_members=chat["members"])

    # Return chat
    chat.update({
        "error": False,
        "emojis": list(db.chat_emojis.find({
            "chat_id": chat["_id"]
        }, projection={"chat_id": 0, "created_at": 0, "created_by": 0})),
        "stickers": list(db.chat_stickers.find({
            "chat_id": chat["_id"]
        }, projection={"chat_id": 0, "created_at": 0, "created_by": 0}))
    })
    return chat, 200


@chats_bp.delete("/<chat_id>/members/<username>")
async def remove_chat_member(chat_id, username):
    # Check authorization
    if not request.user:
        abort(401)

    # Check ratelimit
    if security.ratelimited(f"update_chat:{request.user}"):
        abort(429)

    # Ratelimit
    security.ratelimit(f"update_chat:{request.user}", 5, 5)

    # Get chat
    chat = db.chats.find_one({
        "_id": chat_id,
        "members": {"$all": [request.user, username]},
        "deleted": False
    })
    if not chat:
        abort(404)

    # Check remove_members permission and hierarchy
    chat = chat_permissions.ensure_roles(chat)
    if not chat_permissions.has(chat, request.user, "remove_members"):
        return missing_permission()
    if not chat_permissions.outranks(chat, request.user, username):
        return insufficient_rank()

    # Update chat
    chat["members"].remove(username)
    chat["member_roles"].pop(username, None)
    db.chats.update_one({"_id": chat_id}, {
        "$pull": {"members": username},
        "$unset": {f"member_roles.{username}": ""}
    })

    # Send delete chat event to user
    app.cl.send_event("delete_chat", {"chat_id": chat_id}, usernames=[username])

    # Send update chat event
    app.cl.send_event("update_chat", {
        "_id": chat_id,
        "members": chat["members"],
        "member_roles": chat["member_roles"]
    }, usernames=chat["members"])

    # Send inbox message to user
    app.supporter.create_post("inbox", username, f"You have been removed from the group chat '{chat['nickname']}' by @{request.user}!")

    # Send in-chat notification
    app.supporter.create_post(chat_id, "Server", f"@{request.user} removed @{username} from the group chat.", chat_members=chat["members"])

    # Return chat
    chat.update({
        "error": False,
        "emojis": list(db.chat_emojis.find({
            "chat_id": chat["_id"]
        }, projection={"chat_id": 0, "created_at": 0, "created_by": 0})),
        "stickers": list(db.chat_stickers.find({
            "chat_id": chat["_id"]
        }, projection={"chat_id": 0, "created_at": 0, "created_by": 0}))
    })
    return chat, 200


@chats_bp.post("/<chat_id>/members/<username>/transfer")
async def transfer_chat_ownership(chat_id, username):
    # Check authorization
    if not request.user:
        abort(401)

    # Check ratelimit
    if security.ratelimited(f"update_chat:{request.user}"):
        abort(429)

    # Ratelimit
    security.ratelimit(f"update_chat:{request.user}", 5, 5)

    # Get chat
    chat = db.chats.find_one({
        "_id": chat_id,
        "members": {"$all": [request.user, username]},
        "deleted": False
    })
    if not chat:
        abort(404)

    # Make sure requester is owner
    if chat["owner"] != request.user:
        abort(403)

    # Make sure requested user isn't already owner
    if chat["owner"] == username:
        chat["error"] = False
        return chat, 200

    # Update chat
    if chat["type"] == 0:
        chat = chat_permissions.ensure_roles(chat)
        new_owner_roles = chat["member_roles"].setdefault(username, ["default"])
        if "administrator" not in new_owner_roles:
            new_owner_roles.append("administrator")
    chat["owner"] = username
    db.chats.update_one({"_id": chat_id}, {
        "$set": ({"owner": username, "member_roles": chat["member_roles"]} if chat["type"] == 0 else {"owner": username})
    })

    # Send update chat event
    app.cl.send_event("update_chat", {
        "_id": chat_id,
        "owner": chat["owner"],
        "member_roles": chat.get("member_roles")
    }, usernames=chat["members"])

    # Send in-chat notification
    app.supporter.create_post(chat_id, "Server", f"@{request.user} transferred ownership of the group chat to @{username}.", chat_members=chat["members"])

    # Return chat
    chat.update({
        "error": False,
        "emojis": list(db.chat_emojis.find({
            "chat_id": chat["_id"]
        }, projection={"chat_id": 0, "created_at": 0, "created_by": 0})),
        "stickers": list(db.chat_stickers.find({
            "chat_id": chat["_id"]
        }, projection={"chat_id": 0, "created_at": 0, "created_by": 0}))
    })
    return chat, 200


@chats_bp.post("/<chat_id>/invites")
@validate_request(InviteBody)
async def create_chat_invite(chat_id, data: InviteBody):
    # Check authorization
    if not request.user:
        abort(401)

    # Check ratelimit
    if security.ratelimited(f"update_chat:{request.user}"):
        abort(429)

    # Ratelimit
    security.ratelimit(f"update_chat:{request.user}", 10, 5)

    # Check restrictions
    if security.is_restricted(request.user, security.Restrictions.NEW_CHATS):
        return {"error": True, "type": "accountBanned"}, 403

    # Get chat
    chat = db.chats.find_one({"_id": chat_id, "members": request.user, "deleted": False})
    if not chat:
        abort(404)

    # Make sure the requester isn't banned from the chat
    ban = security.chat_ban(chat, request.user)
    if ban:
        return {"error": True, "type": "chatBanned", "expires": ban["expires"]}, 403

    # Group chats only
    if chat["type"] != 0:
        abort(400)

    # Check invite permission (and whether invites are enabled)
    chat = chat_permissions.ensure_roles(chat)
    if not chat_permissions.has(chat, request.user, "invite_members"):
        return missing_permission()
    if not chat.get("invites_enabled", True) and request.user != chat["owner"] and \
       not chat_permissions.has(chat, request.user, "toggle_invites"):
        return {"error": True, "type": "invitesDisabled"}, 403

    username = data.username

    # Make sure the chat isn't full
    if len(chat["members"]) >= 256:
        return {"error": True, "type": "chatFull"}, 403

    # Make sure the user isn't already in the chat
    if username in chat["members"]:
        return {"error": True, "type": "chatMemberAlreadyExists"}, 409

    # Make sure requested user exists and isn't deleted
    user = db.usersv0.find_one({"_id": username}, projection={"permissions": 1})
    if (not user) or (user["permissions"] is None):
        abort(404)

    # Make sure requested user isn't banned from the chat
    if security.chat_ban(chat, username):
        return {"error": True, "type": "chatBanned"}, 403

    # Make sure requested user isn't blocked or is blocking client
    if db.relationships.count_documents({"$or": [
        {
            "_id": {"from": request.user, "to": username},
            "state": 2
        },
        {
            "_id": {"from": username, "to": request.user},
            "state": 2
        }
    ]}, limit=1) > 0:
        abort(403)

    return send_chat_invite(chat, username, data.expires_in)


@chats_bp.get("/<chat_id>/invites")
async def get_chat_invites(chat_id):
    # Check authorization
    if not request.user:
        abort(401)

    # Get chat
    chat = db.chats.find_one({"_id": chat_id, "members": request.user, "deleted": False})
    if not chat:
        abort(404)

    # Clean up expired invites and return pending ones
    db.chat_invites.delete_many({"chat_id": chat_id, "expires": {"$lt": int(time.time())}})
    return {
        "error": False,
        "autoget": list(db.chat_invites.find({"chat_id": chat_id})),
        "page#": 1,
        "pages": 1
    }, 200


@chats_bp.post("/<chat_id>/dm_request/accept")
async def accept_dm_request(chat_id):
    # Check authorization
    if not request.user:
        abort(401)

    # Get chat with a pending DM request
    chat = db.chats.find_one({"_id": chat_id, "type": 1, "members": request.user, "deleted": False})
    if not chat:
        abort(404)
    chat = security.normalize_dm_request(chat)
    if not chat.get("dm_request") or not chat["dm_request"].get("from"):
        abort(404)

    # Only the recipient can accept
    if chat["dm_request"]["to"] != request.user:
        abort(403)

    # Accept the request
    db.chats.update_one({"_id": chat_id}, {"$unset": {"dm_request": ""}})
    del chat["dm_request"]

    # Notify members and post a notice in the DM
    app.cl.send_event("update_chat", {
        "_id": chat_id,
        "dm_request": None
    }, usernames=chat["members"])
    app.supporter.create_post(chat_id, "Server", f"@{request.user} accepted the DM request.", chat_members=chat["members"])

    chat["error"] = False
    return chat, 200


@chats_bp.post("/<chat_id>/dm_request/ignore")
async def ignore_dm_request(chat_id):
    # Check authorization
    if not request.user:
        abort(401)

    # Get chat with a pending DM request
    chat = db.chats.find_one({"_id": chat_id, "type": 1, "members": request.user, "deleted": False})
    if not chat:
        abort(404)
    chat = security.normalize_dm_request(chat)
    if not chat.get("dm_request") or not chat["dm_request"].get("from"):
        abort(404)

    # Only the recipient can ignore
    if chat["dm_request"]["to"] != request.user:
        abort(403)

    # Ignore: flag the request and remove the chat from the recipient's
    # active DMs. Nothing is sent to the requester.
    db.chats.update_one({"_id": chat_id}, {"$set": {"dm_request.ignored": True}})
    db.user_settings.update_one({"_id": request.user}, {
        "$pull": {"active_dms": chat_id}
    })
    app.cl.send_event("delete_chat", {"chat_id": chat_id}, usernames=[request.user])

    return {"error": False}, 200


@chats_bp.put("/<chat_id>/bans/<username>")
@validate_request(BanBody)
async def ban_chat_member(chat_id, username, data: BanBody):
    # Check authorization
    if not request.user:
        abort(401)

    # Check ratelimit
    if security.ratelimited(f"update_chat:{request.user}"):
        abort(429)

    # Ratelimit
    security.ratelimit(f"update_chat:{request.user}", 5, 5)

    # Get chat
    chat = db.chats.find_one({"_id": chat_id, "members": request.user, "deleted": False})
    if not chat:
        abort(404)

    # Check ban_members permission and hierarchy
    chat = chat_permissions.ensure_roles(chat)
    if not chat_permissions.has(chat, request.user, "ban_members"):
        return missing_permission()
    if username == chat["owner"] or not chat_permissions.outranks(chat, request.user, username):
        return insufficient_rank()

    # Make sure the target is a member (or already banned) and isn't the owner
    is_member = username in chat["members"]
    if not (is_member or security.chat_ban(chat, username)):
        abort(400)

    # Replace any existing ban for the user
    ban = {
        "username": username,
        "banned_by": request.user,
        "banned_at": int(time.time()),
        "expires": (int(time.time()) + data.expires_in) if data.expires_in else None
    }
    db.chats.update_one({"_id": chat_id}, {"$pull": {"bans": {"username": username}}})
    db.chats.update_one({"_id": chat_id}, {
        "$push": {"bans": ban},
        "$pull": {"members": username},
        "$unset": {f"member_roles.{username}": ""}
    })
    chat["bans"] = [b for b in chat.get("bans", []) if b["username"] != username] + [ban]
    chat["member_roles"].pop(username, None)
    if is_member:
        chat["members"].remove(username)

    # Revoke any pending invites for and from the user
    db.chat_invites.delete_many({"chat_id": chat_id, "$or": [{"to": username}, {"from": username}]})

    # Send delete chat event to user
    if is_member:
        app.cl.send_event("delete_chat", {"chat_id": chat_id}, usernames=[username])

    # Send update chat event
    app.cl.send_event("update_chat", {
        "_id": chat_id,
        "members": chat["members"],
        "member_roles": chat["member_roles"],
        "bans": chat["bans"]
    }, usernames=chat["members"])

    # Send inbox message to user
    app.supporter.create_post("inbox", username, f"You have been banned from the group chat '{chat['nickname']}' by @{request.user}." + (f" The ban expires <t:{ban['expires']}:R>." if ban["expires"] else ""))

    # Send in-chat notification
    app.supporter.create_post(chat_id, "Server", f"@{request.user} banned @{username} from the group chat.", chat_members=chat["members"])

    return {"error": False, "ban": ban}, 200


@chats_bp.get("/<chat_id>/bans")
async def get_chat_bans(chat_id):
    # Check authorization
    if not request.user:
        abort(401)

    # Get chat
    chat = db.chats.find_one({"_id": chat_id, "members": request.user, "deleted": False})
    if not chat:
        abort(404)

    # Only members who can ban may view bans
    chat = chat_permissions.ensure_roles(chat)
    if not chat_permissions.has(chat, request.user, "ban_members"):
        return missing_permission()

    return {
        "error": False,
        "autoget": [
            ban for ban in chat.get("bans", [])
            if ban["expires"] is None or ban["expires"] > time.time()
        ],
        "page#": 1,
        "pages": 1
    }, 200


@chats_bp.delete("/<chat_id>/bans/<username>")
async def unban_chat_member(chat_id, username):
    # Check authorization
    if not request.user:
        abort(401)

    # Check ratelimit
    if security.ratelimited(f"update_chat:{request.user}"):
        abort(429)

    # Ratelimit
    security.ratelimit(f"update_chat:{request.user}", 5, 5)

    # Get chat
    chat = db.chats.find_one({"_id": chat_id, "members": request.user, "deleted": False})
    if not chat:
        abort(404)

    # Check ban_members permission (and hierarchy if the target is still a member)
    chat = chat_permissions.ensure_roles(chat)
    if not chat_permissions.has(chat, request.user, "ban_members"):
        return missing_permission()
    if username in chat["members"] and not chat_permissions.outranks(chat, request.user, username):
        return insufficient_rank()

    # Remove the ban
    db.chats.update_one({"_id": chat_id}, {"$pull": {"bans": {"username": username}}})
    chat["bans"] = [b for b in chat.get("bans", []) if b["username"] != username]

    # Send update chat event
    app.cl.send_event("update_chat", {
        "_id": chat_id,
        "bans": chat["bans"]
    }, usernames=chat["members"])

    # Send inbox message to user
    app.supporter.create_post("inbox", username, f"You have been unbanned from the group chat '{chat['nickname']}'.")

    # Send in-chat notification
    app.supporter.create_post(chat_id, "Server", f"@{request.user} unbanned @{username} from the group chat.", chat_members=chat["members"])

    return {"error": False}, 200


@chats_bp.get("/<chat_id>/pins")
@validate_querystring(GetPostsQueryArgs)
def get_chat_pins(chat_id, query_args: GetPostsQueryArgs):
    # Check authorization
    if not request.user:
        abort(401)

    # Make sure chat exists and requester has access
    if not db.chats.count_documents({
        "_id": chat_id,
        "members": request.user,
        "deleted": False
    }, limit=1):
        abort(404)

    # Get and return pinned posts
    query = {"post_origin": chat_id, "pinned": True}
    return {
        "error": False,
        "autoget": app.supporter.parse_posts_v0(
            db.posts.find(
                query,
                sort=[("t.e", pymongo.DESCENDING), ("_id", pymongo.DESCENDING)],
                skip=(query_args.page-1)*25,
                limit=25
            ),
            include_replies=True,
            requester=request.user
        ),
        "page#": query_args.page,
        "pages": (get_total_pages("posts", query) if request.user else 1)
    }, 200


@chats_bp.get("/<chat_id>/<emote_type>")
async def get_chat_emotes(chat_id: str, emote_type: Literal["emojis", "stickers"]):
    # Make sure emote type is valid
    if emote_type not in ["emojis", "stickers"]:
        abort(404)

    # Check authorization
    if not request.user:
        abort(401)

    # Make sure chat exists and requester has access
    if not db.chats.count_documents({
        "_id": chat_id,
        "members": request.user,
        "deleted": False
    }, limit=1):
        abort(404)

    # Get and return emotes
    return {
        "error": False,
        "autoget": list(db[f"chat_{emote_type}"].find({
            "chat_id": chat_id
        }, sort=[("created_at", pymongo.DESCENDING)], projection={
            "chat_id": 0,
            "created_at": 0,
            "created_by": 0
        })),
        "page#": 1,
        "pages": 1
    }, 200


@chats_bp.get("/<chat_id>/<emote_type>/<emote_id>")
async def get_chat_emote(chat_id: str, emote_type: Literal["emojis", "stickers"], emote_id: str):
    # Make sure emote type is valid
    if emote_type not in ["emojis", "stickers"]:
        abort(404)

    # Check authorization
    if not request.user:
        abort(401)

    # Make sure chat exists and requester has access
    if not db.chats.count_documents({
        "_id": chat_id,
        "members": request.user,
        "deleted": False
    }, limit=1):
        abort(404)

    # Get emote
    emote = db[f"chat_{emote_type}"].find_one({
        "_id": emote_id,
        "chat_id": chat_id
    }, projection={"chat_id": 0, "created_at": 0, "created_by": 0})
    if not emote:
        abort(404)

    # Return emote
    emote["error"] = False
    return emote, 200


@chats_bp.put("/<chat_id>/<emote_type>/<emote_id>")
@validate_request(EmoteBody)
async def create_chat_emote(chat_id: str, emote_type: Literal["emojis", "stickers"], emote_id: str, data: EmoteBody):
    # Make sure emote type is valid
    if emote_type not in ["emojis", "stickers"]:
        abort(404)

    # Check authorization
    if not request.user:
        abort(401)

    # Check ratelimit
    if security.ratelimited(f"update_chat:{request.user}"):
        abort(429)

    # Ratelimit
    security.ratelimit(f"update_chat:{request.user}", 5, 5)

    # Get chat
    chat = db.chats.find_one({
        "_id": chat_id,
        "members": {"$all": [request.user]},
        "deleted": False
    })
    if not chat:
        abort(404)

    # Check add_emotes permission (DMs are unrestricted)
    if chat["type"] == 0:
        chat = chat_permissions.ensure_roles(chat)
        if not chat_permissions.has(chat, request.user, "add_emotes"):
            return missing_permission()
    elif chat["type"] != 1:
        abort(403)

    # Make sure there's not too many emotes in the chat (250 for emojis, 50 for stickers)
    if emote_type == "emojis":
        if db.chat_emojis.count_documents({"chat_id": chat_id}, limit=250) >= int(os.getenv("CHAT_EMOJIS_LIMIT", 250)):
            return {"error": True, "type": "tooManyEmojis"}, 403
    elif emote_type == "stickers":
        if db.chat_stickers.count_documents({"chat_id": chat_id}, limit=50) >= int(os.getenv("CHAT_STICKERS_LIMIT", 50)):
            return {"error": True, "type": "tooManyStickers"}, 403

    # Make sure a name is specified
    if not data.name:
        return {"error": True, "type": "nameRequired"}, 400

    # Claim file
    try:
        claim_file(emote_id, emote_type, request.user)
    except Exception as e:
        log(f"Unable to claim emote: {e}")
        return {"error": True, "type": "unableToClaimEmote"}, 500
    else:
        file = db.files.find_one({"_id": emote_id})


    # Create emote
    emote = {
        "_id": emote_id,
        "chat_id": chat_id,
        "name": data.name,
        "animated": (file["mime"] == "image/gif"),
        "created_at": int(time.time()),
        "created_by": request.user
    }
    db[f"chat_{emote_type}"].insert_one(emote)
    del emote["created_at"]
    del emote["created_by"]
    app.cl.send_event(f"create_{emote_type[:-1]}", emote, usernames=chat["members"])

    # Return new emote
    del emote["chat_id"]
    emote["error"] = False
    return emote, 200


@chats_bp.patch("/<chat_id>/<emote_type>/<emote_id>")
@validate_request(EmoteBody)
async def update_chat_emote(chat_id: str, emote_type: Literal["emojis", "stickers"], emote_id: str, data: EmoteBody):
    # Make sure emote type is valid
    if emote_type not in ["emojis", "stickers"]:
        abort(404)

    # Make sure a new name is being specified
    if not data.name:
        abort(400)

    # Check authorization
    if not request.user:
        abort(401)

    # Check ratelimit
    if security.ratelimited(f"update_chat:{request.user}"):
        abort(429)

    # Ratelimit
    security.ratelimit(f"update_chat:{request.user}", 5, 5)

    # Get chat
    chat = db.chats.find_one({
        "_id": chat_id,
        "members": {"$all": [request.user]},
        "deleted": False
    })
    if not chat:
        abort(404)

    # Check edit_emotes permission (DMs are unrestricted)
    if chat["type"] == 0:
        chat = chat_permissions.ensure_roles(chat)
        if not chat_permissions.has(chat, request.user, "edit_emotes"):
            return missing_permission()
    elif chat["type"] != 1:
        abort(403)

    # Get emote
    emote = db[f"chat_{emote_type}"].find_one({
        "_id": emote_id,
        "chat_id": chat_id
    }, projection={"chat_id": 0, "created_at": 0, "created_by": 0})
    if not emote:
        abort(404)

    # Update emote name
    emote["name"] = data.name
    db[f"chat_{emote_type}"].update_one({"_id": emote_id}, {"$set": {"name": data.name}})
    app.cl.send_event(f"update_{emote_type[:-1]}", {
        "_id": emote_id,
        "chat_id": chat_id,
        "name": data.name
    }, usernames=chat["members"])

    # Return updated emote
    emote["error"] = False
    return emote, 200


@chats_bp.delete("/<chat_id>/<emote_type>/<emote_id>")
async def delete_chat_emote(chat_id: str, emote_type: Literal["emojis", "stickers"], emote_id: str):
    # Make sure emote type is valid
    if emote_type not in ["emojis", "stickers"]:
        abort(404)

    # Check authorization
    if not request.user:
        abort(401)

    # Check ratelimit
    if security.ratelimited(f"update_chat:{request.user}"):
        abort(429)

    # Ratelimit
    security.ratelimit(f"update_chat:{request.user}", 5, 5)

    # Get chat
    chat = db.chats.find_one({
        "_id": chat_id,
        "members": {"$all": [request.user]},
        "deleted": False
    })
    if not chat:
        abort(404)

    # Check delete_emotes permission (DMs are unrestricted)
    if chat["type"] == 0:
        chat = chat_permissions.ensure_roles(chat)
        if not chat_permissions.has(chat, request.user, "delete_emotes"):
            return missing_permission()
    elif chat["type"] != 1:
        abort(403)

    # Delete emote
    result = db[f"chat_{emote_type}"].delete_one({"_id": emote_id, "chat_id": chat_id})
    if not result.deleted_count:
        abort(404)
    app.cl.send_event(f"delete_{emote_type[:-1]}", {
        "_id": emote_id,
        "chat_id": chat_id
    }, usernames=chat["members"])
    unclaim_file(emote_id)

    return {"error": False}, 200


def get_role(chat: dict, role_id: str):
    return next((r for r in chat.get("roles", []) if r["_id"] == role_id), None)


def send_roles_update(chat: dict):
    payload = {
        "_id": chat["_id"],
        "roles": chat["roles"],
        "member_roles": chat["member_roles"]
    }
    if "invites_enabled" in chat:
        payload["invites_enabled"] = chat["invites_enabled"]
    app.cl.send_event("update_chat", payload, usernames=chat["members"])


async def get_group_chat_for_roles(chat_id):
    # Auth + ratelimit shared by all role endpoints
    if not request.user:
        abort(401)
    if security.ratelimited(f"update_chat:{request.user}"):
        abort(429)
    security.ratelimit(f"update_chat:{request.user}", 5, 5)

    chat = db.chats.find_one({"_id": chat_id, "members": request.user, "deleted": False})
    if not chat:
        abort(404)
    if chat["type"] != 0:
        abort(400)
    return chat_permissions.ensure_roles(chat)


@chats_bp.post("/<chat_id>/roles")
@validate_request(RoleBody)
async def create_chat_role(chat_id, data: RoleBody):
    chat = await get_group_chat_for_roles(chat_id)

    # Check create_roles permission
    if not chat_permissions.has(chat, request.user, "create_roles"):
        return missing_permission()

    # Non-owner/admin actors can only create roles below their own rank
    if request.user != chat["owner"] and \
            chat_permissions.rank(chat, request.user) < chat_permissions.ADMIN_TIER and \
            data.tier >= chat_permissions.rank(chat, request.user):
        return insufficient_rank()

    # Validate colours
    colors = clean_role_colors(data.colors)
    if colors is None:
        return {"error": True, "type": "badColors"}, 400

    # Validate permissions
    role = {
        "_id": chat_permissions.new_role_id(),
        "name": data.name,
        "tier": data.tier,
        "permissions": {},
        "colors": colors,
        "preset": False
    }
    for perm, val in data.permissions.items():
        if perm not in chat_permissions.PERMISSIONS:
            return {"error": True, "type": "unknownPermission"}, 400
        if val is True:
            if chat_permissions.PERMISSIONS[perm] > data.tier:
                return {"error": True, "type": "permissionAboveRoleTier"}, 400
            if not chat_permissions.can_grant_perm(chat, request.user, perm):
                return missing_permission()
        role["permissions"][perm] = val

    # Create role
    chat["roles"].append(role)
    db.chats.update_one({"_id": chat_id}, {"$push": {"roles": role}})

    # Send update chat event
    send_roles_update(chat)

    role["error"] = False
    return role, 200


@chats_bp.patch("/<chat_id>/roles/<role_id>")
@validate_request(RolePatchBody)
async def update_chat_role(chat_id, role_id, data: RolePatchBody):
    chat = await get_group_chat_for_roles(chat_id)

    # Tier is immutable
    if data.tier is not None:
        abort(400)

    # Get role
    role = get_role(chat, role_id)
    if not role:
        abort(404)

    # Validate colours
    colors = None
    if data.colors is not None:
        colors = clean_role_colors(data.colors)
        if colors is None:
            return {"error": True, "type": "badColors"}, 400

    # Check modify_roles permission. The administrator role can't be edited,
    # except that the owner may change its (purely cosmetic) colours.
    if role["_id"] == "administrator":
        if request.user != chat["owner"] or data.name is not None or data.permissions or colors is None:
            return missing_permission()
    elif not chat_permissions.has(chat, request.user, "modify_roles") or \
            not chat_permissions.can_manage_role(chat, request.user, role):
        return missing_permission()

    # Validate permission changes
    new_permissions = dict(role["permissions"])
    if data.permissions:
        for perm, val in data.permissions.items():
            if perm not in chat_permissions.PERMISSIONS:
                return {"error": True, "type": "unknownPermission"}, 400
            if role["permissions"].get(perm) == val:
                continue
            if val is True and chat_permissions.PERMISSIONS[perm] > role["tier"]:
                return {"error": True, "type": "permissionAboveRoleTier"}, 400
            if not chat_permissions.can_grant_perm(chat, request.user, perm):
                return missing_permission()
            if val is None:
                new_permissions.pop(perm, None)
            else:
                new_permissions[perm] = val

    # Update role
    if data.name is not None:
        role["name"] = data.name
    if colors is not None:
        role["colors"] = colors
    role["permissions"] = new_permissions
    db.chats.update_one({"_id": chat_id}, {"$set": {"roles": chat["roles"]}})

    # Send update chat event
    send_roles_update(chat)

    role["error"] = False
    return role, 200


@chats_bp.delete("/<chat_id>/roles/<role_id>")
async def delete_chat_role(chat_id, role_id):
    chat = await get_group_chat_for_roles(chat_id)

    # Get role
    role = get_role(chat, role_id)
    if not role:
        abort(404)

    # The default and administrator roles can never be deleted
    if role_id in {"default", "administrator"}:
        abort(403)

    # Check delete_roles permission
    if not chat_permissions.has(chat, request.user, "delete_roles") or \
            not chat_permissions.can_manage_role(chat, request.user, role):
        return missing_permission()

    # Remove role and unassign it from every member
    chat["roles"].remove(role)
    for roles in chat["member_roles"].values():
        while role_id in roles:
            roles.remove(role_id)
    db.chats.update_one({"_id": chat_id}, {
        "$set": {"roles": chat["roles"], "member_roles": chat["member_roles"]}
    })

    # Send update chat event
    send_roles_update(chat)

    return {"error": False}, 200


async def update_member_role(chat_id, username, role_id, assign: bool):
    chat = await get_group_chat_for_roles(chat_id)

    # Get role and check target is a member
    role = get_role(chat, role_id)
    if not role:
        abort(404)
    if username not in chat["members"]:
        abort(404)

    # Check assign_roles permission
    if not chat_permissions.has(chat, request.user, "assign_roles"):
        return missing_permission()

    # Actors cannot change their own roles unless they're the owner
    if username == request.user and request.user != chat["owner"]:
        return insufficient_rank()

    # Check hierarchy
    if not chat_permissions.outranks(chat, request.user, username):
        return insufficient_rank()

    # The administrator role can only be assigned/unassigned by the owner
    # and can never be removed from the owner
    if role_id == "administrator":
        if request.user != chat["owner"] or (not assign and username == chat["owner"]):
            return missing_permission()
    elif not chat_permissions.can_manage_role(chat, request.user, role):
        return missing_permission()

    # Update member roles
    member_roles = chat["member_roles"].setdefault(username, [])
    if assign and role_id not in member_roles:
        member_roles.append(role_id)
    elif not assign and role_id in member_roles:
        member_roles.remove(role_id)
    db.chats.update_one({"_id": chat_id}, {"$set": {f"member_roles.{username}": member_roles}})

    # Send update chat event
    send_roles_update(chat)

    return {"error": False}, 200


@chats_bp.put("/<chat_id>/members/<username>/roles/<role_id>")
async def assign_member_role(chat_id, username, role_id):
    return await update_member_role(chat_id, username, role_id, True)


@chats_bp.delete("/<chat_id>/members/<username>/roles/<role_id>")
async def unassign_member_role(chat_id, username, role_id):
    return await update_member_role(chat_id, username, role_id, False)
