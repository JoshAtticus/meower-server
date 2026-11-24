import time
from database import db


class FileNotFoundError(Exception): pass
class FileAlreadyClaimedError(Exception): pass


# Post attachments may only be images or videos. SVG is excluded (it can
# carry scripts).
_MEDIA_EXTENSIONS = {
    "png", "jpg", "jpeg", "gif", "webp", "avif", "bmp",
    "mp4", "webm", "mov", "m4v",
}

def file_is_media(file: dict) -> bool:
    mime = (file or {}).get("mime") or ""
    if mime:
        return (mime.startswith("image/") or mime.startswith("video/")) \
            and mime != "image/svg+xml"
    filename = (file or {}).get("filename") or ""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return ext in _MEDIA_EXTENSIONS

def claim_file(file_id: str, bucket: str, uploader: str):
    # Find file
    file = db.files.find_one()
    if not file:
        raise FileNotFoundError

    result = db.files.update_one({
        "_id": file_id,
        "bucket": bucket,
        "uploaded_by": uploader,
        "claimed": False
    }, {"$set": {"claimed": True}})
    if result.matched_count == 0:
        raise FileNotFoundError
    elif result.modified_count == 0:
        raise FileAlreadyClaimedError

def unclaim_file(file_id: str, keep_days: int = 7):
    # Keep deleted-post uploads for 7 days for moderation before expiry
    db.files.update_one(
        {"_id": file_id},
        {"$set": {"claimed": False, "uploaded_at": int(time.time()) + keep_days * 86400}}
    )

def expire_file(file_id: str, ttl_seconds: int = 86400):
    # The uploads service deletes unclaimed files 30 minutes after uploaded_at,
    # so push uploaded_at forward to land the deletion ttl_seconds from now
    db.files.update_one(
        {"_id": file_id},
        {"$set": {"claimed": False, "uploaded_at": int(time.time()) + ttl_seconds - 1800}}
    )

def unclaim_all_files(username: str):
    db.files.update_many(
        {"uploaded_by": username},
        {"$set": {"claimed": False, "uploaded_at": 0}}
    )
