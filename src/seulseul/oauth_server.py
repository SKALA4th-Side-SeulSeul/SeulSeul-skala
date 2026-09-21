"""Slack 외부 워크스페이스 설치용 OAuth HTTP 서버.

Socket Mode 봇과 분리된 WSGI 앱으로 실행한다. OAuth callback에는 Slack이 전달한
일회성 code와 state가 포함되므로 access log에 query string을 남기지 않는다.
설치 결과는 컨테이너 재시작 후에도 유지할 수 있는 파일 저장소에 보관한다.
"""

from pathlib import Path
from typing import Any

from flask import Flask, Response, request
from slack_bolt import App as BoltApp
from slack_bolt.adapter.flask import SlackRequestHandler
from slack_bolt.oauth.oauth_settings import OAuthSettings
from slack_sdk.oauth.installation_store import FileInstallationStore
from slack_sdk.oauth.state_store import FileOAuthStateStore

from seulseul.config import SlackOAuthSettings, load_oauth_settings


def create_oauth_http_app(
    settings: SlackOAuthSettings | None = None,
    *,
    storage_dir: str | Path | None = None,
) -> Flask:
    """Bolt OAuth를 Flask HTTP 앱의 설치·callback 경로에 연결한다."""
    settings = settings or load_oauth_settings()
    storage_root = Path(storage_dir or settings.storage_dir)
    installation_dir = storage_root / "installations"
    state_dir = storage_root / "states"
    _prepare_private_directory(storage_root)
    _prepare_private_directory(installation_dir)
    _prepare_private_directory(state_dir)

    installation_store = FileInstallationStore(
        base_dir=str(installation_dir),
        historical_data_enabled=False,
        client_id=settings.client_id,
    )
    state_store = FileOAuthStateStore(
        expiration_seconds=600,
        base_dir=str(state_dir),
        client_id=settings.client_id,
    )
    oauth_settings = OAuthSettings(
        client_id=settings.client_id,
        client_secret=settings.client_secret,
        scopes=settings.scopes,
        install_path=settings.install_path,
        redirect_uri_path=settings.redirect_uri_path,
        redirect_uri=settings.redirect_uri,
        installation_store=installation_store,
        state_store=state_store,
    )
    bolt_app = BoltApp(
        signing_secret=settings.signing_secret,
        oauth_settings=oauth_settings,
    )
    request_handler = SlackRequestHandler(bolt_app)
    web_app = Flask("seulseul.oauth_server")

    @web_app.after_request
    def add_security_headers(response: Response) -> Response:
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        return response

    @web_app.get("/healthz")
    def healthz() -> tuple[str, int]:
        return "ok", 200

    @web_app.route(settings.install_path, methods=["GET"])
    @web_app.route(settings.redirect_uri_path, methods=["GET"])
    def slack_oauth() -> Any:
        return request_handler.handle(request)

    return web_app


def _prepare_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


if __name__ == "__main__":
    app = create_oauth_http_app()
    app.run(host="127.0.0.1", port=load_oauth_settings().port)
