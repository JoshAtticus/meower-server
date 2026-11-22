from uuid import uuid4

from database import db

# Permission key -> minimum tier a role must have to be granted it
PERMISSIONS = {
    "send_messages": 0,
    "delete_own_messages": 0,
    "invite_members": 0,
    "add_emotes": 0,
    "edit_emotes": 0,
    "delete_emotes": 0,
    "change_color": 0,
    "change_name": 0,
    "change_icon": 0,
    "delete_icon": 0,
    "pin_messages": 0,
    "unpin_messages": 0,
    "add_reactions": 0,
    "share_posts": 0,
    "react": 0,
    "edit_messages": 0,
    "delete_others_messages": 1,
    "remove_members": 1,
    "ban_members": 2,
    "modify_roles": 2,
    "assign_roles": 2,
    "change_settings": 3,
    "create_roles": 3,
    "delete_roles": 3,
    "toggle_invites": 3,
}
OWNER_RANK = 5
ADMIN_TIER = 4

# (id, name, tier, granted permissions)
PRESET_ROLES = [
    ("default", "Default", 0, ["send_messages", "delete_own_messages", "invite_members", "add_reactions", "share_posts", "react", "edit_messages"]),
    ("stickerer", "Stickerer", 0, ["add_emotes", "edit_emotes", "delete_emotes"]),
    ("customiser", "Customiser", 0, ["change_color", "change_name", "change_icon", "delete_icon"]),
    ("pinner", "Pinner", 0, ["pin_messages", "unpin_messages"]),
    ("minimod", "Minimod", 1, ["delete_others_messages", "remove_members"]),
    ("moderator", "Moderator", 2, ["ban_members", "modify_roles", "assign_roles"]),
    ("higher_moderator", "Higher Moderator", 3, ["change_settings", "create_roles", "delete_roles", "toggle_invites"]),
    ("administrator", "Administrator", 4, []),
]


def default_roles() -> list:
    return [{
        "_id": role_id,
        "name": name,
        "tier": tier,
        "permissions": {perm: True for perm in perms},
        "preset": True
    } for role_id, name, tier, perms in PRESET_ROLES]


def new_role_id() -> str:
    return str(uuid4())


def _roles_by_id(chat: dict) -> dict:
    return {role["_id"]: role for role in chat.get("roles", [])}


def role_effective(chat: dict, role: dict) -> set:
    if role["tier"] >= ADMIN_TIER:
        return set(PERMISSIONS)
    if role["tier"] < 0:
        # Cosmetic roles grant no permissions
        return set()
    effective = set()
    # Inherit explicit allows from every lower-tier role (never cosmetic ones)
    for other in chat.get("roles", []):
        if 0 <= other["tier"] < role["tier"]:
            effective.update(perm for perm, val in other["permissions"].items() if val is True)
    # Apply this role's own overrides
    effective.update(perm for perm, val in role["permissions"].items() if val is True)
    effective.difference_update(perm for perm, val in role["permissions"].items() if val is False)
    return effective


def member_permissions(chat: dict, username: str) -> set:
    if username == chat.get("owner"):
        return set(PERMISSIONS)
    if username not in chat.get("members", []):
        return set()
    roles = _roles_by_id(chat)
    perms = set()
    for role_id in chat.get("member_roles", {}).get(username, []):
        role = roles.get(role_id)
        if role:
            perms.update(role_effective(chat, role))
    return perms


def has(chat: dict, username: str, perm: str) -> bool:
    return perm in member_permissions(chat, username)


def rank(chat: dict, username: str) -> int:
    if username == chat.get("owner"):
        return OWNER_RANK
    roles = _roles_by_id(chat)
    tiers = [
        roles[role_id]["tier"]
        for role_id in chat.get("member_roles", {}).get(username, [])
        if role_id in roles
    ]
    return max(tiers) if tiers else -1


def outranks(chat: dict, actor: str, target: str) -> bool:
    return rank(chat, actor) > rank(chat, target)


def can_manage_role(chat: dict, actor: str, role: dict) -> bool:
    if role["_id"] == "administrator":
        return False
    if chat.get("owner") == actor or rank(chat, actor) >= ADMIN_TIER:
        return True
    return role["tier"] < rank(chat, actor)


def can_grant_perm(chat: dict, actor: str, perm: str) -> bool:
    if chat.get("owner") == actor or rank(chat, actor) >= ADMIN_TIER:
        return True
    return PERMISSIONS[perm] < rank(chat, actor)


def ensure_roles(chat: dict) -> dict:
    if chat.get("type") != 0 or "roles" in chat:
        return chat

    chat["roles"] = default_roles()
    allow_pinning = chat.get("allow_pinning", False)
    member_roles = {}
    for member in chat.get("members", []):
        roles = ["default"]
        if member == chat.get("owner"):
            roles.append("administrator")
        elif allow_pinning:
            roles.append("pinner")
        member_roles[member] = roles
    chat["member_roles"] = member_roles
    chat["invites_enabled"] = True
    chat["allow_sharing"] = True
    chat["share_context"] = True
    chat.pop("allow_pinning", None)

    db.chats.update_one({"_id": chat["_id"]}, {
        "$set": {
            "roles": chat["roles"],
            "member_roles": member_roles,
            "invites_enabled": True,
            "allow_sharing": True,
            "share_context": True,
            "reactions_perm_migrated": True,
            "perms_v2_migrated": True
        },
        "$unset": {"allow_pinning": ""}
    })
    return chat


# Chat purposes chosen at creation tweak the preset roles / settings
CHAT_PURPOSES = ["friends", "public", "private", "announcements", "emojis", "other"]


def purpose_defaults(purpose) -> tuple:
    """Return (roles, invites_enabled, allow_sharing, share_context) for a new
    group chat's purpose."""
    roles = default_roles()
    invites_enabled = True
    allow_sharing = True
    share_context = True
    default_role = next(role for role in roles if role["_id"] == "default")
    if purpose == "friends":
        # Everyone can customise the chat, manage emotes and pin
        for preset in ("stickerer", "customiser", "pinner"):
            for perm in next(role for role in roles if role["_id"] == preset)["permissions"]:
                default_role["permissions"][perm] = True
    elif purpose == "private":
        invites_enabled = False
        allow_sharing = False
        share_context = False
        # Members can chat but not react/share/edit
        default_role["permissions"] = {
            perm: True
            for perm in ("send_messages", "delete_own_messages", "invite_members")
        }
    elif purpose in ("announcements", "emojis"):
        # Members can't post, but can still react/share/edit
        default_role["permissions"] = {
            perm: True
            for perm in ("add_reactions", "react", "share_posts", "edit_messages")
        }
    return roles, invites_enabled, allow_sharing, share_context
