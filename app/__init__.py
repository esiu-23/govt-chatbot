import logging
import threading

from flask import Flask, request
from flask import redirect, url_for

from .routes import pages, chat, legislation, meetings, subscriptions, illinois_legislation


logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
_raw_log = logging.getLogger("raw_wsgi")


class RawLoggingMiddleware:
    """Fires before Flask routing — confirms the request reached Python at all."""
    def __init__(self, wsgi_app):
        self._app = wsgi_app

    def __call__(self, environ, start_response):
        _raw_log.info(
            "[raw] %s %s  content_type=%r  content_length=%s  thread=%s",
            environ.get("REQUEST_METHOD"),
            environ.get("PATH_INFO"),
            environ.get("CONTENT_TYPE"),
            environ.get("CONTENT_LENGTH"),
            threading.current_thread().name,
        )
        return self._app(environ, start_response)


def create_app() -> Flask:
    app = Flask(__name__, static_folder="../static")
    app.wsgi_app = RawLoggingMiddleware(app.wsgi_app)
    app.logger.setLevel(logging.INFO)

    @app.before_request
    def log_request_start():
        app.logger.info(
            "[flask] %s %s  content_type=%r  content_length=%s  thread=%s",
            request.method,
            request.path,
            request.content_type,
            request.content_length,
            threading.current_thread().name,
        )

    @app.after_request
    def log_request_end(response):
        app.logger.info(
            "[flask] %s %s → %s",
            request.method,
            request.path,
            response.status_code,
        )
        return response

    app.register_blueprint(pages.bp)
    app.register_blueprint(chat.bp)
    app.register_blueprint(legislation.bp)
    app.register_blueprint(illinois_legislation.bp)
    app.register_blueprint(meetings.bp)
    app.register_blueprint(subscriptions.bp)

    print("BLUEPRINTS:", app.blueprints)
    print("URL MAP:", app.url_map)

    return app
