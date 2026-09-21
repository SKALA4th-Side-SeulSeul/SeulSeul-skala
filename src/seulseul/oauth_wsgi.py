"""Gunicorn이 불러올 OAuth WSGI 앱 진입점."""

from seulseul.oauth_server import create_oauth_http_app

app = create_oauth_http_app()
