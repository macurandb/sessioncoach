"""Helpers for tests that talk to the dashboard server (no network beyond 127.0.0.1)."""


def session_cookie(httpd):
    """The Cookie header the dashboard's login link would have set (every page and API call needs it)."""
    return "sessioncoach_%d=%s" % (httpd.server_address[1], httpd.RequestHandlerClass.kit_session)
