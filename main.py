import base64
import itertools
import smtplib
import sys
import threading
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Dict, List, Optional

import yaml
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

app = FastAPI()

_cfg: dict = {}
_relay_cycle = None
_lock = threading.Lock()


def load_config(path):
    with open(path) as f:
        return yaml.safe_load(f)


class SendRequest(BaseModel):
    to: List[str]
    subject: str
    body: str
    html: bool = False
    headers: Optional[Dict[str, str]] = None


class _PlainSMTP(smtplib.SMTP):
    def login(self, user, password, *, initial_response_ok=True):
        self.ehlo_or_helo_if_needed()
        encoded = base64.b64encode(
            b"\x00" + user.encode() + b"\x00" + password.encode()
        ).decode()
        code, resp = self.docmd("AUTH", f"PLAIN {encoded}")
        if code != 235:
            raise smtplib.SMTPAuthenticationError(code, resp)
        return code, resp


def get_relay() -> dict:
    with _lock:
        return next(_relay_cycle)


def send_via_relay(relay: dict, req: SendRequest) -> str:
    from_addr = relay["from"]

    if req.html:
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText(req.body, "html", "utf-8"))
    else:
        msg = MIMEText(req.body, "plain", "utf-8")

    msg["From"] = from_addr
    msg["To"] = ", ".join(req.to)
    msg["Subject"] = req.subject

    if req.headers:
        for k, v in req.headers.items():
            msg[k] = v

    host = relay["host"]
    port = relay.get("port", 587)
    password = relay.get("password", "")

    smtp = _PlainSMTP(host, port, timeout=30)
    smtp.ehlo()
    smtp.login(from_addr, password)
    smtp.sendmail(from_addr, req.to, msg.as_bytes())
    smtp.quit()
    return host


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/send")
def send(req: SendRequest, x_api_key: str = Header(None)):
    api_keys = _cfg.get("api_keys", [])
    if not x_api_key or x_api_key not in api_keys:
        raise HTTPException(status_code=401, detail="unauthorized")

    relay = get_relay()
    try:
        host = send_via_relay(relay, req)
        return {"ok": True, "relay": host}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def main():
    import uvicorn

    global _cfg, _relay_cycle

    path = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"
    _cfg = load_config(path)
    _relay_cycle = itertools.cycle(_cfg.get("relays", []))

    listen = _cfg.get("listen", ":8080")
    if listen.startswith(":"):
        host, port = "0.0.0.0", int(listen[1:])
    else:
        h, p = listen.rsplit(":", 1)
        host, port = h, int(p)

    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
