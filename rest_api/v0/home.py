from quart import Blueprint, current_app as app, request, abort
from quart_schema import validate_querystring, validate_request
from pydantic import BaseModel, Field
from typing import Optional
import pymongo, copy

import security
from database import db, get_total_pages
from forwarding import resolve_forward
from uploads import claim_file, file_is_media
from utils import log


home_bp = Blueprint("home_bp", __name__, url_prefix="/home")


class GetHomeQueryArgs(BaseModel):
    page: Optional[int] = Field(default=1, ge=1)

class PostBody(BaseModel):
    content: Optional[str] = Field(default="", max_length=4000)
    nonce: Optional[str] = Field(default=None, max_length=64)
    attachments: Optional[list[str]] = Field(default_factory=list)
    reply_to: Optional[list[str]] = Field(default_factory=list)
    stickers: Optional[list[str]] = Field(default_factory=list)
    reply_only: Optional[bool] = False
    forward_of: Optional[str] = Field(default=None, max_length=64)

    class Config:
        validate_assignment = True
        str_strip_whitespace = True


@home_bp.get("/")
@validate_querystring(GetHomeQueryArgs)
async def get_home_posts(query_args: GetHomeQueryArgs):
    if not request.user:
        query_args.page = 1
    query = {"post_origin": "home", "isDeleted": False, "reply_only": {"$ne": True}}
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


@home_bp.post("/")
@validate_request(PostBody)
async def create_home_post(data: PostBody):
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
    if security.is_restricted(request.user, security.Restrictions.HOME_POSTS):
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
    for sticker_id in copy.copy(data.stickers):
        if not db.chat_stickers.count_documents({"_id": sticker_id}, limit=1):
            data.stickers.remove(sticker_id)

    # Make sure replied to post IDs exist and are unique
    unique_reply_to_post_ids = []
    for post_id in data.reply_to:
        if db.posts.count_documents({"_id": post_id, "post_origin": "home"}, limit=1) and \
            post_id not in unique_reply_to_post_ids:
            unique_reply_to_post_ids.append(post_id)

    # Claim attachments (images/videos only)
    attachments = []
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

    # Forwarded messages carry a reference to the original
    forwarded_from = None
    if data.forward_of:
        forwarded_from, forward_error = resolve_forward(data.forward_of, request.user)
        if forward_error:
            return forward_error

    # Make sure the post has text content or at least 1 attachment or at least 1 sticker or a forwarded message
    if not data.content and not attachments and not data.stickers and not forwarded_from:
        abort(400)

    # Create post
    post = app.supporter.create_post(
        "home",
        request.user,
        data.content,
        attachments=attachments,
        stickers=data.stickers,
        nonce=data.nonce,
        reply_to=unique_reply_to_post_ids,
        forwarded_from=forwarded_from,
        reply_only=bool(unique_reply_to_post_ids and (data.reply_only or db.posts.count_documents(
            {"_id": {"$in": unique_reply_to_post_ids}, "reply_to.0": {"$exists": True}}, limit=1)))
    )

    # Return new post
    post["error"] = False
    return app.supporter.parse_posts_v0([post], requester=request.user)[0], 200


@home_bp.post("/typing")
async def emit_typing():
    # Check authorization
    if not request.user:
        abort(401)

    # Check ratelimit
    if security.ratelimited(f"typing:{request.user}"):
        abort(429)

    # Ratelimit
    security.ratelimit(f"typing:{request.user}", 6, 5)

    # Check restrictions
    if security.is_restricted(request.user, security.Restrictions.HOME_POSTS):
        return {"error": True, "type": "accountBanned"}, 403

    # Send new state
    app.cl.send_event("typing", {"chat_id": "home", "username": request.user})

    return {"error": False}, 200
