import time, secrets
from typing import Optional
from pymongo.errors import DuplicateKeyError

from database import db

CHANGE_COOLDOWN = 72 * 60 * 60  # one change every 72 hours
RESERVATION_TTL = 30 * 24 * 60 * 60  # old names are held for 30 days


def reservation(lower_name: str) -> Optional[dict]:
    """The active reservation for a (lowercase) name, if any."""
    doc = db.username_reservations.find_one({"_id": lower_name})
    if doc and doc["expires"] > time.time():
        return doc
    return None


def name_blocked_for(lower_name: str, uuid: Optional[str] = None) -> bool:
    """True if the name is held for someone other than `uuid`."""
    held = reservation(lower_name)
    return bool(held and held["uuid"] != uuid)


def cooldown_remaining(account: dict) -> int:
    last = account.get("username_changed_at") or 0
    return max(0, int(last + CHANGE_COOLDOWN - time.time()))


def _move_compound_ids(collection, field_path: str, old: str, new: str):
    """Collections that keep usernames inside a compound _id: re-key them."""
    for doc in list(collection.find({f"_id.{field_path}": old})):
        new_id = dict(doc["_id"])
        new_id[field_path] = new
        doc["_id"] = new_id
        try:
            collection.insert_one(doc)
        except DuplicateKeyError:
            pass
        collection.delete_one({"_id": {**new_id, field_path: old}})


def change_username(old: str, new: str) -> None:
    """
    Rename an account everywhere. Usernames are the account's _id, so the
    user document is copied under the new name and the old one removed, and
    every reference elsewhere is updated.
    """
    now = int(time.time())
    account = db.usersv0.find_one({"_id": old})
    settings = db.user_settings.find_one({"_id": old}) or {"_id": old}

    # tokens and lower_username are uniquely indexed: free them on the old
    # document before the new one takes them over
    db.usersv0.update_one({"_id": old}, {"$set": {
        "tokens": [f"renaming:{secrets.token_urlsafe(24)}"],
        "lower_username": f"renaming:{secrets.token_urlsafe(12)}"
    }})

    new_account = dict(account)
    new_account["_id"] = new
    new_account["lower_username"] = new.lower()
    new_account["username_changed_at"] = now
    try:
        db.usersv0.insert_one(new_account)
    except Exception:
        # put the old account back exactly as it was
        db.usersv0.update_one({"_id": old}, {"$set": {
            "tokens": account["tokens"],
            "lower_username": account["lower_username"]
        }})
        raise

    new_settings = dict(settings)
    new_settings["_id"] = new
    db.user_settings.replace_one({"_id": new}, new_settings, upsert=True)

    # simple references
    db.posts.update_many({"u": old}, {"$set": {"u": new}})
    db.chats.update_many({"owner": old}, {"$set": {"owner": new}})
    db.chats.update_many({"members": old}, {"$set": {"members.$": new}})
    db.chats.update_many(
        {f"member_roles.{old}": {"$exists": True}},
        {"$rename": {f"member_roles.{old}": f"member_roles.{new}"}}
    )
    db.chats.update_many({"dm_request.from": old}, {"$set": {"dm_request.from": new}})
    db.chats.update_many(
        {"bans.username": old},
        {"$set": {"bans.$[b].username": new}},
        array_filters=[{"b.username": old}]
    )
    db.chats.update_many(
        {"bans.banned_by": old},
        {"$set": {"bans.$[b].banned_by": new}},
        array_filters=[{"b.banned_by": old}]
    )
    db.chat_invites.update_many({"from": old}, {"$set": {"from": new}})
    db.chat_invites.update_many({"to": old}, {"$set": {"to": new}})
    db.chat_emojis.update_many({"created_by": old}, {"$set": {"created_by": new}})
    db.chat_stickers.update_many({"created_by": old}, {"$set": {"created_by": new}})
    db.files.update_many({"uploaded_by": old}, {"$set": {"uploaded_by": new}})
    db.authenticators.update_many({"user": old}, {"$set": {"user": new}})
    db.data_exports.update_many({"user": old}, {"$set": {"user": new}})
    db.bots.update_many({"owner": old}, {"$set": {"owner": new}})
    db.audit_log.update_many({"mod_username": old}, {"$set": {"mod_username": new}})
    db.reports.update_many(
        {"reports.user": old},
        {"$set": {"reports.$[r].user": new}},
        array_filters=[{"r.user": old}]
    )
    db.reports.update_many({"type": "user", "content_id": old}, {"$set": {"content_id": new}})

    # compound ids
    _move_compound_ids(db.netlog, "user", old, new)
    _move_compound_ids(db.post_reactions, "user", old, new)
    _move_compound_ids(db.relationships, "from", old, new)
    _move_compound_ids(db.relationships, "to", old, new)

    # admin notes are keyed by username
    note = db.admin_notes.find_one({"_id": old})
    if note:
        note["_id"] = new
        try:
            db.admin_notes.insert_one(note)
            db.admin_notes.delete_one({"_id": old})
        except DuplicateKeyError:
            pass

    # drop the old documents
    db.usersv0.delete_one({"_id": old})
    db.user_settings.delete_one({"_id": old})

    # hold the old name for its owner and let profile links follow the rename
    db.username_reservations.replace_one(
        {"_id": old.lower()},
        {
            "_id": old.lower(),
            "uuid": account["uuid"],
            "old_name": old,
            "expires": now + RESERVATION_TTL
        },
        upsert=True
    )
    # taking a held name back (or changing only its case) releases it
    db.username_reservations.delete_one({"_id": new.lower(), "uuid": account["uuid"]})
