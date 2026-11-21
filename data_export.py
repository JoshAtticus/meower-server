"""
Builds account data exports in-process (the original standalone data
export service isn't part of this repo).
"""
from threading import Thread
import io, json, os, time, zipfile

import security
from database import db
from utils import log

EXPORTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data_exports")
EXPORT_TTL = 604800  # completed exports can be downloaded for 7 days


def export_path(export_id: str) -> str:
    return os.path.join(EXPORTS_DIR, f"{export_id}.zip")


def _dump(obj) -> str:
    return json.dumps(obj, indent=2, default=str, ensure_ascii=False)


def build_export(username: str) -> bytes:
    account = db.usersv0.find_one({"_id": username}, projection=security.SENSITIVE_ACCOUNT_FIELDS_DB_PROJECTION) or {}
    files = {
        "account.json": account,
        "settings.json": db.user_settings.find_one({"_id": username}) or {},
        "posts.json": list(db.posts.find({"u": username}).sort("t.e", 1)),
        "post_revisions.json": list(db.post_revisions.find({
            "post_id": {"$in": [p["_id"] for p in db.posts.find({"u": username}, projection={"_id": 1})]}
        })),
        "chats.json": list(db.chats.find({"members": username}, projection={
            "_id": 1, "type": 1, "nickname": 1, "owner": 1, "members": 1, "created": 1
        })),
        "relationships.json": list(db.relationships.find({"$or": [
            {"_id.from": username}, {"_id.to": username}
        ]})),
        "reactions.json": list(db.post_reactions.find({"_id.user": username})),
        "reports_filed.json": [
            {
                "content_id": r.get("content_id"),
                "type": r.get("type"),
                "status": r.get("status"),
                **next((x for x in r.get("reports", []) if x.get("user") == username), {})
            }
            for r in db.reports.find({"reports.user": username})
        ],
        "authenticators.json": list(db.authenticators.find({"user": username}, projection={
            "_id": 1, "type": 1, "nickname": 1, "registered_at": 1
        })),
        "chat_invites.json": list(db.chat_invites.find({"$or": [{"to": username}, {"from": username}]})),
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, _dump(data))
        zf.writestr("README.txt", f"Meower data export for @{username}\nCreated {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}\n")
    return buf.getvalue()


def _process(export_id: str, username: str):
    try:
        os.makedirs(EXPORTS_DIR, exist_ok=True)
        with open(export_path(export_id), "wb") as f:
            f.write(build_export(username))
        db.data_exports.update_one({"_id": export_id}, {"$set": {
            "status": "completed",
            "completed_at": int(time.time())
        }})
    except Exception as e:
        log(f"Data export {export_id} for {username} failed: {e}")
        db.data_exports.update_one({"_id": export_id}, {"$set": {
            "status": "failed",
            "error": str(e),
            "completed_at": int(time.time())
        }})


def start_export(export_id: str, username: str):
    Thread(target=_process, args=(export_id, username), daemon=True).start()


def cleanup_expired():
    """Delete expired export zips and mark them expired."""
    cutoff = int(time.time()) - EXPORT_TTL
    for export in db.data_exports.find({
        "status": "completed",
        "completed_at": {"$lte": cutoff}
    }, projection={"_id": 1}):
        try:
            if os.path.exists(export_path(export["_id"])):
                os.remove(export_path(export["_id"]))
            db.data_exports.update_one({"_id": export["_id"]}, {"$set": {"status": "expired"}})
        except Exception as e:
            log(f"Failed to clean up data export {export['_id']}: {e}")


def cleanup_loop():
    while True:
        try:
            cleanup_expired()
        except Exception as e:
            log(f"Data export cleanup failed: {e}")
        time.sleep(3600)
