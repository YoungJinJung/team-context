"""Send credential-bearing requests without forwarding them across redirects."""
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def open_url(request):
    return urllib.request.build_opener(NoRedirect).open(request, timeout=15)
