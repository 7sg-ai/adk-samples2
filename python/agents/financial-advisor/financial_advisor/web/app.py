"""Dual-mode FastAPI gateway for anonymous and Salesforce advisor sessions."""

from __future__ import annotations

import html
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from urllib.parse import urlparse

import httpx
from fastapi import Cookie, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field

from financial_advisor.client_profile import (
    ACCESS_MODE_STATE_KEY,
    INVESTMENT_GOALS_STATE_KEY,
    PROFILE_SOURCE_STATE_KEY,
    RISK_PROFILE_STATE_KEY,
    _validate_account_id,
)

from .adk_gateway import AdkGateway
from .models import GatewaySession, OAuthTransaction
from .oauth import SalesforceOAuthClient, SalesforceOAuthError
from .profile_service import ProfileService, SalesforceProfileService
from .security import random_urlsafe, sign_cookie, verify_cookie
from .settings import Settings
from .storage import (
    CloudKmsTokenCipher,
    FirestoreGatewayStore,
    GatewayStore,
    InMemoryGatewayStore,
    PlaintextDevelopmentCipher,
    TokenCipher,
)
from .token_provider import AdvisorTokenProvider, ReauthorizationRequired

SESSION_COOKIE = "financial_advisor_session"
OAUTH_COOKIE = "financial_advisor_oauth"


class SessionResponse(BaseModel):
    session_id: str
    access_mode: str


class SalesforceSessionRequest(BaseModel):
    account_id: str = Field(min_length=15, max_length=18)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=20_000)


class GatewayContainer:
    def __init__(
        self,
        *,
        settings: Settings,
        store: GatewayStore,
        cipher: TokenCipher,
        http_client: httpx.AsyncClient,
        adk: AdkGateway,
        profiles: ProfileService,
    ) -> None:
        self.settings = settings
        self.store = store
        self.oauth = SalesforceOAuthClient(settings, http_client)
        self.tokens = AdvisorTokenProvider(
            store=store, cipher=cipher, oauth_client=self.oauth
        )
        self.adk = adk
        self.profiles = profiles


def create_app(  # noqa: PLR0915
    *,
    settings: Settings | None = None,
    store: GatewayStore | None = None,
    cipher: TokenCipher | None = None,
    http_client: httpx.AsyncClient | None = None,
    adk: AdkGateway | None = None,
    profiles: ProfileService | None = None,
) -> FastAPI:
    settings = settings or Settings.from_environment()
    store, cipher = _build_storage(settings, store, cipher)
    container = GatewayContainer(
        settings=settings,
        store=store,
        cipher=cipher,
        http_client=http_client or httpx.AsyncClient(timeout=30),
        adk=adk or AdkGateway(),
        profiles=profiles or SalesforceProfileService(settings.salesforce_mcp_url),
    )
    app = FastAPI(title="Financial Advisor Gateway", docs_url=None, redoc_url=None)
    app.state.gateway = container

    @app.middleware("http")
    async def security_middleware(request: Request, call_next: Any) -> Response:
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("origin")
            allowed_origin = _origin(container.settings.public_base_url)
            if origin and origin != allowed_origin:
                return Response(status_code=403, content="Cross-origin request denied")
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'unsafe-inline' 'self'; "
            "style-src 'self' 'unsafe-inline'; frame-ancestors 'none'"
        )
        return response

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    async def anonymous_home() -> str:
        return _home_page()

    @app.post("/api/anonymous/sessions", response_model=SessionResponse)
    async def create_anonymous_session(response: Response) -> SessionResponse:
        session = await _create_gateway_session(container, access_mode="anonymous")
        _set_session_cookie(response, container.settings, session)
        return SessionResponse(
            session_id=session.session_id, access_mode=session.access_mode
        )

    @app.get("/auth/salesforce/login")
    async def salesforce_login(
        account_id: Annotated[str | None, Query()] = None,
    ) -> RedirectResponse:
        return_path = "/"
        if account_id:
            _validate_account_or_400(account_id)
            return_path = f"/salesforce/accounts/{account_id}"
        state, verifier = random_urlsafe(), random_urlsafe(64)
        transaction = OAuthTransaction(
            state=state,
            code_verifier=verifier,
            return_path=return_path,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
        await container.store.put_oauth_transaction(transaction)
        redirect = RedirectResponse(
            container.oauth.authorization_url(state=state, code_verifier=verifier)
        )
        redirect.set_cookie(
            OAUTH_COOKIE,
            sign_cookie({"state": state}, container.settings.session_signing_key),
            httponly=True,
            secure=container.settings.cookie_secure,
            samesite="lax",
            max_age=600,
        )
        return redirect

    @app.get("/auth/salesforce/callback")
    async def salesforce_callback(
        code: Annotated[str, Query()],
        state: Annotated[str, Query()],
        oauth_cookie: Annotated[str | None, Cookie(alias=OAUTH_COOKIE)] = None,
    ) -> RedirectResponse:
        signed = verify_cookie(
            oauth_cookie or "", container.settings.session_signing_key
        )
        if not signed or signed.get("state") != state:
            raise HTTPException(status_code=400, detail="Invalid OAuth transaction")
        transaction = await container.store.pop_oauth_transaction(state)
        if not transaction or transaction.expires_at <= datetime.now(UTC):
            raise HTTPException(status_code=400, detail="Invalid OAuth transaction")
        try:
            tokens, identity = await container.oauth.exchange_code(
                code=code, code_verifier=transaction.code_verifier
            )
            await container.tokens.save_authorization(identity.advisor_key, tokens)
        except (SalesforceOAuthError, ReauthorizationRequired) as exc:
            raise HTTPException(
                status_code=401, detail="Salesforce authorization failed"
            ) from exc
        auth_session = await _create_gateway_session(
            container,
            access_mode="salesforce-authenticated",
            advisor_key=identity.advisor_key,
        )
        redirect = RedirectResponse(transaction.return_path)
        redirect.delete_cookie(OAUTH_COOKIE)
        _set_session_cookie(redirect, container.settings, auth_session)
        return redirect

    @app.get(
        "/salesforce/accounts/{account_id}",
        response_class=HTMLResponse,
        response_model=None,
    )
    async def salesforce_account_page(
        account_id: str,
        session_cookie: Annotated[
            str | None, Cookie(alias=SESSION_COOKIE)
        ] = None,
    ) -> Response:
        _validate_account_or_400(account_id)
        session = await _get_gateway_session(container, session_cookie)
        if not session or not session.advisor_key:
            return RedirectResponse(
                f"/auth/salesforce/login?account_id={account_id}"
            )
        return HTMLResponse(_salesforce_page(account_id))

    @app.post("/api/salesforce/sessions", response_model=SessionResponse)
    async def create_salesforce_session(
        body: SalesforceSessionRequest,
        response: Response,
        session_cookie: Annotated[
            str | None, Cookie(alias=SESSION_COOKIE)
        ] = None,
    ) -> SessionResponse:
        _validate_account_or_400(body.account_id)
        auth_session = await _require_advisor(container, session_cookie)
        try:
            access_token = await container.tokens.get_valid_access_token(
                auth_session.advisor_key or ""
            )
            profile = await container.profiles.get_profile(
                account_id=body.account_id, access_token=access_token
            )
        except ReauthorizationRequired as exc:
            raise HTTPException(
                status_code=401, detail="Salesforce reauthorization required"
            ) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=403, detail="Account is unavailable or unauthorized"
            ) from exc
        session = await _create_gateway_session(
            container,
            access_mode="salesforce",
            advisor_key=auth_session.advisor_key,
            adk_state={
                RISK_PROFILE_STATE_KEY: profile.risk_profile,
                INVESTMENT_GOALS_STATE_KEY: profile.investment_goals,
                PROFILE_SOURCE_STATE_KEY: "salesforce",
            },
        )
        _set_session_cookie(response, container.settings, session)
        return SessionResponse(
            session_id=session.session_id, access_mode=session.access_mode
        )

    @app.post("/api/sessions/{session_id}/messages")
    async def chat(
        session_id: str,
        body: ChatRequest,
        session_cookie: Annotated[
            str | None, Cookie(alias=SESSION_COOKIE)
        ] = None,
    ) -> StreamingResponse:
        session = await _get_gateway_session(container, session_cookie)
        if not session or session.session_id != session_id:
            raise HTTPException(status_code=401, detail="Invalid session")
        return StreamingResponse(
            container.adk.chat(
                user_id=session.adk_user_id,
                session_id=session.adk_session_id,
                message=body.message,
            ),
            media_type="text/plain",
        )

    return app


async def _create_gateway_session(
    container: GatewayContainer,
    *,
    access_mode: str,
    advisor_key: str | None = None,
    adk_state: dict[str, Any] | None = None,
) -> GatewaySession:
    now = datetime.now(UTC)
    public_id = random_urlsafe()
    adk_user_id = advisor_key or f"anonymous:{random_urlsafe()}"
    state = {ACCESS_MODE_STATE_KEY: access_mode, **(adk_state or {})}
    adk_session_id = await container.adk.create_session(
        user_id=adk_user_id, state=state
    )
    session = GatewaySession(
        session_id=public_id,
        adk_session_id=adk_session_id,
        adk_user_id=adk_user_id,
        access_mode=access_mode,
        advisor_key=advisor_key,
        created_at=now,
        expires_at=now
        + timedelta(hours=8 if advisor_key else 1),
    )
    await container.store.put_session(session)
    return session


async def _get_gateway_session(
    container: GatewayContainer, cookie: str | None
) -> GatewaySession | None:
    signed = verify_cookie(cookie or "", container.settings.session_signing_key)
    session_id = signed.get("sid") if signed else None
    return await container.store.get_session(session_id) if session_id else None


async def _require_advisor(
    container: GatewayContainer, cookie: str | None
) -> GatewaySession:
    session = await _get_gateway_session(container, cookie)
    if not session or session.access_mode not in {
        "salesforce-authenticated",
        "salesforce",
    }:
        raise HTTPException(status_code=401, detail="Salesforce login required")
    return session


def _set_session_cookie(
    response: Response, settings: Settings, session: GatewaySession
) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        sign_cookie({"sid": session.session_id}, settings.session_signing_key),
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        max_age=8 * 60 * 60 if session.advisor_key else 60 * 60,
    )


def _build_storage(
    settings: Settings,
    store: GatewayStore | None,
    cipher: TokenCipher | None,
) -> tuple[GatewayStore, TokenCipher]:
    if store and cipher:
        return store, cipher
    if settings.storage_backend == "firestore":
        if not settings.firestore_project or not settings.kms_key_name:
            raise RuntimeError("Firestore project and TOKEN_KMS_KEY_NAME are required")
        return (
            store
            or FirestoreGatewayStore(
                project=settings.firestore_project,
                database=settings.firestore_database,
            ),
            cipher or CloudKmsTokenCipher(settings.kms_key_name),
        )
    return store or InMemoryGatewayStore(), cipher or PlaintextDevelopmentCipher()


def _validate_account_or_400(account_id: str) -> None:
    try:
        _validate_account_id(account_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid Account ID") from exc


def _origin(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        raise RuntimeError("PUBLIC_BASE_URL must be an absolute URL")
    return f"{parsed.scheme}://{parsed.netloc}"


def _home_page() -> str:
    return """<!doctype html><html><body><h1>Financial Advisor</h1>
<p>Public mode does not connect to Salesforce or access Account data.</p>
<button id='start'>Start anonymous session</button><pre id='out'></pre>
<script>document.getElementById('start').onclick=async()=>{const r=await fetch(
'/api/anonymous/sessions',{method:'POST'});document.getElementById('out').textContent=
JSON.stringify(await r.json(),null,2)};</script></body></html>"""


def _salesforce_page(account_id: str) -> str:
    safe_id = html.escape(account_id)
    return f"""<!doctype html><html><body><h1>Financial Advisor</h1>
<p>Authenticated Salesforce mode for the selected Account.</p>
<button id='start'>Start advisor session</button><pre id='out'></pre>
<script>document.getElementById('start').onclick=async()=>{{const r=await fetch(
'/api/salesforce/sessions',{{method:'POST',headers:{{'Content-Type':'application/json'}},
body:JSON.stringify({{account_id:'{safe_id}'}})}});document.getElementById('out').textContent=
JSON.stringify(await r.json(),null,2)}};</script></body></html>"""


app = create_app()