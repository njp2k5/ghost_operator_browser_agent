import secrets
import string
from datetime import datetime, timezone

from jose import JWTError, jwt

from app.core.config import settings

ALGORITHM = "HS256"
TOKEN_LENGTH = 12


def generate_session_token() -> str:
    """Generate a short random alphanumeric token (e.g. abc123xyz789)."""
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(TOKEN_LENGTH))


def sign_token(token: str) -> str:
    """Wrap a session token in a signed JWT for tamper-proof URL sharing."""
    payload = {
        "sub": token,
        "iat": datetime.now(timezone.utc).timestamp(),
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=ALGORITHM)


def verify_token(signed: str) -> str | None:
    """Verify and extract the raw session token from a signed JWT."""
    try:
        payload = jwt.decode(signed, settings.SECRET_KEY, algorithms=[ALGORITHM])
        return payload.get("sub")
    except JWTError:
        return None
