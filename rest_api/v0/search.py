from quart import Blueprint, current_app as app, request
from quart_schema import validate_querystring
from pydantic import BaseModel, Field
from typing import Optional, Literal

import re, math, time
from quart import abort

import security
from database import db, get_total_pages


search_bp = Blueprint("search_bp", __name__, url_prefix="/search")


class SearchQueryArgs(BaseModel):
    q: str = Field(min_length=1, max_length=4000)
    page: Optional[int] = Field(default=1, ge=1)


@search_bp.get("/home")
@validate_querystring(SearchQueryArgs)
async def search_home(query_args: SearchQueryArgs):
    query = {"post_origin": "home", "isDeleted": False, "$text": {"$search": query_args.q}}
    return {
        "error": False,
        "autoget": app.supporter.parse_posts_v0(db.posts.find(
            query,
            skip=(query_args.page-1)*25,
            limit=25
        ), requester=request.user),
        "page#": query_args.page,
        "pages": get_total_pages("posts", query)
    }, 200


@search_bp.get("/users")
@validate_querystring(SearchQueryArgs)
async def search_users(query_args: SearchQueryArgs):
    # Get users
    query = {"pswd": {"$type": "string"}, "$text": {"$search": query_args.q}}
    usernames = [user["_id"] for user in db.usersv0.find(query, skip=(query_args.page-1)*25, limit=25, projection={"_id": 1})]

    # Return users
    return {
        "error": False,
        "autoget": [security.get_account(username) for username in usernames],
        "page#": query_args.page,
        "pages": get_total_pages("usersv0", query)
    }, 200


class UnifiedSearchQueryArgs(BaseModel):
    q: str = Field(min_length=1, max_length=200)
    page: Optional[int] = Field(default=1, ge=1)
    type: Optional[Literal["all", "posts", "users", "chats"]] = Field(default="all")
    chat_id: Optional[str] = Field(default=None, max_length=64)  # only search posts in this chat


def _member_chat_ids(username):
    if not username:
        return []
    return [chat["_id"] for chat in db.chats.find(
        {"members": username, "deleted": False}, projection={"_id": 1})]


@search_bp.get("/")
@validate_querystring(UnifiedSearchQueryArgs)
async def unified_search(query_args: UnifiedSearchQueryArgs):
    if security.ratelimited(f"search:{request.ip}"):
        abort(429)
    security.ratelimit(f"search:{request.ip}", 30, 30)

    q = query_args.q.strip()
    q_re = re.escape(q)
    lower_q = q.lower()
    limit = query_args.page * 25
    users, posts, chats = [], [], []

    # Searching inside one chat: members only, posts only
    only_chat = None
    if query_args.chat_id:
        if not request.user or not db.chats.count_documents({
            "_id": query_args.chat_id, "members": request.user, "deleted": False
        }, limit=1):
            abort(404)
        only_chat = query_args.chat_id
        query_args.type = "posts"

    if query_args.type in ("all", "users"):
        # Users: exact > username prefix > display_name prefix > username
        # contains > display_name contains
        seen = set()
        stages = [
            ({"lower_username": lower_q}, 100),
            ({"lower_username": {"$regex": "^" + q_re.lower()}}, 60),
            ({"display_name": {"$regex": "^" + q_re, "$options": "i"}}, 50),
            ({"lower_username": {"$regex": q_re.lower()}}, 35),
            ({"display_name": {"$regex": q_re, "$options": "i"}}, 30),
        ]
        for query, score in stages:
            query["pswd"] = {"$type": "string"}
            for user in db.usersv0.find(query, projection={"_id": 1},
                                        limit=limit).max_time_ms(1500):
                if user["_id"] not in seen:
                    seen.add(user["_id"])
                    users.append({"type": "user", "score": float(score),
                                  "user": security.get_account(user["_id"])})
                if len(users) >= limit:
                    break
            if len(users) >= limit:
                break

    if query_args.type in ("all", "posts"):
        member_chats = _member_chat_ids(request.user)
        if only_chat:
            member_chats = [only_chat]
        # $text only covers Home posts (partial text index); member chats go
        # through the regex path below
        text_hits = [] if only_chat else list(db.posts.find(
            {"post_origin": "home", "isDeleted": False,
             "$text": {"$search": q}},
            {"score": {"$meta": "textScore"}}
        ).sort([("score", {"$meta": "textScore"})]).limit(limit).max_time_ms(1500))
        for post in text_hits:
            posts.append({
                "type": "post",
                "score": post.get("score", 0)*10
                    + 2*math.log1p(post.get("like_count", 0))
                    + math.log1p(post.get("reply_count", 0)),
                "post": post,
                "_created": post["t"]["e"]
            })
        if only_chat or len(text_hits) < 5 or member_chats:
            # Partial-word / short-term fallback (and the only path that can
            # reach member-chat posts): escaped contains-regex on Home plus
            # chats the requester is a member of
            now = time.time()
            regex_hits = list(db.posts.find({
                "post_origin": {"$in": member_chats if only_chat else ["home"] + member_chats},
                "isDeleted": False,
                "p": {"$regex": q_re, "$options": "i"}
            }).sort("t.e", -1).limit(50).max_time_ms(1500))
            existing = {post["post"]["_id"] for post in posts}
            for post in regex_hits:
                if post["_id"] in existing:
                    continue
                age_days = max(0, (now - post["t"]["e"]) / 86400)
                posts.append({
                    "type": "post",
                    "score": 5 - min(age_days, 30)*0.01,
                    "post": post,
                    "_created": post["t"]["e"]
                })

    if query_args.type in ("all", "chats") and request.user:
        prefix = db.chats.find({
            "type": 0, "members": request.user, "deleted": False,
            "nickname": {"$regex": "^" + q_re, "$options": "i"}
        }, limit=limit).max_time_ms(1500)
        seen_chats = set()
        for chat in prefix:
            seen_chats.add(chat["_id"])
            chats.append({"type": "chat", "score": 40.0, "chat": _chat_search_payload(chat)})
        for chat in db.chats.find({
            "type": 0, "members": request.user, "deleted": False,
            "nickname": {"$regex": q_re, "$options": "i"}
        }, limit=limit).max_time_ms(1500):
            if chat["_id"] not in seen_chats:
                chats.append({"type": "chat", "score": 25.0, "chat": _chat_search_payload(chat)})

    counts = {
        "users": len(users) if query_args.type in ("all", "users") else 0,
        "posts": len(posts) if query_args.type in ("all", "posts") else 0,
        "chats": len(chats) if query_args.type in ("all", "chats") else 0
    }

    type_order = {"user": 0, "chat": 1, "post": 2}
    items = users + posts + chats
    items.sort(key=lambda item: (
        -item["score"], type_order[item["type"]],
        -item.get("_created", 0)))

    merged = []
    chat_names = {}
    for item in items[(query_args.page-1)*25:query_args.page*25]:
        item.pop("_created", None)
        if item["type"] == "post":
            origin_id = item["post"]["post_origin"]
            if origin_id not in chat_names:
                if origin_id == "home":
                    chat_names[origin_id] = {"id": "home", "name": "Home"}
                else:
                    chat = db.chats.find_one({"_id": origin_id}, projection={"nickname": 1, "type": 1, "icon": 1, "icon_color": 1})
                    is_group = bool(chat and chat.get("type") == 0)
                    chat_names[origin_id] = {
                        "id": origin_id,
                        "name": (chat.get("nickname") if is_group else None) or "Direct messages",
                        "icon": chat.get("icon", "") if is_group else "",
                        "icon_color": chat.get("icon_color", "") if is_group else "",
                        "type": chat.get("type", 0) if chat else 0
                    }
            item["origin"] = chat_names[origin_id]
            item["post"] = app.supporter.parse_posts_v0(
                [item["post"]], requester=request.user)[0]
        merged.append(item)

    return {
        "error": False,
        "autoget": merged,
        "counts": counts,
        "page#": query_args.page,
        "pages": max(1, math.ceil(len(items) / 25))
    }, 200


def _chat_search_payload(chat):
    return {
        "_id": chat["_id"],
        "nickname": chat.get("nickname", ""),
        "icon": chat.get("icon", ""),
        "icon_color": chat.get("icon_color", ""),
        "members": len(chat.get("members", [])),
        "mode": chat.get("mode", "chat")
    }
