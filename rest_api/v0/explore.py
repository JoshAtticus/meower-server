from quart import Blueprint, current_app as app, request, abort
from quart_schema import validate_querystring
from pydantic import BaseModel, Field
from typing import Optional, Literal

import time, json, hashlib

import security
from database import db, rdb, get_total_pages


explore_bp = Blueprint("explore_bp", __name__, url_prefix="/explore")


class ExploreQueryArgs(BaseModel):
    range: Optional[Literal["week", "month", "year", "all"]] = Field(default="week")
    page: Optional[int] = Field(default=1, ge=1)


RANGES = {"week": 7, "month": 30, "year": 365, "all": None}


@explore_bp.get("/")
@validate_querystring(ExploreQueryArgs)
async def explore(query_args: ExploreQueryArgs):
    # Users the requester has blocked (state 2) are hidden, so the cache is
    # keyed by that list and never shared between people with different blocks
    blocked = []
    if request.user:
        blocked = sorted(rel["_id"]["to"] for rel in db.relationships.find(
            {"_id.from": request.user, "state": 2}, projection={"_id": 1}))
    blocked_key = hashlib.md5(",".join(blocked).encode()).hexdigest()[:12] if blocked else "none"
    cache_key = f"explore:{query_args.range}:{query_args.page}:{blocked_key}"
    cached = rdb.get(cache_key)
    if cached:
        result = json.loads(cached)
        result["autoget"] = [app.supporter.parse_posts_v0(
            [post], requester=request.user)[0] for post in [
            db.posts.find_one({"_id": pid, "isDeleted": False}) for pid in result["ids"]
        ] if post]
        for post, extra in zip(result["autoget"], result["extras"]):
            post.update(extra)
        del result["ids"], result["extras"]
        return result, 200

    query = {"post_origin": "home", "isDeleted": False}
    days = RANGES[query_args.range]
    if days is not None:
        query["t.e"] = {"$gte": int(time.time()) - days*86400}

    if blocked:
        query["u"] = {"$nin": blocked}

    now = time.time()
    posts = list(db.posts.aggregate([
        {"$match": query},
        {"$addFields": {"score": {"$divide": [
            {"$add": [
                {"$multiply": [3, {"$ifNull": ["$like_count", 0]}]},
                {"$multiply": [1.5, {"$ifNull": ["$reaction_count", 0]}]},
                {"$multiply": [4, {"$ifNull": ["$reply_count", 0]}]},
                1
            ]},
            {"$pow": [
                {"$add": [
                    {"$divide": [{"$subtract": [now, {"$ifNull": ["$t.e", now]}]}, 86400]},
                    2
                ]}, 0.5
            ]}
        ]}}},
        {"$sort": {"score": -1, "_id": -1}},
        {"$skip": (query_args.page-1)*25},
        {"$limit": 25}
    ]))

    extras = [{
        "like_count": post.get("like_count", 0),
        "reaction_count": post.get("reaction_count", 0),
        "reply_count": post.get("reply_count", 0),
        "score": round(post["score"], 2)
    } for post in posts]

    rdb.setex(cache_key, 60, json.dumps({
        "error": False,
        "ids": [post["_id"] for post in posts],
        "extras": extras,
        "page#": query_args.page,
        "pages": get_total_pages("posts", query)
    }))

    autoget = app.supporter.parse_posts_v0(posts, requester=request.user)
    for post, extra in zip(autoget, extras):
        post.update(extra)

    return {
        "error": False,
        "autoget": autoget,
        "page#": query_args.page,
        "pages": get_total_pages("posts", query)
    }, 200
