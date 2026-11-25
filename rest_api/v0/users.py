from quart import Blueprint, current_app as app, request, abort
from quart_schema import validate_querystring, validate_request
from pydantic import BaseModel, Field
from typing import Literal, Optional
import pymongo
import uuid
import time

import security
import email_verification
import username_change
from database import db, get_total_pages


users_bp = Blueprint("users_bp", __name__, url_prefix="/users/<username>")

class GetPostsQueryArgs(BaseModel):
    page: Optional[int] = Field(default=1, ge=1)

class UpdateRelationshipBody(BaseModel):
    state: Literal[
        0,  # no relationship
        1,  # friend request / friend (when mutual)
        2,  # blocking
    ]
    notify: Optional[bool] = Field(default=False)

    class Config:
        validate_assignment = True

class ReportBody(BaseModel):
    reason: str = Field(default="No reason provided", min_length=1, max_length=2000)
    comment: str = Field(default="", max_length=2000)

    class Config:
        validate_assignment = True
        str_strip_whitespace = True

@users_bp.before_request
async def check_user_exists():
    username = request.view_args.get("username")
    user = db.usersv0.find_one({"lower_username": username.lower()}, projection={"_id": 1, "flags": 1})
    request.renamed_from = None
    if not user:
        # links to a username that was recently changed follow the account
        held = username_change.reservation(username.lower())
        if held:
            user = db.usersv0.find_one({"uuid": held["uuid"]}, projection={"_id": 1, "flags": 1})
            if user:
                request.renamed_from = held["old_name"]
    if (not user) or (user["flags"] & security.UserFlags.DELETED == security.UserFlags.DELETED):
        abort(404)
    else:
        request.view_args["username"] = user["_id"]


@users_bp.get("/")
async def get_user(username):
    account = security.get_account(username, (request.user and request.user.lower() == username.lower()))
    account["error"] = False
    if request.renamed_from:
        account["renamed_from"] = request.renamed_from
    return account, 200


@users_bp.get("/posts")
@validate_querystring(GetPostsQueryArgs)
async def get_user_posts(username, query_args: GetPostsQueryArgs):
    query = {"post_origin": "home", "isDeleted": False, "u": username}
    return {
        "error": False,
        "autoget": app.supporter.parse_posts_v0(db.posts.find(
            query,
            sort=[("t.e", pymongo.DESCENDING), ("_id", pymongo.DESCENDING)],
            skip=(query_args.page-1)*25,
            limit=25
        ), requester=request.user),
        "page#": query_args.page,
        "pages": get_total_pages("posts", query)
    }, 200


@users_bp.get("/relationship")
async def get_relationship(username):
    # Check authorization
    if not request.user:
        abort(401)

    # Make sure the requested user isn't the requester
    if request.user == username:
        abort(400)

    # Get relationship
    relationship = db.relationships.find_one({"_id": {"from": request.user, "to": username}})

    # Return relationship
    if relationship:
        del relationship["_id"]
        return relationship, 200
    else:
        return {
            "state": 0,
            "updated_at": None
        }, 200


@users_bp.patch("/relationship")
@validate_request(UpdateRelationshipBody)
async def update_relationship(username, data: UpdateRelationshipBody):
    # Check authorization
    if not request.user:
        abort(401)
    if data.state == 1:
        if resp := email_verification.require_verified(request.user):
            return resp

    # Check ratelimit
    if security.ratelimited(f"relationships:{request.user}"):
        abort(429)

    # Ratelimit
    security.ratelimit(f"relationships:{request.user}", 10, 15)

    # Make sure the requested user isn't the requester
    if request.user == username:
        abort(400)

    # Get relationship
    relationship = db.relationships.find_one({"_id": {"from": request.user, "to": username}})
    if not relationship:
        relationship = {
            "_id": {"from": request.user, "to": username},
            "state": 0,
            "updated_at": None
        }

    # Removing a relationship also clears a pending friend request from them
    if data.state == 0:
        res = db.relationships.delete_one({"_id": {"from": username, "to": request.user}, "state": 1})
        if res.deleted_count:
            app.cl.send_event("update_friends", {}, usernames=[request.user, username])

    # Make sure the state relationship state changed
    if data.state == relationship["state"]:
        del relationship["_id"]
        return relationship, 200

    # Update relationship
    was_blocked = relationship["state"] == 2
    relationship["state"] = data.state
    relationship["updated_at"] = int(time.time())
    if data.state == 0:
        db.relationships.delete_one({"_id": {"from": request.user, "to": username}})
    else:
        db.relationships.update_one({"_id": {"from": request.user, "to": username}}, {"$set": relationship}, upsert=True)

    # Unblocking someone restarts the expiry countdown on any DM request
    # they sent the requester, and removes the block notice from their DM
    if was_blocked:
        for dm in db.chats.find({
            "type": 1,
            "members": {"$all": [request.user, username]},
            "dm_request.from": username,
            "dm_request.to": request.user,
            "dm_request.first_message_sent": True
        }, projection={"_id": 1}):
            db.chats.update_one({"_id": dm["_id"]}, {
                "$set": {"dm_request.created": int(time.time())}
            })
        dm = db.chats.find_one({
            "type": 1,
            "members": {"$all": [request.user, username]},
            "deleted": False
        }, projection={"members": 1, "block_notice": 1})
        if dm and dm.get("block_notice") == request.user:
            db.chats.update_one({"_id": dm["_id"]}, {
                "$unset": {"block_notice": ""}
            })
            app.cl.send_event("update_chat", {
                "_id": dm["_id"],
                "block_notice": None
            }, usernames=dm["members"])

    # Optionally let the blocked user know they were blocked by giving them
    # the DM with a block notice
    if data.state == 2 and data.notify:
        dm = db.chats.find_one({
            "type": 1,
            "members": {"$all": [request.user, username]},
            "deleted": False
        })
        if not dm:
            dm = {
                "_id": str(uuid.uuid4()),
                "type": 1,
                "nickname": None,
                "owner": None,
                "members": [request.user, username],
                "created": int(time.time()),
                "last_active": int(time.time()),
                "deleted": False
            }
            if not security.are_friends(request.user, username):
                # Keep the message request rules if they're unblocked later
                dm["dm_request"] = {
                    "from": None,
                    "to": None,
                    "created": 0,
                    "first_message_sent": False,
                    "ignored": False
                }
            db.chats.insert_one(dm)
        db.chats.update_one({"_id": dm["_id"]}, {
            "$set": {"block_notice": request.user}
        })

        # Make sure the chat is at the front of the target's active DMs
        already_active = db.user_settings.count_documents({
            "_id": username,
            "active_dms": dm["_id"]
        }, limit=1) > 0
        db.user_settings.update_one({"_id": username}, {
            "$pull": {"active_dms": dm["_id"]}
        })
        db.user_settings.update_one({"_id": username}, {
            "$push": {"active_dms": {
                "$each": [dm["_id"]],
                "$position": 0,
                "$slice": -150
            }}
        })

        # Notify both members; if the target didn't already have the DM in
        # their list, send them the chat too
        app.cl.send_event("update_chat", {
            "_id": dm["_id"],
            "block_notice": request.user
        }, usernames=dm["members"])
        if not already_active:
            dm.update({
                "emojis": list(db.chat_emojis.find({
                    "chat_id": dm["_id"]
                }, projection={"chat_id": 0, "created_at": 0, "created_by": 0})),
                "stickers": list(db.chat_stickers.find({
                    "chat_id": dm["_id"]
                }, projection={"chat_id": 0, "created_at": 0, "created_by": 0}))
            })
            dm["block_notice"] = request.user
            if dm.get("dm_request"):
                dm["dm_request"] = security.public_dm_request(dm["dm_request"])
            app.cl.send_event("create_chat", dm, usernames=[username])

    # Friend request/friendship notifications
    if data.state == 1:
        reciprocal = db.relationships.find_one({"_id": {"from": username, "to": request.user}, "state": 1})
        if reciprocal:
            app.supporter.create_post("inbox", username, f"@{request.user} accepted your friend request!", category="friends")
            app.supporter.create_post("inbox", request.user, f"You are now friends with @{username}!", category="friends")
            # Becoming friends auto-accepts any pending DM request between them
            for dm in db.chats.find({"type": 1, "members": {"$all": [request.user, username]}, "dm_request": {"$exists": True}}, projection={"members": 1}):
                db.chats.update_one({"_id": dm["_id"]}, {"$unset": {"dm_request": ""}})
                app.cl.send_event("update_chat", {
                    "_id": dm["_id"],
                    "dm_request": None
                }, usernames=dm["members"])
        else:
            app.supporter.create_post("inbox", username, f"@{request.user} sent you a friend request! Head to the friends page to accept it.", category="friends")
        app.cl.send_event("update_friends", {}, usernames=[request.user, username])
    elif data.state == 0:
        app.cl.send_event("update_friends", {}, usernames=[request.user, username])
    elif data.state == 2:
        # Blocking someone also removes any friend request/friendship from them
        db.relationships.delete_one({"_id": {"from": username, "to": request.user}, "state": 1})
        app.cl.send_event("update_friends", {}, usernames=[request.user, username])

    # Sync relationship between sessions
    app.cl.send_event("update_relationship", {
        "username": username,
        "state": relationship["state"],
        "updated_at": relationship["updated_at"]
    }, usernames=[request.user])

    # Return updated relationship
    del relationship["_id"]
    return relationship, 200

@users_bp.post("/report")
@validate_request(ReportBody)
async def report_post(username, data: ReportBody):
    if not request.user:
        abort(401)

    security.ratelimit(f"report:{request.user}", 3, 5)
    
    report = db.reports.find_one({
        "content_id": username,
        "status": "pending",
        "type": "user"
    })

    if not report:
        report = {
            "_id": str(uuid.uuid4()),
            "type": "user",
            "content_id": username,
            "status": "pending",
            "escalated": False,
            "reports": []
        }

    for _report in report["reports"]:
        if _report["user"] == request.user:
            report["reports"].remove(_report)
            break
    
    report["reports"].append({
        "user": request.user,
        "ip": request.ip,
        "reason": data.reason,
        "comment": data.comment,
        "time": int(time.time())
    })

    db.reports.update_one({"_id": report["_id"]}, {"$set": report}, upsert=True)

    return {"error": False}, 200


@users_bp.get("/dm")
async def get_dm_chat(username):
    # Check authorization
    if not request.user:
        abort(401)
    if resp := email_verification.require_verified(request.user):
        return resp

    # Check ratelimit
    if security.ratelimited(f"create_chat:{request.user}"):
        abort(429)

    # Ratelimit
    security.ratelimit(f"create_chat:{request.user}", 5, 30)

    # Make sure the requested user isn't the requester
    if request.user == username:
        abort(400)

    # Get existing chat or create new chat
    chat = db.chats.find_one({
        "members": {"$all": [request.user, username]},
        "type": 1,
        "deleted": False
    })
    if not chat:
        # Check restrictions
        if security.is_restricted(request.user, security.Restrictions.NEW_CHATS):
            return {"error": True, "type": "accountBanned"}, 403

        # Create chat (a DM request if the users aren't friends)
        chat = {
            "_id": str(uuid.uuid4()),
            "type": 1,
            "nickname": None,
            "owner": None,
            "members": [request.user, username],
            "created": int(time.time()),
            "last_active": 0,
            "deleted": False
        }
        if not security.are_friends(request.user, username):
            chat["dm_request"] = {
                "from": None,
                "to": None,
                "created": 0,
                "first_message_sent": False,
                "ignored": False
            }
        db.chats.insert_one(chat)
    else:
        chat = security.normalize_dm_request(chat)

        # Reopening a DM the requester doesn't have in their active DMs (they
        # deleted it or never had it) requires a new request between non-friends
        if "dm_request" not in chat and not security.are_friends(request.user, username):
            if not db.user_settings.count_documents({
                "_id": request.user,
                "active_dms": chat["_id"]
            }, limit=1):
                chat["dm_request"] = {
                    "from": None,
                    "to": None,
                    "created": 0,
                    "first_message_sent": False,
                    "ignored": False
                }
                db.chats.update_one({"_id": chat["_id"]}, {
                    "$set": {"dm_request": chat["dm_request"]}
                })

    # Never expose the internal dm_request fields to clients
    if "dm_request" in chat:
        chat["dm_request"] = security.public_dm_request(chat["dm_request"])

    # Return chat
    if chat["last_active"] == 0:
        chat["last_active"] = int(time.time())
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
