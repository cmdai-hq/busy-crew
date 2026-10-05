import base64
import json
import urllib.error
import urllib.request

APPLICATION = "agent-deck"
DEFAULT_ADDRESS = "http://busybar.local"
MAX_RESPONSE_BYTES = 65536


class NoRedirects(urllib.request.HTTPRedirectHandler):
    """Device credentials must never follow an HTTP redirect."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Device:
    def __init__(self, address=DEFAULT_ADDRESS, token=None):
        self.address = address.rstrip("/")
        self.token = token
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirects())

    def request(self, path, method="GET", payload=None):
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["X-API-Token"] = self.token
        data = None if payload is None else json.dumps(payload).encode()
        request = urllib.request.Request(self.address + path, data=data, method=method, headers=headers)
        try:
            with self.opener.open(request, timeout=4) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise ValueError("Device response exceeds size limit")
                return raw
        except urllib.error.HTTPError as exc:
            exc.close()
            raise

    def draw(self, frame):
        return self.request("/api/display/draw", "POST", {
            "application_name": APPLICATION, "priority": 50,
            "elements": [{"id": "front", "type": "xpmbitmap", "display": "front",
                          "x": 0, "y": 0, "timeout": 15, "data": frame.xpm()}]})

    def clear(self):
        # Firmware 1.2.4 ignores application_name in DELETE JSON bodies.
        return self.request("/api/display/draw?application_name=" + APPLICATION, "DELETE")

    def screen(self):
        raw = base64.b64decode(self.request("/api/screen?display=0"), validate=True)
        if len(raw) != 72 * 16 * 3:
            raise ValueError("Unexpected front frame size")
        # BUSY firmware exports BGR888.
        return bytes(c for i in range(0, len(raw), 3) for c in (raw[i+2], raw[i+1], raw[i]))
