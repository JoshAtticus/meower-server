import pymongo
import redis
import os
import secrets
from radix import Radix

from utils import log

CURRENT_DB_VERSION = 10

# Create Redis connection
log("Connecting to Redis...")
try:
    rdb = redis.from_url(os.getenv("REDIS_URI", "redis://127.0.0.1:6379/0"))
except Exception as e:
    log(f"Failed to connect to database! Error: {e}")
    exit()
else:
    log("Successfully connected to Redis!")


# Create database connection
log("Connecting to database...")
try:
    db = pymongo.MongoClient(os.getenv("MONGO_URI", "mongodb://127.0.0.1:27017"))[os.getenv("MONGO_DB", "meowerserver")]
    db.command("ping")
except Exception as e:
    log(f"Failed to connect to database! Error: {e}")
    exit()
else:
    log("Successfully connected to database!")


# Create database collections
existing_collections = db.list_collection_names()
for collection_name in []:
    if collection_name not in existing_collections:
        log(f"Creating {collection_name} database collection...")
        db.create_collection(collection_name)

# Create usersv0 indexes
try: db.usersv0.create_index([("lower_username", pymongo.ASCENDING)], name="lower_username", unique=True)
except: pass
try: db.usersv0.create_index([("tokens", pymongo.ASCENDING)], name="tokens", unique=True)
except: pass
try: db.usersv0.create_index([("created", pymongo.DESCENDING)], name="recent_users")
except: pass
try:
    db.usersv0.create_index([
        ("lower_username", pymongo.TEXT),
        ("quote", pymongo.TEXT)
    ], name="search", partialFilterExpression={"pswd": {"$type": "string"}})
except: pass
try: db.usersv0.create_index([
        ("delete_after", pymongo.ASCENDING)
    ], name="scheduled_deletions", partialFilterExpression={"delete_after": {"$type": "number"}})
except: pass

# Create authenticators indexes
try: db.authenticators.create_index([("user", pymongo.ASCENDING)], name="user")
except: pass

# Create data exports indexes
try: db.data_exports.create_index([("user", pymongo.ASCENDING)], name="user")
except: pass

# Create relationships indexes
try: db.relationships.create_index([("_id.from", pymongo.ASCENDING)], name="from")
except: pass

# Create netinfo indexes
try: db.netinfo.create_index([("last_refreshed", pymongo.ASCENDING)], name="last_refreshed")
except: pass

# Create netlog indexes
try: db.netlog.create_index([("_id.ip", pymongo.ASCENDING)], name="ip")
except: pass
try: db.netlog.create_index([("_id.user", pymongo.ASCENDING)], name="user")
except: pass
try: db.netlog.create_index([("last_used", pymongo.ASCENDING)], name="last_used")
except: pass

# Create posts indexes
try:
    db.posts.create_index([
        ("post_origin", pymongo.ASCENDING),
        ("isDeleted", pymongo.ASCENDING),
        ("t.e", pymongo.DESCENDING)
    ], name="default")
except: pass
try:
    db.posts.create_index([
        ("u", pymongo.ASCENDING),
        ("post_origin", pymongo.ASCENDING),
        ("t.e", pymongo.DESCENDING)
    ], name="user")
except: pass
try:
    db.posts.create_index([
        ("p", pymongo.TEXT)
    ], name="search", partialFilterExpression={"post_origin": "home", "isDeleted": False})
except: pass

try:
    db.posts.create_index([
        ("deleted_at", pymongo.ASCENDING)
    ], name="scheduled_purges", partialFilterExpression={"isDeleted": True, "mod_deleted": False})
except: pass

try:
    db.posts.create_index([
        ("post_origin", pymongo.ASCENDING),
        ("pinned", pymongo.ASCENDING),
        ("t.e", pymongo.DESCENDING)
    ], name="pinned_posts", partialFilterExpression={"pinned": True})
except: pass

try:
    db.posts.create_index([("reply_to", pymongo.ASCENDING)], name="reply_to")
except: pass
try:
    db.posts.create_index([
        ("post_origin", pymongo.ASCENDING),
        ("t.e", pymongo.DESCENDING)
    ], name="origin_time")
except: pass
try:
    db.posts.create_index([
        ("post_origin", pymongo.ASCENDING),
        ("isDeleted", pymongo.ASCENDING),
        ("like_count", pymongo.DESCENDING)
    ], name="explore")
except: pass

# Create post revisions indexes
try:
    db.post_revisions.create_index([
        ("post_id", pymongo.ASCENDING),
        ("time", pymongo.DESCENDING)
    ], name="post_revisions")
except: pass
try:
    db.post_revisions.create_index([
        ("time", pymongo.ASCENDING)
    ], name="scheduled_purges")
except: pass

# Create chats indexes
try:
    db.chats.create_index([
        ("members", pymongo.ASCENDING),
        ("type", pymongo.ASCENDING),
    ], name="user_chats")
except: pass

# Create chat_emojis indexes
try:
    db.chat_emojis.create_index([
        ("chat_id", pymongo.ASCENDING),
    ], name="chat_id")
except: pass

# Create chat_stickers indexes
try:
    db.chat_stickers.create_index([
        ("chat_id", pymongo.ASCENDING),
    ], name="chat_id")
except: pass

# Create chat_invites indexes
try:
    db.chat_invites.create_index([("to", pymongo.ASCENDING)], name="to")
except: pass
try:
    db.chat_invites.create_index([("chat_id", pymongo.ASCENDING)], name="chat_id")
except: pass

# Create chat_invite_links indexes
try:
    db.chat_invite_links.create_index([("chat_id", pymongo.ASCENDING)], name="chat_id")
except: pass

# Create reports indexes
try:
    db.reports.create_index([
        ("content_id", pymongo.ASCENDING)
    ], name="pending_reports", partialFilterExpression={"status": "pending"})
except: pass
try:
    db.reports.create_index([
        ("escalated", pymongo.DESCENDING),
        ("reports.time", pymongo.DESCENDING),
        ("status", pymongo.ASCENDING),
        ("type", pymongo.ASCENDING)
    ], name="all_reports")
except: pass

# Create audit log indexes
try:
    db.audit_log.create_index([
        ("time", pymongo.ASCENDING),
        ("type", pymongo.ASCENDING)
    ], name="scheduled_purges")
except: pass

# Create post reactions index
try:
    db.post_reactions.create_index([("_id.post_id", pymongo.ASCENDING), ("_id.emoji", pymongo.ASCENDING)])
except: pass

# Create files indexes
try:
    db.files.create_index([("hash", pymongo.ASCENDING)], name="hash")
except: pass
try:
    db.files.create_index([("uploaded_by", pymongo.ASCENDING)], name="uploaded_by")
except: pass
try:
    db.files.create_index([
        ("claimed", pymongo.ASCENDING),
        ("uploaded_by", pymongo.ASCENDING)
    ], name="unclaimed", partialFilterExpression={"claimed": False})
except: pass


# Create default database items
for username in ["Server", "Deleted", "Meower", "Admin", "username"]:
    try:
        db.usersv0.insert_one({
            "_id": username,
            "lower_username": username.lower(),
            "uuid": None,
            "created": None,
            "pfp_data": None,
            "avatar": None,
            "avatar_color": None,
            "quote": None,
            "pswd": None,
            "tokens": None,
            "flags": 1,
            "permissions": None,
            "ban": None,
            "last_seen": None,
            "delete_after": None
        })
    except: pass
try:
    db.config.insert_one({
        "_id": "migration",
        "database": 1
    })
except: pass
try:
    db.config.insert_one({
        "_id": "status",
        "repair_mode": False,
        "registration": True
    })
except: pass


# Load netblocks
blocked_ips = Radix()
registration_blocked_ips = Radix()
for netblock in db.netblock.find({}):
    try:
        if netblock["type"] == 0:
            blocked_ips.add(netblock["_id"])
        if netblock["type"] == 1:
            registration_blocked_ips.add(netblock["_id"])
    except Exception as e:
        log(f"Failed to load netblock {netblock['_id']}: {e}")
log(f"Successfully loaded {len(blocked_ips.nodes())} netblock(s) into Radix!")
log(f"Successfully loaded {len(registration_blocked_ips.nodes())} registration netblock(s) into Radix!")


def get_total_pages(collection: str, query: dict, page_size: int = 25) -> int:
    item_count = db[collection].count_documents(query)
    pages = (item_count // page_size)
    if (item_count % page_size) > 0:
        pages += 1
    return pages

if db.config.find_one({"_id": "migration", "database": {"$ne": CURRENT_DB_VERSION}}):
    log(f"[Migrator] Migrating DB to version {CURRENT_DB_VERSION}. ")
    log(f"[Migrator] Please do not shut the server down until it is done.")

    # Chat pinning
    log("[Migrator] Adding pinned messages to database")
    db.posts.update_many({"pinned": {"$exists": False}}, {"$set": {"pinned": False}})

    log("[Migrator] Adding perm for pinning posts")
    db.chats.update_many({"allow_pinning": {"$exists": False}}, {"$set": {"allow_pinning": False}})

    # Experiments
    log("[Migrator] Removing experiments from database")
    db.usersv0.update_many({"experiments": {"$exists": True}}, {"$unset": {"experiments": ""}})

    # Custom profile pictures
    log("[Migrator] Adding custom profile pictures to database")
    db.usersv0.update_many({"pswd": {"$ne": None}, "avatar": {"$exists": False}}, {"$set": {"avatar": ""}})
    db.usersv0.update_many({"pswd": {"$ne": None}, "avatar_color": {"$exists": False}}, {"$set": {"avatar_color": "000000"}})

    # Chat icons
    log("[Migrator] Adding chat icons to database")
    db.chats.update_many({"icon": {"$exists": False}}, {"$set": {"icon": ""}})
    db.chats.update_many({"icon_color": {"$exists": False}}, {"$set": {"icon_color": "000000"}})

    # Post attachments
    log("[Migrator] Adding post attachments to database")
    db.posts.update_many({"attachments": {"$exists": False}}, {"$set": {"attachments": []}})

    # Profanity filter
    log("[Migrator] Removing profanity filter")
    db.config.delete_one({"_id": "filter"})
    db.posts.update_many({"unfiltered_p": {"$exists": True}}, [{"$set": {"p": "$unfiltered_p"}}])
    db.posts.update_many({"unfiltered_p": {"$exists": True}}, {"$unset": {"unfiltered_p": ""}})

    # MFA recovery codes
    log("[Migrator] Adding MFA recovery codes")
    for user in db.usersv0.find({"pswd": {"$ne": None}, "mfa_recovery_code": {"$exists": False}}, projection={"_id": 1}):
        db.usersv0.update_one({"_id": user["_id"]}, {"$set": {
            "mfa_recovery_code": secrets.token_hex(5)
        }})
    
    # Post reactions
    log("[Migrator] Adding post reactions to database")
    db.posts.update_many({"reactions": {"$exists": False}}, {"$set": {"reactions": []}})

    # Remove type and post_id fields in posts database
    log("[Migrator] Removing type and post_id fields from posts database")
    db.posts.update_many({}, {"$unset": {"type": "", "post_id": ""}})

    # Post replies
    log("[Migrator] Adding post replies to database")
    db.posts.update_many({"reply_to": {"$exists": False}}, {"$set": {"reply_to": []}})

    # Fix MFA recovery codes
    log("[Migrator] Fixing MFA recovery codes")
    for user in db.usersv0.aggregate([
        {"$match": {"mfa_recovery_code": {"$ne": None}}},
        {"$project": {
            "mfa_recovery_code": 1,
            "length": {"$strLenCP": "$mfa_recovery_code"}
        }},
        {"$match": {
            "length": {"$gt": 10}
        }}
    ]):
        db.usersv0.update_one({"_id": user["_id"]}, {"$set": {
            "mfa_recovery_code": user["mfa_recovery_code"][:10]
        }})

    # Post attachments
    log("[Migrator] Converting post attachments")
    updates = []
    for post in db.posts.find({"attachments.id": {"$exists": True}}):
        updates.append(pymongo.UpdateOne(
            {"_id": post["_id"]},
            {"$set": {"attachments": [attachment["id"] for attachment in post["attachments"]]}}
        ))
    if len(updates):
        db.posts.bulk_write(updates)

    db.config.update_one({"_id": "migration"}, {"$set": {"database": CURRENT_DB_VERSION}})
    log(f"[Migrator] Finished Migrating DB to version {CURRENT_DB_VERSION}")

# Group chat roles (idempotent, runs on every startup)
import chat_permissions
for chat in db.chats.find({"type": 0, "roles": {"$exists": False}}, projection={"_id": 1}):
    log(f"[Migrator] Adding roles to group chat {chat['_id']}")
    chat_permissions.ensure_roles(db.chats.find_one({"_id": chat["_id"]}))

# Custom emojis: posts edited before edits refreshed their emoji list
import re as _re
_EMOJI_RE = r"<:([a-zA-Z0-9]{24})>"  # same as supporter.CUSTOM_EMOJI_REGEX
for _post in db.posts.find({"p": {"$regex": "<:"}, "emojis": {"$size": 0}}, projection={"p": 1}):
    _found = list(set(_re.findall(_EMOJI_RE, _post["p"])))
    if _found:
        db.posts.update_one({"_id": _post["_id"]}, {"$set": {"emojis": _found}})

# The Profile theme replaced the profile_theme toggle
db.user_settings.update_many({"profile_theme": True}, {"$set": {"theme": "profile", "profile_theme": False}})

# Reactions permission: grant it to the Default role once per existing chat
# (the marker keeps later edits to the permission from being overwritten)
for chat in db.chats.find({"type": 0, "roles": {"$exists": True}, "reactions_perm_migrated": {"$ne": True}}, projection={"_id": 1}):
    log(f"[Migrator] Adding reactions permission to group chat {chat['_id']}")
    db.chats.update_one(
        {"_id": chat["_id"], "roles._id": "default"},
        {"$set": {"roles.$.permissions.add_reactions": True}}
    )
    db.chats.update_one({"_id": chat["_id"]}, {"$set": {"reactions_perm_migrated": True}})

# Sharing perms + settings: grant share_posts/react/edit_messages to the
# Default role (unless already explicitly set) and default the sharing
# toggles on for every existing group chat
for chat in db.chats.find(
        {"type": 0, "roles": {"$exists": True}, "perms_v2_migrated": {"$ne": True}},
        projection={"_id": 1, "roles": 1}):
    log(f"[Migrator] Adding sharing permissions to group chat {chat['_id']}")
    default_idx = next(
        (i for i, r in enumerate(chat["roles"]) if r["_id"] == "default"), None)
    set_fields = {
        "perms_v2_migrated": True,
        "allow_sharing": True,
        "share_context": False
    }
    if default_idx is not None:
        perms = chat["roles"][default_idx].get("permissions", {})
        for perm in ("share_posts", "react", "edit_messages"):
            if perm not in perms:
                set_fields[f"roles.{default_idx}.permissions.{perm}"] = True
    db.chats.update_one({"_id": chat["_id"]}, {"$set": set_fields})


# Inbox categories: infer a category for existing inbox posts
if not db.config.find_one({"_id": "inbox_categories_migrated"}):
    def _inbox_category(post):
        p = post.get("p", "").lower()
        if post.get("u") == "Server":
            return "announcement"
        if "welcome to meower" in p or "scheduled for deletion" in p:
            return "account"
        if "friend request" in p or "now friends" in p:
            return "friends"
        if "password" in p or "multi-factor" in p or "recovery code" in p:
            return "security"
        if "group chat" in p:
            return "chats"
        return None
    for _post in db.posts.find({"post_origin": "inbox", "inbox_category": {"$exists": False}}, projection={"p": 1, "u": 1}):
        _cat = _inbox_category(_post)
        if _cat:
            db.posts.update_one({"_id": _post["_id"]}, {"$set": {"inbox_category": _cat}})
    db.config.update_one({"_id": "inbox_categories_migrated"}, {"$set": {"done": True}}, upsert=True)

# Backfill like_count / reaction_count / reply_count on posts
if not db.config.find_one({"_id": "post_counters_migrated"}):
    log("Migrating post counters (likes, reactions, replies)...")
    counters = {}  # post_id -> [likes, reactions, replies]
    for reaction in db.post_reactions.find({}, projection={"_id": 1}):
        rid = reaction["_id"]
        c = counters.setdefault(rid["post_id"], [0, 0, 0])
        if rid["emoji"] in ("❤️", "❤"):
            c[0] += 1
        else:
            c[1] += 1
    for reply in db.posts.find({"isDeleted": False, "reply_to.0": {"$exists": True}},
                               projection={"reply_to": 1}):
        for parent_id in reply["reply_to"]:
            counters.setdefault(parent_id, [0, 0, 0])[2] += 1
    for post_id, (likes, reactions, replies) in counters.items():
        db.posts.update_one({"_id": post_id}, {"$set": {
            "like_count": likes,
            "reaction_count": reactions,
            "reply_count": replies
        }})
    db.posts.update_many(
        {"_id": {"$nin": list(counters.keys())},
         "$or": [{"like_count": {"$exists": False}},
                 {"reaction_count": {"$exists": False}},
                 {"reply_count": {"$exists": False}}]},
        {"$set": {"like_count": 0, "reaction_count": 0, "reply_count": 0}})
    db.config.update_one({"_id": "post_counters_migrated"}, {"$set": {"done": True}}, upsert=True)

# Set mode:"chat" on existing chats
if not db.config.find_one({"_id": "chat_mode_migrated"}):
    db.chats.update_many({"mode": {"$exists": False}}, {"$set": {"mode": "chat"}})
    db.config.update_one({"_id": "chat_mode_migrated"}, {"$set": {"done": True}}, upsert=True)

# Grant reply_to_posts on the default role of existing group chats
if not db.config.find_one({"_id": "reply_to_posts_migrated"}):
    db.chats.update_many(
        {"type": 0, "roles._id": "default"},
        {"$set": {"roles.$.permissions.reply_to_posts": True}})
    db.config.update_one({"_id": "reply_to_posts_migrated"}, {"$set": {"done": True}}, upsert=True)

print("") # finished startup logs
