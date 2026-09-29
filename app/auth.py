"""Password hashing + signed bearer tokens (stdlib only)."""
import base64
import hashlib
import hmac
import json
import os
import secrets
import time

SECRET_KEY = os.environ.get("SECRET_KEY", "")
TOKEN_TTL = 60 * 60 * 24 * 30          # 30 days ("จดจำการเข้าสู่ระบบ")
TOKEN_TTL_SHORT = 60 * 60 * 12         # 12 hours when not remembered
PBKDF2_ROUNDS = 240_000


def _secret() -> bytes:
    if not SECRET_KEY or len(SECRET_KEY) < 32:
        raise RuntimeError("SECRET_KEY must be set (at least 32 characters)")
    return SECRET_KEY.encode()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${PBKDF2_ROUNDS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, rounds, salt_hex, dk_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(rounds))
        return hmac.compare_digest(dk.hex(), dk_hex)
    except Exception:
        return False


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def make_token(user_id: int, token_version: int, remember: bool = True) -> str:
    ttl = TOKEN_TTL if remember else TOKEN_TTL_SHORT
    payload = _b64(json.dumps({"u": user_id, "v": token_version, "e": int(time.time()) + ttl},
                              separators=(",", ":")).encode())
    sig = _b64(hmac.new(_secret(), payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{sig}"


def read_token(token: str):
    """Returns (user_id, token_version) or None."""
    try:
        payload, sig = token.split(".")
        good = _b64(hmac.new(_secret(), payload.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, good):
            return None
        data = json.loads(_unb64(payload))
        if data["e"] < time.time():
            return None
        return int(data["u"]), int(data["v"])
    except Exception:
        return None


def random_password(n: int = 12) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"
    return "".join(secrets.choice(alphabet) for _ in range(n))
