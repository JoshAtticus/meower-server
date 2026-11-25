import chat_permissions
from database import db


def resolve_forward(post_id: str, username: str):
    """Details of a post that `username` may forward, or an (error, status) tuple."""
    post = db.posts.find_one({"_id": post_id, "isDeleted": False})
    if not post or post["post_origin"] in ("inbox", "livechat"):
        return None, ({"error": True, "type": "notFound"}, 404)

    origin = post["post_origin"]
    if origin != "home":
        chat = db.chats.find_one({"_id": origin, "members": username, "deleted": False})
        if not chat:
            return None, ({"error": True, "type": "notFound"}, 404)
        if chat["type"] == 0:
            chat = chat_permissions.ensure_roles(chat)
            if chat.get("allow_sharing", True) is False or not chat_permissions.has(chat, username, "share_posts"):
                return None, ({"error": True, "type": "sharingDisabled"}, 403)

    # Forwarding a forward keeps pointing at the original
    inner = post.get("forwarded_from")
    return {
        "post_id": inner["post_id"] if inner else post["_id"],
        "user": inner["user"] if inner else post["u"],
        "origin": inner["origin"] if inner else origin,
        "content": (inner["content"] if inner else post["p"])[:1000],
        "attachments": inner["attachments"] if inner else len(post.get("attachments", [])),
    }, None
