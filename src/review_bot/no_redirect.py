"""A ``urllib`` handler that follows no redirect, for every client that sends a credential."""

import urllib.request
from http.client import HTTPMessage

__all__ = ["NoRedirect"]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuses every redirect, so a credential never follows one to another host."""

    def redirect_request(  # noqa: PLR0913, PLR0917 - urllib's signature
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> None:
        """Return None, so urllib raises the redirect's status as an ``HTTPError``."""
