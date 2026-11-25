import re, os, time, requests, pyotp, secrets
from pydantic import BaseModel
from quart import Blueprint, request, abort, current_app as app
from quart_schema import validate_request
from pydantic import Field
from typing import Optional
from database import db, registration_blocked_ips
import security
import email_verification
import username_change

auth_bp = Blueprint("auth_bp", __name__, url_prefix="/auth")

class AuthRequest(BaseModel):
    username: str = Field(min_length=1, max_length=20)
    password: str = Field(min_length=1, max_length=255)
    totp_code: Optional[str] = Field(default="", min_length=6, max_length=6)
    mfa_recovery_code: Optional[str] = Field(default="", min_length=10, max_length=10)
    captcha: Optional[str] = Field(default="", max_length=2000)
    email: Optional[str] = Field(default=None, max_length=254)


@auth_bp.post("/login")
@validate_request(AuthRequest)
async def login(data: AuthRequest):
    # Make sure IP isn't ratelimited
    if security.ratelimited(f"login:i:{request.ip}"):
        abort(429)
    security.ratelimit(f"login:i:{request.ip}", 50, 900)

    # Get basic account details
    account = db.usersv0.find_one({"lower_username": data.username.lower()}, projection={
        "_id": 1,
        "flags": 1,
        "tokens": 1,
        "pswd": 1,
        "prev_pswd": 1,
        "mfa_recovery_code": 1
    })
    if not account:
        abort(401)

    # Make sure account isn't deleted
    if account["flags"] & security.UserFlags.DELETED:
        return {"error": True, "type": "accountDeleted"}, 401

    # Make sure account isn't ratelimited
    if security.ratelimited(f"login:u:{account['_id']}"):
        abort(429)

    # Check credentials
    if data.password not in account["tokens"]:
        # Check password (bots and passwordless accounts can never log in this way)
        password_valid = (
            account["pswd"] is not None
            and not (account["flags"] & security.UserFlags.BOT)
            and security.check_password_hash(data.password, account["pswd"])
        )

        # Maybe they put their MFA credentials at the end of their password?
        if (not password_valid) and db.authenticators.count_documents({"user": account["_id"]}, limit=1):
            if (not data.mfa_recovery_code) and data.password.endswith(account["mfa_recovery_code"]):
                try:
                    data.mfa_recovery_code = data.password[-10:]
                    data.password = data.password[:-10]
                except: pass
                else:
                    password_valid = security.check_password_hash(data.password, account["pswd"])
            elif not data.totp_code:
                try:
                    data.totp_code = data.password[-6:]
                    data.password = data.password[:-6]
                except: pass
                else:
                    if re.fullmatch(security.TOTP_REGEX, data.totp_code):
                        password_valid = security.check_password_hash(data.password, account["pswd"])

        # Abort if password is invalid
        if not password_valid:
            # Maybe they're using a recently-changed previous password
            prev_pswd = account.get("prev_pswd")
            if (
                prev_pswd
                and (time.time() - prev_pswd["changed_at"] < 1209600)  # 14 days
                and security.check_password_hash(data.password, prev_pswd["hash"])
            ):
                return {
                    "error": True,
                    "type": "passwordRecentlyChanged",
                    "changed_at": prev_pswd["changed_at"]
                }, 401

            security.ratelimit(f"login:u:{account['_id']}", 5, 60)
            abort(401)

        # Check MFA
        authenticators = list(db.authenticators.find({"user": account["_id"]}))
        if len(authenticators) > 0:
            if data.totp_code:
                passed = False
                for authenticator in authenticators:
                    if authenticator["type"] != "totp":
                        continue
                    if pyotp.TOTP(authenticator["totp_secret"]).verify(data.totp_code, valid_window=1):
                        passed = True
                        break
                if not passed:
                    security.ratelimit(f"login:u:{account['_id']}", 5, 60)
                    abort(401)
            elif data.mfa_recovery_code:
                if data.mfa_recovery_code == account["mfa_recovery_code"]:
                    db.authenticators.delete_many({"user": account["_id"]})
                    db.usersv0.update_one({"_id": account["_id"]}, {"$set": {
                        "mfa_recovery_code": secrets.token_hex(5)
                    }})
                    app.supporter.create_post("inbox", account["_id"], "All multi-factor authenticators have been removed from your account by someone who used your multi-factor authentication recovery code. If this wasn't you, please contact us to secure your account immediately.", category="security")
                else:
                    security.ratelimit(f"login:u:{account['_id']}", 5, 60)
                    abort(401)
            else:
                mfa_methods = set()
                for authenticator in authenticators:
                    mfa_methods.add(authenticator["type"])
                return {
                    "error": True,
                    "type": "mfaRequired",
                    "mfa_methods": list(mfa_methods)
                }, 401

    # Return account and token
    return {
        "error": False,
        "account": security.get_account(account['_id'], True),
        "token": security.create_user_token(account['_id'], request.ip)
    }, 200

@auth_bp.post("/register")
@validate_request(AuthRequest)
async def register(data: AuthRequest):
    # Make sure registration isn't disabled
    if not app.supporter.registration:
        return {"error": True, "type": "registrationDisabled"}, 403
    
    # Make sure IP isn't being ratelimited
    if security.ratelimited(f"register:{request.ip}:f") or security.ratelimited(f"register:{request.ip}:s"):
        abort(429)

    # Make sure password is between 8-72 characters
    if len(data.password) < 8 or len(data.password) > 72:
        abort(400)

    # Make sure username matches regex
    if not re.fullmatch(security.USERNAME_REGEX, data.username):
        abort(400)
    
    # Make sure IP isn't blocked from creating new accounts
    if registration_blocked_ips.search_best(request.ip):
        security.ratelimit(f"register:{request.ip}:f", 5, 30)
        return {"error": True, "type": "registrationBlocked"}, 403

    # A valid, allowed email is required to sign up
    email = email_verification.normalize(data.email)
    if not email_verification.valid_email(email):
        return {"error": True, "type": "invalidEmail"}, 400
    if email_verification.domain_blocked(email):
        return {"error": True, "type": "emailBlocked"}, 403
    if email_verification.email_in_use(email):
        return {"error": True, "type": "emailInUse"}, 409

    # Make sure username isn't taken or held after someone's rename
    if security.account_exists(data.username, ignore_case=True) or username_change.name_blocked_for(data.username.lower()):
        security.ratelimit(f"register:{request.ip}:f", 5, 30)
        return {"error": True, "type": "usernameExists"}, 409

    # Check captcha
    if os.getenv("CAPTCHA_SECRET"):
        try:
            captcha_valid = requests.post("https://api.hcaptcha.com/siteverify", data={
                "secret": os.getenv("CAPTCHA_SECRET"),
                "response": data.captcha,
            }, timeout=10).json()["success"]
        except requests.RequestException:
            captcha_valid = False
        if not captcha_valid:
            return {"error": True, "type": "invalidCaptcha"}, 403

    # Create account
    security.create_account(data.username, data.password, request.ip, email=email)
    await email_verification.send_verification(data.username, email)

    # Ratelimit
    security.ratelimit(f"register:{request.ip}:s", 5, 900)
    
    # Return account and token
    return {
        "error": False,
        "account": security.get_account(data.username, True),
        "token": security.create_user_token(data.username, request.ip)
    }, 200
