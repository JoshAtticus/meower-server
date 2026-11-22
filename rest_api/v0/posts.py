from quart import Blueprint, current_app as app, request, abort
from quart_schema import validate_querystring, validate_request
from pydantic import BaseModel, Field
from typing import Optional
from threading import Thread
from copy import copy
import pymongo, uuid, time, emoji, msgpack, re

import security
import supporter as supporter_module
import chat_permissions
from database import db, rdb, get_total_pages
from uploads import claim_file, unclaim_file, file_is_media
from utils import log


posts_bp = Blueprint("posts_bp", __name__, url_prefix="/posts")


def can_view_post(post: dict) -> bool:
    if post["post_origin"] == "inbox":
        return post["u"] in ["Server", request.user]
    elif post["post_origin"] != "home":
        return bool(request.user) and bool(db.chats.count_documents({
            "_id": post["post_origin"],
            "members": request.user,
            "deleted": False
        }, limit=1))
    return True


def post_event_recipients(post: dict) -> Optional[list[str]]:
    if post["post_origin"] == "home" or (post["post_origin"] == "inbox" and post["u"] == "Server"):
        return None
    elif post["post_origin"] == "inbox":
        return [post["u"]]
    else:
        chat = db.chats.find_one({
            "_id": post["post_origin"],
            "deleted": False
        }, projection={"members": 1})
        return chat["members"] if chat else []


class PostIdQueryArgs(BaseModel):
    id: str = Field()
    s: Optional[str] = Field(default=None)

class ContextQueryArgs(BaseModel):
    limit: Optional[int] = Field(default=5, ge=1, le=10)
    s: Optional[str] = Field(default=None)

class PagedQueryArgs(BaseModel):
    page: Optional[int] = Field(default=1, ge=1)

class PostBody(BaseModel):
    content: Optional[str] = Field(default="", max_length=4000)
    nonce: Optional[str] = Field(default=None, max_length=64)
    attachments: Optional[list[str]] = Field(default_factory=list)
    reply_to: Optional[list[str]] = Field(default_factory=list)
    stickers: Optional[list[str]] = Field(default_factory=list)

    class Config:
        validate_assignment = True
        str_strip_whitespace = True

class ReportBody(BaseModel):
    reason: str = Field(default="No reason provided", max_length=2000)
    comment: str = Field(default="", max_length=2000)

    class Config:
        validate_assignment = True
        str_strip_whitespace = True


@posts_bp.get("/")
@validate_querystring(PostIdQueryArgs)
async def get_post(query_args: PostIdQueryArgs):    
    # Get post
    post = db.posts.find_one({"_id": query_args.id, "isDeleted": False})
    if not post:
        abort(404)

    # Check access
    if (post["post_origin"] == "inbox") and (post["u"] not in ["Server", request.user]):
        abort(404)
    elif post["post_origin"] not in ["home", "inbox"]:
        chat = db.chats.find_one({
            "_id": post["post_origin"],
            "deleted": False
        }, projection={"members": 1, "type": 1, "allow_sharing": 1})
        if not chat:
            abort(404)
        if request.user not in chat["members"] and \
                (chat.get("type") != 0 or not chat.get("allow_sharing", True) or
                 not security.valid_share_token(post["_id"], query_args.s)):
            abort(404)

    # Return post
    post["error"] = False
    return app.supporter.parse_posts_v0([post], requester=request.user)[0], 200


@posts_bp.get("/<post_id>/context")
@validate_querystring(ContextQueryArgs)
async def get_post_context(post_id, query_args: ContextQueryArgs):
    # Get post
    post = db.posts.find_one({"_id": post_id, "isDeleted": False})
    if not post:
        abort(404)

    # Check access
    if (post["post_origin"] == "inbox") and (post["u"] not in ["Server", request.user]):
        abort(404)
    elif post["post_origin"] not in ["home", "inbox"]:
        chat = db.chats.find_one({
            "_id": post["post_origin"],
            "deleted": False
        }, projection={"members": 1, "type": 1, "allow_sharing": 1, "share_context": 1})
        if not chat:
            abort(404)
        if request.user not in chat["members"] and \
                (chat.get("type") != 0 or not chat.get("allow_sharing", True) or
                 not chat.get("share_context", False) or
                 not security.valid_share_token(post["_id"], query_args.s)):
            abort(404)

    # Get surrounding posts (ULIDs sort by creation time)
    base_query = {
        "post_origin": post["post_origin"],
        "isDeleted": False
    }
    if post["post_origin"] == "inbox":
        base_query["u"] = {"$in": ["Server", request.user] if request.user else ["Server"]}
    before = list(db.posts.find(
        {**base_query, "_id": {"$gt": post_id}},
        sort=[("_id", pymongo.ASCENDING)],
        limit=query_args.limit
    ))
    after = list(db.posts.find(
        {**base_query, "_id": {"$lt": post_id}},
        sort=[("_id", pymongo.DESCENDING)],
        limit=query_args.limit
    ))

    return {
        "error": False,
        "post": app.supporter.parse_posts_v0([post], requester=request.user)[0],
        "before": app.supporter.parse_posts_v0(before, requester=request.user),
        "after": app.supporter.parse_posts_v0(after, requester=request.user)
    }, 200


@posts_bp.get("/<post_id>/share")
async def get_post_share_token(post_id: str):
    # Check authorization
    if not request.user:
        abort(401)

    # Check ratelimit
    if security.ratelimited(f"share:{request.user}"):
        abort(429)

    # Ratelimit
    security.ratelimit(f"share:{request.user}", 30, 60)

    # Get post
    post = db.posts.find_one({"_id": post_id, "isDeleted": False})
    if not post:
        abort(404)

    # Home posts are public already — no token needed
    if post["post_origin"] in ("home", "livechat"):
        return {"error": False, "token": None}, 200
    if post["post_origin"] == "inbox":
        abort(404)

    # Chat posts: members only; group chats need the share_posts permission
    chat = db.chats.find_one({
        "_id": post["post_origin"],
        "deleted": False
    })
    if not chat or request.user not in chat["members"]:
        abort(404)
    if chat["type"] == 1:
        return {"error": True, "type": "sharingUnavailable"}, 403
    if not chat_permissions.has(chat_permissions.ensure_roles(chat), request.user, "share_posts"):
        return {"error": True, "type": "missingPermission"}, 403

    return {"error": False, "token": security.share_token(post_id)}, 200


@posts_bp.patch("/")
@validate_querystring(PostIdQueryArgs)
@validate_request(PostBody)
async def update_post(query_args: PostIdQueryArgs, data: PostBody):
    # Check authorization
    if not request.user:
        abort(401)

    if not (request.flags & security.UserFlags.POST_RATELIMIT_BYPASS):
        # Check ratelimit
        if security.ratelimited(f"post:{request.user}"):
            abort(429)

        # Ratelimit
        security.ratelimit(f"post:{request.user}", 6, 5)
    
    # Get post
    post = db.posts.find_one({"_id": query_args.id, "isDeleted": False})
    if not post:
        abort(404)

    # Check access
    if (post["post_origin"] == "inbox") and (post["u"] != request.user):
        abort(404)
    elif post["post_origin"] not in ["home", "inbox"]:
        chat = db.chats.find_one({
            "_id": post["post_origin"],
            "members": request.user,
            "deleted": False
        })
        if not chat:
            abort(404)

    # Check permissions
    if post["post_origin"] == "inbox" or post["u"] != request.user:
        abort(403)

    # Check restrictions
    if post["post_origin"] == "home" and security.is_restricted(request.user, security.Restrictions.HOME_POSTS):
        return {"error": True, "type": "accountBanned"}, 403
    elif post["post_origin"] != "home" and security.is_restricted(request.user, security.Restrictions.CHAT_POSTS):
        return {"error": True, "type": "accountBanned"}, 403

    # Group chats can gate editing your own messages
    if post["post_origin"] not in ["home", "inbox"] and chat["type"] == 0:
        if not chat_permissions.has(chat_permissions.ensure_roles(chat), request.user, "edit_messages"):
            return {"error": True, "type": "missingPermission"}, 403

    # Can't edit the message that makes up a pending DM request
    if post["post_origin"] not in ["home", "inbox"] and chat["type"] == 1:
        dm_request = chat.get("dm_request")
        if dm_request and dm_request.get("from") == post["u"]:
            return {"error": True, "type": "dmRequestPending"}, 403

    # Make sure new content isn't the same as the old content
    if post["p"] == data.content:
        post["error"] = False
        return app.supporter.parse_posts_v0([post], requester=request.user)[0], 200

    # Make sure the post has text content
    if not data.content:
        abort(400)

    # Add revision
    db.post_revisions.insert_one({
        "_id": str(uuid.uuid4()),
        "post_id": post["_id"],
        "old_content": post["p"],
        "new_content": data.content,
        "time": int(time.time())
    })

    # Update post
    post["edited_at"] = int(time.time())
    post["p"] = data.content
    # custom emojis can be added or removed by an edit
    post["emojis"] = list(set(re.findall(supporter_module.CUSTOM_EMOJI_REGEX, post["p"])))
    db.posts.update_one({"_id": query_args.id}, {"$set": {
        "p": post["p"],
        "emojis": post["emojis"],
        "edited_at": post["edited_at"]
    }})

    # Send update post event
    app.cl.send_event("update_post", post, usernames=(None if post["post_origin"] == "home" else chat["members"]))

    # Return post
    post["error"] = False
    return app.supporter.parse_posts_v0([post], requester=request.user)[0], 200

@posts_bp.post("/<post_id>/report")
@validate_request(ReportBody)
async def report_post(post_id, data: ReportBody):
    if not request.user:
        abort(401)

    # Check ratelimit
    if security.ratelimited(f"report:{request.user}"):
        abort(429)

    # Get post and check access
    post = db.posts.find_one({"_id": post_id})
    if not post or not can_view_post(post):
        abort(404)

    # Send to files automod if there are attachments
    if len(post["attachments"]):
        rdb.publish("automod:files", msgpack.packb({
            "type": 2,
            "username": post["u"],
            "file_bucket": "attachments",
            "file_hashes": [file["hash"] for file in db.files.find({"_id": {"$in": post["attachments"]}})],
            "post_id": post["_id"],
            "post_content": post["p"]
        }))

    security.ratelimit(f"report:{request.user}", 3, 5)
    
    report = db.reports.find_one({
        "content_id": post_id,
        "status": "pending",
        "type": "post"
    })

    if not report:
        report = {
            "_id": str(uuid.uuid4()),
            "type": "post",
            "content_id": post_id,
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

    unique_ips = set([_report["ip"] for _report in report["reports"]])

    if report["status"] == "pending" and not report["escalated"] and len(unique_ips) >= 3:
        db.reports.update_one({"_id": report["_id"]}, {"$set": {"escalated": True}})
        db.posts.update_one({"_id": post_id, "isDeleted": False}, {"$set": {
            "isDeleted": True,
            "mod_deleted": True,
            "deleted_at": int(time.time())
        }})

    return {"error": False}, 200


@posts_bp.post("/<post_id>/pin")
async def pin_post(post_id):
    if not request.user:
        abort(401)
    post = db.posts.find_one({"_id": post_id})
    if not post:
        abort(404)
    query = {"_id": post["post_origin"]}

    has_perm = security.has_permission(request.permissions, security.AdminPermissions.EDIT_CHATS)
    if not has_perm:
        query["members"] = request.user
        query["deleted"] = False



    chat = db.chats.find_one(query)
    if not chat:
        abort(401)

    if not has_perm:
        allowed = chat.get("type") == 0 and chat_permissions.has(
            chat_permissions.ensure_roles(chat), request.user, "pin_messages")
        if not allowed:
            return {"error": True, "type": "missingPermission"}, 403

    was_pinned = post.get("pinned", False)

    db.posts.update_one({"_id": post_id}, {"$set": {
        "pinned": True
    }})

    post["pinned"] = True

    app.cl.send_event("update_post", post, usernames=(None if post["post_origin"] == "home" else chat["members"]))

    # Announce the change in the chat
    if was_pinned != True:
        app.supporter.create_post(chat["_id"], "Server", f"@{request.user} pinned a post from @{post['u']}.", chat_members=chat["members"])

    post["error"] = False
    return app.supporter.parse_posts_v0([post], requester=request.user)[0], 200


@posts_bp.delete("/<post_id>/pin")
async def unpin_post(post_id):
    if not request.user:
        abort(401)

    post = db.posts.find_one({"_id": post_id})
    if not post:
        abort(404)

    query = {"_id": post["post_origin"]}
    has_perm = security.has_permission(request.permissions, security.AdminPermissions.EDIT_CHATS)
    if not has_perm:
        query["members"] = request.user
        query["deleted"] = False

    chat = db.chats.find_one(query)
    if not chat:
        abort(401)

    if not has_perm:
        allowed = chat.get("type") == 0 and chat_permissions.has(
            chat_permissions.ensure_roles(chat), request.user, "unpin_messages")
        if not allowed:
            return {"error": True, "type": "missingPermission"}, 403


    was_pinned = post.get("pinned", False)

    db.posts.update_one({"_id": post_id}, {"$set": {
        "pinned": False
    }})

    post["pinned"] = False

    app.cl.send_event("update_post", post, usernames=(None if post["post_origin"] == "home" else chat["members"]))

    # Announce the change in the chat
    if was_pinned != False:
        app.supporter.create_post(chat["_id"], "Server", f"@{request.user} unpinned a post from @{post['u']}.", chat_members=chat["members"])

    post["error"] = False
    return app.supporter.parse_posts_v0([post], requester=request.user)[0], 200


@posts_bp.delete("/<post_id>/attachments/<attachment_id>")
async def delete_attachment(post_id: str, attachment_id: str):
    # Check authorization
    if not request.user:
        abort(401)
    
    # Get post
    post = db.posts.find_one({"_id": post_id, "isDeleted": False})
    if not post:
        abort(404)

    # Check access
    if (post["post_origin"] == "inbox") and (post["u"] != request.user):
        abort(404)
    elif post["post_origin"] not in ["home", "inbox"]:
        chat = db.chats.find_one({
            "_id": post["post_origin"],
            "members": request.user,
            "deleted": False
        })
        if not chat:
            abort(404)

    # Check permissions
    if post["post_origin"] == "inbox" or post["u"] != request.user:
        abort(403)

    # Delete attachment
    if attachment_id in post["attachments"]:
        post["attachments"].remove(attachment_id)
        unclaim_file(attachment_id)
    else:
        abort(404)

    if post["p"] or post["attachments"] > 0:
        # Update post
        db.posts.update_one({"_id": post_id}, {"$set": {
            "attachments": post["attachments"]
        }})

        # Send update post event
        app.cl.send_event("update_post", post, usernames=(None if post["post_origin"] == "home" else chat["members"]))
    else:  # delete post if no content and attachments remain
        # Update post
        db.posts.update_one({"_id": post_id}, {"$set": {
            "isDeleted": True,
            "deleted_at": int(time.time())
        }})

        # Send delete post event
        app.cl.send_event("delete_post", {
            "chat_id": post["post_origin"],
            "post_id": post_id
        }, usernames=(None if post["post_origin"] == "home" else chat["members"]))

    # Return post
    post["error"] = False
    return app.supporter.parse_posts_v0([post], requester=request.user)[0], 200


@posts_bp.delete("/")
@validate_querystring(PostIdQueryArgs)
async def delete_post(query_args: PostIdQueryArgs):
    # Check authorization
    if not request.user:
        abort(401)

    if not (request.flags & security.UserFlags.POST_RATELIMIT_BYPASS):
        # Check ratelimit
        if security.ratelimited(f"post:{request.user}"):
            abort(429)

        # Ratelimit
        security.ratelimit(f"post:{request.user}", 6, 5)
    
    # Get post
    post = db.posts.find_one({"_id": query_args.id, "isDeleted": False})
    if not post:
        abort(404)

    # Check access
    if post["post_origin"] not in {"home", "inbox"}:
        chat = db.chats.find_one({
            "_id": post["post_origin"],
            "members": request.user,
            "deleted": False
        }, projection={"owner": 1, "members": 1, "type": 1, "announce_deletes": 1,
                       "announce_deleter": 1, "roles": 1, "member_roles": 1})
        if not chat:
            abort(404)
    if post["post_origin"] in {"home", "inbox"}:
        if post["post_origin"] == "inbox" or post["u"] != request.user:
            abort(403)
    elif chat["type"] == 0:
        chat = chat_permissions.ensure_roles(chat)
        if post["u"] == request.user:
            if not chat_permissions.has(chat, request.user, "delete_own_messages"):
                return {"error": True, "type": "missingPermission"}, 403
        elif post["u"] == "Server":
            if chat_permissions.rank(chat, request.user) < chat_permissions.ADMIN_TIER:
                return {"error": True, "type": "insufficientRank"}, 403
        else:
            if not chat_permissions.has(chat, request.user, "delete_others_messages"):
                return {"error": True, "type": "missingPermission"}, 403
            if not chat_permissions.outranks(chat, request.user, post["u"]):
                return {"error": True, "type": "insufficientRank"}, 403
    elif post["u"] != request.user:
        abort(403)

    # Delete attachments
    for attachment in post["attachments"]:
        try:
            unclaim_file(attachment["id"])
        except Exception as e:
            log(f"Unable to delete attachment: {e}")

    # Update post
    db.posts.update_one({"_id": query_args.id}, {"$set": {
        "isDeleted": True,
        "deleted_at": int(time.time())
    }})

    # Send delete post event
    app.cl.send_event("delete_post", {
        "chat_id": post["post_origin"],
        "post_id": query_args.id
    }, usernames=(None if post["post_origin"] == "home" else chat["members"]))

    # Announce the deletion in group chats that have it enabled
    if post["post_origin"] not in {"home", "inbox"} \
            and chat["type"] == 0 \
            and chat.get("announce_deletes", False) \
            and post["u"] != "Server":
        if not chat.get("announce_deleter", False):
            text = f"A message from @{post['u']} was deleted."
        elif request.user == post["u"]:
            text = f"@{request.user} deleted their message."
        else:
            text = f"@{request.user} deleted a message from @{post['u']}."
        app.supporter.create_post(chat["_id"], "Server", text, chat_members=chat["members"])

    return {"error": False}, 200


@posts_bp.get("/<chat_id>")
@validate_querystring(PagedQueryArgs)
async def get_chat_posts(chat_id, query_args: PagedQueryArgs):
    # Check authorization
    if not request.user:
        abort(401)

    # Make sure chat exists
    if not db.chats.count_documents({
        "_id": chat_id,
        "members": request.user,
        "deleted": False
    }, limit=1):
        abort(404)

    # Get and return posts
    query = {"post_origin": chat_id, "isDeleted": False}
    return {
        "error": False,
        "autoget": app.supporter.parse_posts_v0(db.posts.find(
            query,
            sort=[("t.e", pymongo.DESCENDING), ("_id", pymongo.DESCENDING)],
            skip=(query_args.page-1)*25,
            limit=25
        ), requester=request.user),
        "page#": query_args.page,
        "pages": (get_total_pages("posts", query) if request.user else 1)
    }, 200


@posts_bp.post("/<chat_id>")
@validate_request(PostBody)
async def create_chat_post(chat_id, data: PostBody):
    # Check authorization
    if not request.user:
        abort(401)

    if not (request.flags & security.UserFlags.POST_RATELIMIT_BYPASS):
        # Check ratelimit
        if security.ratelimited(f"post:{request.user}"):
            abort(429)

        # Ratelimit
        security.ratelimit(f"post:{request.user}", 6, 5)

    # Check restrictions
    if security.is_restricted(request.user, security.Restrictions.CHAT_POSTS):
        return {"error": True, "type": "accountBanned"}, 403

    # Make sure there's not too many attachments
    if len(data.attachments) > 10:
        return {"error": True, "type": "tooManyAttachments"}, 400

    # Make sure the post isn't replying to too many posts
    if len(data.reply_to) > 10:
        return {"error": True, "type": "tooManyReplies"}, 400
    
    # Make sure there's not too many stickers
    if len(data.stickers) > 10:
        return {"error": True, "type": "tooManyStickers"}, 400

    # Make sure stickers exist
    for sticker_id in copy(data.stickers):
        if not db.chat_stickers.count_documents({"_id": sticker_id}, limit=1):
            data.stickers.remove(sticker_id)

    # Make sure replied to post IDs exist and are unique
    unique_reply_to_post_ids = []
    if chat_id != "livechat":
        for post_id in data.reply_to:
            if db.posts.count_documents({"_id": post_id, "post_origin": chat_id}, limit=1) and \
                post_id not in unique_reply_to_post_ids:
                unique_reply_to_post_ids.append(post_id)

    # Claim attachments (images only)
    attachments = []
    if chat_id != "livechat":
        for attachment_id in data.attachments:
            if attachment_id in attachments:
                continue
            file = db.files.find_one({"_id": attachment_id, "bucket": "attachments", "uploaded_by": request.user})
            if file and not file_is_media(file):
                return {"error": True, "type": "mediaOnly"}, 400
            try:
                claim_file(attachment_id, "attachments", request.user)
            except Exception as e:
                log(f"Unable to claim attachment: {e}")
                return {"error": True, "type": "unableToClaimAttachment"}, 500
            else:
                attachments.append(attachment_id)

    # Make sure the post has text content or at least 1 attachment or at least 1 sticker
    if not data.content and not attachments and not data.stickers:
        abort(400)

    dm_request_update = None  # dict to set, or "unset" when the recipient replies
    if chat_id != "livechat":
        # Get chat
        chat = db.chats.find_one({
            "_id": chat_id,
            "members": request.user,
            "deleted": False
        }, projection={"type": 1, "members": 1, "bans": 1, "dm_request": 1,
                       "owner": 1, "roles": 1, "member_roles": 1})
        if not chat:
            abort(404)

        # Make sure the user isn't banned from the chat
        ban = security.chat_ban(chat, request.user)
        if ban:
            return {"error": True, "type": "chatBanned", "expires": ban["expires"]}, 403

        # Group chats require permission to send messages
        if chat["type"] == 0:
            chat = chat_permissions.ensure_roles(chat)
            if not chat_permissions.has(chat, request.user, "send_messages"):
                return {"error": True, "type": "missingPermission"}, 403

        # DM stuff
        if chat["type"] == 1:
            # Enforce pending DM requests: the requester gets one text-only
            # message, then must wait for the recipient to accept. This is
            # checked before the blocked-privacy check so a blocked sender
            # can't tell the difference.
            chat = security.normalize_dm_request(chat)
            dm_request = chat.get("dm_request")
            if dm_request:
                if dm_request["from"] is None:
                    # Whoever posts first becomes the requester
                    if attachments or data.stickers:
                        return {"error": True, "type": "dmRequestTextOnly"}, 403
                    dm_request_update = {
                        "from": request.user,
                        "to": next(member for member in chat["members"] if member != request.user),
                        "created": int(time.time()),
                        "first_message_sent": True,
                        "ignored": False
                    }
                elif dm_request["from"] == request.user:
                    return {"error": True, "type": "dmRequestPending"}, 403
                else:
                    # The recipient replying accepts the request
                    dm_request_update = "unset"

            # Check privacy options
            if db.relationships.count_documents({"$or": [
                {"_id": {"from": chat["members"][0], "to": chat["members"][1]}},
                {"_id": {"from": chat["members"][1], "to": chat["members"][0]}}
            ], "state": 2}, limit=1) > 0:
                abort(403)

            # Update user settings
            Thread(target=db.user_settings.bulk_write, args=([
                pymongo.UpdateMany({"$or": [
                    {"_id": chat["members"][0]},
                    {"_id": chat["members"][1]}
                ]}, {"$pull": {"active_dms": chat_id}}),
                pymongo.UpdateMany({"$or": [
                    {"_id": chat["members"][0]},
                    {"_id": chat["members"][1]}
                ]}, {"$push": {"active_dms": {
                    "$each": [chat_id],
                    "$position": 0,
                    "$slice": -150
                }}})
            ],)).start()

    # Create post
    post = app.supporter.create_post(
        chat_id,
        request.user,
        data.content,
        attachments=attachments,
        stickers=data.stickers,
        nonce=data.nonce,
        chat_members=(None if chat_id == "livechat" else chat["members"]),
        reply_to=unique_reply_to_post_ids
    )

    # Apply any DM request change and tell both members (public shape only)
    if dm_request_update == "unset":
        db.chats.update_one({"_id": chat_id}, {"$unset": {"dm_request": ""}})
        app.cl.send_event("update_chat", {
            "_id": chat_id,
            "dm_request": None
        }, usernames=chat["members"])
    elif dm_request_update:
        db.chats.update_one({"_id": chat_id}, {"$set": {"dm_request": dm_request_update}})
        app.cl.send_event("update_chat", {
            "_id": chat_id,
            "dm_request": security.public_dm_request(dm_request_update)
        }, usernames=chat["members"])

    # Return new post
    post["error"] = False
    return app.supporter.parse_posts_v0([post], requester=request.user)[0], 200

@posts_bp.get("/<post_id>/reactions/<emoji_reaction>")
@validate_querystring(PagedQueryArgs)
async def get_post_reactors(query_args: PagedQueryArgs, post_id: str, emoji_reaction: str):
    # Get necessary post details and check access
    post = db.posts.find_one({
        "_id": post_id,
        "isDeleted": {"$ne": True}
    }, projection={"_id": 1, "post_origin": 1, "u": 1})
    if not post:
        abort(404)
    elif post["post_origin"] != "home" and not request.user:
        abort(404)
    elif post["post_origin"] == "inbox" and post["u"] not in ["Server", request.user]:
        abort(404)
    elif post["post_origin"] not in ["home", "inbox"]:
        if not db.chats.count_documents({
            "_id": post["post_origin"],
            "members": request.user,
            "deleted": False
        }, limit=1):
            abort(404)

    # Get and return reactors
    query = {"_id.post_id": post_id, "_id.emoji": emoji_reaction}
    return {
        "error": False,
        "autoget": [security.get_account(r["_id"]["user"]) for r in db.post_reactions.find(
            query,
            sort=[("time", pymongo.DESCENDING)],
            skip=(query_args.page-1)*25,
            limit=25
        )],
        "page#": query_args.page,
        "pages": (get_total_pages("post_reactions", query) if request.user else 1)
    }, 200

@posts_bp.post("/<post_id>/reactions/<emoji_reaction>")
async def add_post_reaction(post_id: str, emoji_reaction: str):
    # Check authorization
    if not request.user:
        abort(401)

    # Ratelimit
    if security.ratelimited(f"react:{request.user}"):
        abort(429)
    security.ratelimit(f"react:{request.user}", 5, 5)

    # Check if the emoji is only one emoji, with support for variants
    if not (emoji.purely_emoji(emoji_reaction) and len(emoji.distinct_emoji_list(emoji_reaction)) == 1):
        # Check if the emoji is a custom emoji
        if not db.chat_emojis.count_documents({"_id": emoji_reaction}, limit=1):
            abort(400)

    # Get necessary post details and check access
    post = db.posts.find_one({
        "_id": post_id,
        "isDeleted": {"$ne": True}
    }, projection={
        "_id": 1,
        "post_origin": 1,
        "u": 1,
        "reactions": 1
    })
    if not post:
        abort(404)
    elif post["post_origin"] == "inbox" and post["u"] not in ["Server", request.user]:
        abort(404)
    elif post["post_origin"] not in ["home", "inbox"]:
        chat = db.chats.find_one({
            "_id": post["post_origin"],
            "members": request.user,
            "deleted": False
        })
        if not chat:
            abort(404)
        if chat["type"] == 0:
            chat = chat_permissions.ensure_roles(chat)
            # Joining an existing reaction needs `react`; adding an emoji the
            # post doesn't have yet needs `add_reactions`
            already_used = any(
                reaction["emoji"] == emoji_reaction
                for reaction in post.get("reactions", [])
            )
            if not chat_permissions.has(chat, request.user,
                                        "react" if already_used else "add_reactions"):
                return {"error": True, "type": "missingPermission"}, 403

    # Make sure there's not too many reactions (50)
    if len(post["reactions"]) >= 50:
        return {"error": True, "type": "tooManyReactions"}, 403

    # Add reaction
    db.post_reactions.update_one({"_id": {
        "post_id": post["_id"],
        "emoji": emoji_reaction,
        "user": request.user
    }}, {"$set": {"time": int(time.time())}}, upsert=True)

    # Update post
    existing_reaction = None
    for reaction in post["reactions"]:
        if reaction["emoji"] == emoji_reaction:
            existing_reaction = reaction
            break
    if existing_reaction:
        existing_reaction["count"] = db.post_reactions.count_documents({
            "_id.post_id": post["_id"],
            "_id.emoji": reaction["emoji"]
        })
    else:
        post["reactions"].append({
            "emoji": emoji_reaction,
            "count": 1
        })
    db.posts.update_one({"_id": post["_id"]}, {"$set": {
        "reactions": post["reactions"]
    }})

    # Send event
    app.cl.send_event("post_reaction_add", {
        "chat_id": post["post_origin"],
        "post_id": post["_id"],
        "emoji": emoji_reaction,
        "username": request.user
    }, usernames=post_event_recipients(post))

    return {"error": False}, 200

@posts_bp.delete("/<post_id>/reactions/<emoji_reaction>/<username>")
async def remove_post_reaction(post_id: str, emoji_reaction: str, username: str):
    # Check authorization
    if not request.user:
        abort(401)

    # @me -> requester
    if username == "@me":
        username = request.user

    # Ratelimit
    if security.ratelimited(f"react:{request.user}"):
        abort(429)
    security.ratelimit(f"react:{request.user}", 5, 5)

    # Get necessary post details and check access
    post = db.posts.find_one({
        "_id": post_id,
        "isDeleted": {"$ne": True}
    }, projection={
        "_id": 1,
        "post_origin": 1,
        "u": 1,
        "reactions": 1
    })
    if not post:
        abort(404)
    elif post["post_origin"] == "inbox" and post["u"] not in ["Server", request.user]:
        abort(404)
    elif post["post_origin"] not in ["home", "inbox"]:
        chat = db.chats.find_one({
            "_id": post["post_origin"],
            "members": request.user,
            "deleted": False
        }, projection={"owner": 1})
        if not chat:
            abort(404)

    # Make sure requester can remove the reaction
    if request.user != username:
        if (post["post_origin"] in ["home", "inbox"]) or (chat["owner"] != request.user):
            abort(403)

    # Make sure reaction exists
    if not db.post_reactions.count_documents({"_id": {
        "post_id": post["_id"],
        "emoji": emoji_reaction,
        "user": username
    }}, limit=1):
        abort(404)

    # Remove reaction
    db.post_reactions.delete_one({"_id": {
        "post_id": post["_id"],
        "emoji": emoji_reaction,
        "user": username
    }})

    # Update post
    for reaction in post["reactions"]:
        if reaction["emoji"] != emoji_reaction:
            continue
        reaction["count"] = db.post_reactions.count_documents({
            "_id.post_id": post["_id"],
            "_id.emoji": reaction["emoji"]
        })
        if not reaction["count"]:
            post["reactions"].remove(reaction)
        break
    db.posts.update_one({"_id": post["_id"]}, {"$set": {
        "reactions": post["reactions"]
    }})

    # Send event
    app.cl.send_event("post_reaction_remove", {
        "chat_id": post["post_origin"],
        "post_id": post["_id"],
        "emoji": emoji_reaction,
        "username": username
    }, usernames=post_event_recipients(post))

    return {"error": False}, 200
