"""One-click Cloudflare Worker relay deployment over HTTPS (urllib only, no Qt, no Node).

The Cloudflare API token is used only in Authorization headers in memory and is never written
to disk, settings, logs, the activity log, or exception messages.
"""
import json
import os
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

API = os.environ.get("BIRUNI_CF_API") or "https://api.cloudflare.com/client/v4"
TIMEOUT = 30
BUCKET = "biruni-mailbox"
MB_DAYS = (7, 14, 30)
EXPIRE_EXTRA = 30
_SUB_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"
_BUNDLE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "org_relay_worker.js")


class OrgRelayError(RuntimeError):
    """Cloudflare deployment error carrying only sanitized API error messages."""


class ChooseAccount(Exception):
    """Raised when the token grants access to multiple Cloudflare accounts."""

    def __init__(self, accounts):
        self.accounts = list(accounts)
        super().__init__("multiple Cloudflare accounts")


def bundle(hub_src, worker_src):
    """Combine relay/hub.js and relay/worker.js into one ES module without local imports."""
    hub_clean = re.sub(r"(?m)^export\s+(class\s+Hub\b)", r"\1", hub_src.replace("\r\n", "\n"))
    worker_clean = re.sub(
        r"(?m)^import\s+[^\n]*\./hub\.js['\"];?\r?\n?", "", worker_src.replace("\r\n", "\n")
    )
    return hub_clean.rstrip() + "\n\n" + worker_clean.lstrip()


def _scrub(text, token):
    s = str(text or "")
    if token:
        s = s.replace(token, "***")
    return s


def _api_base():
    return (os.environ.get("BIRUNI_CF_API") or API).rstrip("/")


def _cf_error_codes(raw):
    try:
        data = json.loads(raw.decode("utf-8", errors="replace")) if raw else {}
    except ValueError:
        data = {}
    if isinstance(data, dict) and isinstance(data.get("errors"), list):
        return [
            int(e["code"])
            for e in data["errors"]
            if isinstance(e, dict) and isinstance(e.get("code"), int) and not isinstance(e.get("code"), bool)
        ]
    return []


def _cf_error_message(raw, status, token):
    try:
        data = json.loads(raw.decode("utf-8", errors="replace")) if raw else {}
    except ValueError:
        data = {}
    if isinstance(data, dict) and isinstance(data.get("errors"), list):
        msgs = [
            str(e.get("message") or "").strip()
            for e in data["errors"]
            if isinstance(e, dict) and e.get("message")
        ]
        if msgs:
            return _scrub("; ".join(msgs), token)
    return _scrub(f"HTTP {status}", token)


def _request(method, url, token, body=None, content_type=None):
    headers = {"Authorization": f"Bearer {token}", "User-Agent": "Biruni-GIS"}
    if content_type:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read()
        msg = _cf_error_message(raw, e.code, token)
        err = OrgRelayError(msg)
        err.status = e.code
        err.codes = _cf_error_codes(raw)
        raise err from None
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        reason = getattr(e, "reason", e)
        err = OrgRelayError(_scrub(str(reason), token))
        err.status = None
        err.codes = []
        raise err from None
    try:
        data = json.loads(raw.decode("utf-8")) if raw else {}
    except (ValueError, UnicodeDecodeError):
        err = OrgRelayError(f"Invalid JSON response (HTTP {status})")
        err.status = status
        err.codes = []
        raise err from None
    if isinstance(data, dict) and data.get("success") is False:
        err = OrgRelayError(_cf_error_message(raw, status, token))
        err.status = status
        err.codes = _cf_error_codes(raw)
        raise err
    return data


def _cf_json(method, path, token, payload=None):
    body = None
    ctype = None
    if payload is not None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        ctype = "application/json"
    return _request(method, _api_base() + path, token, body=body, content_type=ctype)


def _multipart(meta_dict, worker_bytes):
    boundary = "----biruni-" + secrets.token_hex(12)
    meta_bytes = json.dumps(meta_dict, separators=(",", ":")).encode("utf-8")
    body = b"".join([
        f"--{boundary}\r\n".encode("ascii"),
        b'Content-Disposition: form-data; name="metadata"\r\n',
        b"Content-Type: application/json\r\n\r\n",
        meta_bytes,
        b"\r\n",
        f"--{boundary}\r\n".encode("ascii"),
        b'Content-Disposition: form-data; name="worker.js"; filename="worker.js"\r\n',
        b"Content-Type: application/javascript+module\r\n\r\n",
        worker_bytes,
        b"\r\n",
        f"--{boundary}--\r\n".encode("ascii"),
    ])
    return f"multipart/form-data; boundary={boundary}", body


def _load_bundle_bytes():
    with open(_BUNDLE_PATH, "r", encoding="utf-8") as f:
        return f.read().encode("utf-8")


def _live_url(host, path):
    api = _api_base()
    parsed = urllib.parse.urlsplit(api)
    p = path if str(path).startswith("/") else ("/" + str(path))
    if parsed.netloc.lower() == "api.cloudflare.com":
        return f"https://{host}{p}"
    return f"{parsed.scheme}://{parsed.netloc}{p}"


def _probe_v(host):
    url = _live_url(host, "/v")
    req = urllib.request.Request(url, headers={"User-Agent": "Biruni-GIS"}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=min(TIMEOUT, 10)) as resp:
            raw = resp.read()
        data = json.loads(raw.decode("utf-8")) if raw else {}
        if isinstance(data, dict) and "rel" in data:
            return {"rel": int(data.get("rel") or 0), "mb": bool(data.get("mb"))}
    except Exception:  # noqa: BLE001
        pass
    return None


def _r2_error_code(err):
    codes = set(getattr(err, "codes", None) or ())
    status = getattr(err, "status", None)
    msg = str(err).lower()
    if 10042 in codes or "enable r2" in msg or "r2 is not enabled" in msg:
        return "r2_off"
    if status == 403 or 10000 in codes:
        return "r2_perm"
    return "r2_fail"


def _ensure_bucket(account_id, tok, days, say):
    """Ensure the R2 bucket, lock rule, and lifecycle rule exist; returns '' on success or an error code."""
    days = int(days) if days in MB_DAYS else 7
    base = f"/accounts/{account_id}/r2/buckets/{BUCKET}"
    say("Checking R2 mailbox bucket…")
    need_create = False
    try:
        _cf_json("GET", base, tok)
    except OrgRelayError as e:
        if getattr(e, "status", None) == 404 and 10042 not in (getattr(e, "codes", None) or ()) and "enable r2" not in str(e).lower():
            need_create = True
        else:
            return _r2_error_code(e)
    except Exception:  # noqa: BLE001
        return "r2_fail"

    if need_create:
        say("Creating R2 mailbox bucket…")
        try:
            _cf_json("POST", f"/accounts/{account_id}/r2/buckets", tok, {"name": BUCKET})
        except OrgRelayError as e:
            return _r2_error_code(e)
        except Exception:  # noqa: BLE001
            return "r2_fail"

    try:
        _cf_json("PUT", f"{base}/lock", tok, {
            "rules": [{
                "id": "biruni-mb-lock",
                "enabled": True,
                "prefix": "o/",
                "condition": {"type": "Age", "maxAgeSeconds": days * 86400},
            }]
        })
        _cf_json("PUT", f"{base}/lifecycle", tok, {
            "rules": [
                {
                    "id": "biruni-mb-o-expire",
                    "enabled": True,
                    "conditions": {"prefix": "o/"},
                    "deleteObjectsTransition": {"condition": {"type": "Age", "maxAgeSeconds": (days + EXPIRE_EXTRA) * 86400}},
                },
                {
                    "id": "biruni-mb-rc-expire",
                    "enabled": True,
                    "conditions": {"prefix": "rc/"},
                    "deleteObjectsTransition": {"condition": {"type": "Age", "maxAgeSeconds": 90 * 86400}},
                },
            ]
        })
    except OrgRelayError as e:
        code = _r2_error_code(e)
        return code if code in ("r2_off", "r2_perm") else "r2_fail"
    except Exception:  # noqa: BLE001
        return "r2_fail"
    return ""


def _poll_live(host, token):
    dummy = "biruni" + "0" * 40
    url = _live_url(host, f"/r/{dummy}")

    deadline = time.monotonic() + 60.0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise OrgRelayError("Worker did not become reachable within 60 seconds.")
        req = urllib.request.Request(url, headers={"User-Agent": "Biruni-GIS"}, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=min(TIMEOUT, max(1.0, remaining))) as resp:
                if 400 <= resp.status < 500:
                    return
        except urllib.error.HTTPError as e:
            if 400 <= e.code < 500:
                return
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            _ = _scrub(str(e), token)
        sleep_for = min(1.5, deadline - time.monotonic())
        if sleep_for > 0:
            time.sleep(sleep_for)


def deploy(token, account_id=None, progress=None, mailbox=None):
    """Deploy biruni-relay to the user's Cloudflare account; returns {'host', 'account'} (plus mb fields when mailbox is given)."""
    from . import bridge

    tok = str(token or "").strip()
    if not tok:
        raise OrgRelayError("API token is required.")

    def say(msg):
        if progress is not None:
            progress(msg)

    # 1. Account
    if not account_id:
        say("Checking Cloudflare account…")
        res = _cf_json("GET", "/accounts", tok)
        accounts = [
            {"id": str(a.get("id") or ""), "name": str(a.get("name") or a.get("id") or "")}
            for a in (res.get("result") or [])
            if isinstance(a, dict) and a.get("id")
        ]
        if not accounts:
            raise OrgRelayError("No Cloudflare account found for this token.")
        if len(accounts) > 1:
            raise ChooseAccount(accounts)
        account_id = accounts[0]["id"]

    # 2. Workers subdomain
    say("Checking Workers subdomain…")
    sub = ""
    try:
        sub_res = _cf_json("GET", f"/accounts/{account_id}/workers/subdomain", tok)
        r = sub_res.get("result")
        if isinstance(r, dict):
            sub = str(r.get("subdomain") or "").strip()
    except OrgRelayError as e:
        if getattr(e, "status", None) != 404:
            raise
    if not sub:
        say("Creating Workers subdomain…")
        candidate = "biruni-" + "".join(secrets.choice(_SUB_ALPHABET) for _ in range(6))
        try:
            put_res = _cf_json("PUT", f"/accounts/{account_id}/workers/subdomain", tok, {"subdomain": candidate})
            r = put_res.get("result")
            sub = str((r or {}).get("subdomain") or candidate).strip() if isinstance(r, dict) else candidate
        except OrgRelayError:
            raise OrgRelayError(
                "Please open Workers & Pages once in your Cloudflare dashboard, then press Deploy again."
            ) from None

    # 3. Check if biruni-relay already exists
    say("Checking existing Workers scripts…")
    scripts_res = _cf_json("GET", f"/accounts/{account_id}/workers/scripts", tok)
    exists = any(
        isinstance(s, dict) and (s.get("id") == "biruni-relay" or s.get("name") == "biruni-relay")
        for s in (scripts_res.get("result") or [])
    )

    mb_want = isinstance(mailbox, dict)
    mb_days = 7
    mb_secret = ""
    mb_error = ""
    if mb_want:
        raw_days = mailbox.get("days")
        mb_days = int(raw_days) if raw_days in MB_DAYS else 7
        mb_error = _ensure_bucket(account_id, tok, mb_days, say)
        if not mb_error:
            given_sec = str(mailbox.get("secret") or "").strip()
            mb_secret = given_sec if len(given_sec) == 43 else secrets.token_urlsafe(32)

    # 4. Upload worker module
    say("Uploading relay Worker…")
    worker_bytes = _load_bundle_bytes()

    def upload(include_migrations):
        bindings = [{"type": "durable_object_namespace", "name": "HUB", "class_name": "HubDO"}]
        if mb_want and not mb_error:
            bindings.append({"type": "r2_bucket", "name": "MB", "bucket_name": BUCKET})
            bindings.append({"type": "secret_text", "name": "MB_SECRET", "text": mb_secret})
        meta = {
            "main_module": "worker.js",
            "compatibility_date": "2026-01-01",
            "bindings": bindings,
        }
        if include_migrations:
            meta["migrations"] = {"new_tag": "v1", "new_sqlite_classes": ["HubDO"]}
        ctype, body = _multipart(meta, worker_bytes)
        _request("PUT", f"{_api_base()}/accounts/{account_id}/workers/scripts/biruni-relay", tok,
                 body=body, content_type=ctype)

    is_new = not exists
    try:
        upload(is_new)
    except OrgRelayError as e:
        msg_low = str(e).lower()
        if "migration" in msg_low or "durable" in msg_low or "sqlite" in msg_low:
            upload(not is_new)
        else:
            raise

    # 5. Enable workers.dev subdomain
    say("Enabling workers.dev route…")
    _cf_json("POST", f"/accounts/{account_id}/workers/scripts/biruni-relay/subdomain", tok,
             {"enabled": True, "previews_enabled": False})

    # 6. Poll until live
    host = f"biruni-relay.{sub}.workers.dev".lower()
    if not bridge.ORG_RE.fullmatch(host):
        raise OrgRelayError("Invalid worker hostname returned.")
    say("Waiting for relay to come online…")
    _poll_live(host, tok)

    # 7. Validate and return
    if not bridge.ORG_RE.fullmatch(host):
        raise OrgRelayError("Invalid worker hostname returned.")
    if not mb_want:
        return {"host": host, "account": account_id}
    mb_ok = False
    if not mb_error:
        pv = _probe_v(host)
        if pv and pv.get("mb"):
            mb_ok = True
        else:
            mb_error = "r2_bind"
            mb_secret = ""
    return {
        "host": host,
        "account": account_id,
        "mb": mb_ok,
        "mb_error": mb_error,
        "mb_secret": mb_secret if mb_ok else "",
        "mb_days": mb_days,
    }
