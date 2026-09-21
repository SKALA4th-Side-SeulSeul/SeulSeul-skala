"""OAuth 설치 시작·콜백 라우트의 공개 경로와 state 발급을 검증한다."""

import html
import re
from urllib.parse import parse_qs, urlsplit

from seulseul.config import SlackOAuthSettings
from seulseul.oauth_server import create_oauth_http_app


def oauth_settings() -> SlackOAuthSettings:
    return SlackOAuthSettings(
        client_id="123456789.123456789",
        client_secret="client-secret",
        signing_secret="signing-secret",
        redirect_uri="https://example.ngrok-free.dev/slack/oauth/callback",
        scopes=("chat:write", "channels:history"),
        storage_dir="/tmp/seulseul-oauth-test",
        port=8080,
        install_path="/slack/install",
        redirect_uri_path="/slack/oauth/callback",
    )


def test_install_route_redirects_to_slack_with_non_empty_state(tmp_path) -> None:
    web_app = create_oauth_http_app(oauth_settings(), storage_dir=tmp_path)

    response = web_app.test_client().get("/slack/install")

    assert response.status_code == 200
    link_match = re.search(r'<a href="([^"]+)">', html.unescape(response.get_data(as_text=True)))
    assert link_match is not None
    query = parse_qs(urlsplit(link_match.group(1)).query)
    assert query["client_id"] == ["123456789.123456789"]
    assert query["redirect_uri"] == ["https://example.ngrok-free.dev/slack/oauth/callback"]
    assert query["state"]


def test_oauth_callback_path_is_handled_by_bolt_instead_of_returning_404(tmp_path) -> None:
    web_app = create_oauth_http_app(oauth_settings(), storage_dir=tmp_path)

    response = web_app.test_client().get("/slack/oauth/callback")

    assert response.status_code != 404
