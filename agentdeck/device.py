import base64
import json
import urllib.request

APPLICATION = "agent-deck"


class Device:
    def __init__(self, address="http://10.0.4.20", token=None):
        self.address = address.rstrip("/")
        self.token = token
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(self, path, method="GET", payload=None):
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["X-API-Token"] = self.token
        data = None if payload is None else json.dumps(payload).encode()
        request = urllib.request.Request(self.address + path, data=data, method=method, headers=headers)
        with self.opener.open(request, timeout=4) as response:
            return response.read()

    def draw(self, frame):
        return self.request("/api/display/draw", "POST", {
            "application_name": APPLICATION, "priority": 50,
            "elements": [{"id": "front", "type": "xpmbitmap", "display": "front",
                          "x": 0, "y": 0, "timeout": 15, "data": frame.xpm()}]})

    def clear(self):
        return self.request("/api/display/draw", "DELETE", {"application_name": APPLICATION})

    def screen(self):
        raw = base64.b64decode(self.request("/api/screen?display=0"))
        if len(raw) != 72 * 16 * 3:
            raise ValueError("Unexpected front frame size")
        # BUSY firmware exports BGR888.
        return bytes(c for i in range(0, len(raw), 3) for c in (raw[i+2], raw[i+1], raw[i]))
