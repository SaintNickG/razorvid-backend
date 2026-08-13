import base64
import json
import os
import time
from typing import Any, Dict, Optional

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

try:
    import jwt
    from jwt import PyJWKClient
except Exception:  # pragma: no cover - handled at runtime when dependency is absent
    jwt = None
    PyJWKClient = None

from multicam_pipeline.config import AWS_REGION, IS_AWS

security = HTTPBearer(auto_error=False)

AUTH_REQUIRED = os.environ.get("AUTH_REQUIRED", "true" if IS_AWS else "false").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
COGNITO_ISSUER = os.environ.get("COGNITO_ISSUER", "").strip()
COGNITO_AUDIENCE = os.environ.get("COGNITO_CLIENT_ID", "").strip() or os.environ.get(
    "COGNITO_AUDIENCE", ""
).strip()
COGNITO_JWKS_URL = os.environ.get("COGNITO_JWKS_URL", "").strip()
AUTH_ALLOW_UNVERIFIED_TOKENS = os.environ.get(
    "AUTH_ALLOW_UNVERIFIED_TOKENS",
    "false" if IS_AWS else "true",
).lower() in {"1", "true", "yes", "on"}
ADMIN_USER_IDS = {
    item.strip()
    for item in os.environ.get("ADMIN_USER_IDS", "").split(",")
    if item.strip()
}
ADMIN_GROUPS = {
    item.strip()
    for item in os.environ.get("ADMIN_GROUPS", "admin").split(",")
    if item.strip()
}

_jwks_client: Optional[PyJWKClient] = None


def _decode_segment(segment: str) -> Dict[str, Any]:
    padding = "=" * (-len(segment) % 4)
    raw = base64.urlsafe_b64decode(segment + padding)
    return json.loads(raw.decode("utf-8"))


def _parse_unverified_jwt(token: str) -> Dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("Token must contain three parts")

    header = _decode_segment(parts[0])
    payload = _decode_segment(parts[1])
    if header.get("alg") not in {"RS256", "ES256", "HS256", "none"}:
        raise ValueError("Unsupported JWT algorithm")
    return payload


def _validate_claims(payload: Dict[str, Any]) -> Dict[str, Any]:
    now = int(time.time())
    issuer = COGNITO_ISSUER
    audience = COGNITO_AUDIENCE

    if issuer and payload.get("iss") != issuer:
        raise ValueError("JWT issuer mismatch")

    if audience:
        aud = payload.get("aud")
        aud_match = aud == audience or (isinstance(aud, list) and audience in aud)
        # Cognito access tokens frequently expose the app client in `client_id`.
        client_id_match = payload.get("client_id") == audience
        if not aud_match and not client_id_match:
            raise ValueError("JWT audience mismatch")

    if payload.get("exp") is not None and int(payload["exp"]) < now:
        raise ValueError("JWT has expired")

    if payload.get("nbf") is not None and int(payload["nbf"]) > now:
        raise ValueError("JWT is not yet valid")

    token_use = payload.get("token_use")
    if token_use and token_use not in {"id", "access"}:
        raise ValueError("Unexpected token_use claim")

    return payload


def validate_bearer_token(token: str) -> Dict[str, Any]:
    """Validate the bearer token shape and Cognito-style claims.

    This intentionally validates the token structure and the expected Cognito
    issuer/audience claims without requiring a signature library in local dev.
    If PyJWT is installed in the deployment environment, the implementation can
    be extended to verify the signature using the Cognito JWKS.
    """
    if not token:
        raise ValueError("Missing bearer token")

    payload: Dict[str, Any]
    should_verify_signature = bool(COGNITO_ISSUER) and not AUTH_ALLOW_UNVERIFIED_TOKENS

    if should_verify_signature:
        if jwt is None or PyJWKClient is None:
            raise ValueError(
                "Signature verification requires PyJWT. Install dependency 'PyJWT[crypto]'."
            )

        jwks_url = COGNITO_JWKS_URL or f"{COGNITO_ISSUER.rstrip('/')}/.well-known/jwks.json"
        global _jwks_client
        if _jwks_client is None:
            _jwks_client = PyJWKClient(jwks_url)

        try:
            signing_key = _jwks_client.get_signing_key_from_jwt(token)
            payload = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256", "ES256"],
                options={"verify_aud": False},
            )
        except Exception as exc:
            raise ValueError(f"JWT signature verification failed: {exc}") from exc
    else:
        payload = _parse_unverified_jwt(token)

    return _validate_claims(payload)


def principal_id_from_claims(claims: Dict[str, Any]) -> Optional[str]:
    """Extract a stable caller identity from common Cognito/OIDC claims."""
    for key in ("sub", "cognito:username", "username", "preferred_username", "email"):
        value = claims.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def resolve_actor_id(claims: Dict[str, Any], fallback: Optional[str] = None) -> str:
    """
    Resolve caller identity from token claims, with optional trusted fallback.

    In authenticated environments, ownership checks should rely on claims.
    The fallback exists for local development where auth may be disabled.
    """
    actor_id = principal_id_from_claims(claims)
    if actor_id:
        return actor_id
    if fallback and fallback.strip():
        return fallback.strip()
    raise HTTPException(status_code=401, detail="Unable to resolve authenticated user identity.")


def is_admin_from_claims(claims: Dict[str, Any]) -> bool:
    """Return True when claims indicate administrative access."""
    if not AUTH_REQUIRED:
        return True

    actor_id = principal_id_from_claims(claims)
    if actor_id and actor_id in ADMIN_USER_IDS:
        return True

    raw_groups = claims.get("cognito:groups") or claims.get("groups") or []
    if isinstance(raw_groups, str):
        groups = {g.strip() for g in raw_groups.split(",") if g.strip()}
    elif isinstance(raw_groups, list):
        groups = {str(g).strip() for g in raw_groups if str(g).strip()}
    else:
        groups = set()

    if groups.intersection(ADMIN_GROUPS):
        return True

    scope = claims.get("scope")
    if isinstance(scope, str) and "admin" in scope.split():
        return True

    return False


async def require_auth(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> Dict[str, Any]:
    if not AUTH_REQUIRED:
        return {}

    if not credentials or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=401,
            detail="Authentication required. Provide a Bearer token from Cognito.",
        )

    try:
        claims = validate_bearer_token(credentials.credentials)
    except Exception as exc:
        raise HTTPException(status_code=401, detail=f"Invalid bearer token: {exc}") from exc

    return claims


async def require_admin(claims: Dict[str, Any] = Depends(require_auth)) -> Dict[str, Any]:
    """Dependency to enforce admin-only route access."""
    if not is_admin_from_claims(claims):
        raise HTTPException(status_code=403, detail="Admin privileges required.")
    return claims
