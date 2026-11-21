#!/usr/bin/env python3
"""Client for the LEGO parts recognition server (the "lego-moc" agent skill).

Stage 1: uploads 1-30 photos of loose LEGO parts, waits for the asynchronous
result and stores it in a local directory shared by all agents (~/.lego-parts/,
or $LEGO_PARTS_HOME).  Stage 2: downloads the MOC design guide and connector
sheets, submits an LDraw model designed from that inventory and fetches the
verifier's report.  Python 3.8+, standard library only.  See ../SKILL.md.

  lego.py scan [--no-wait] [--wait SECONDS] [--images] PHOTO...
  lego.py status [--wait SECONDS] [--images] [TASK_ID...]
  lego.py parts [--images]
  lego.py whoami
  lego.py moc guide [--parts-file PATH]
  lego.py moc submit [--title NAME] [--design design.json] [--moc MOC_ID] [--parts-file PATH]
                     [--no-wait] [--wait SECONDS] MODEL.ldr
  lego.py moc status [--wait SECONDS] [MOC_ID]

Exit codes: 0 all tasks finished / MOC passed, 4 MOC failed the check, 3 still
running, 1 error (including a server-side MOC error), 2 usage error.
The last line printed is always "RESULT " followed by a one-line JSON object.
"""

import argparse
import errno
import glob
import hashlib
import http.client
import json
import mimetypes
import os
import re
import sys
import tempfile
import time
import traceback
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

# Replaced with the server's public address when this file is downloaded from
# {server}/skill/install/lego.py.  The placeholder must appear on this line only.
DEFAULT_SERVER = "http://136.117.205.131"
PLACEHOLDER = "__LEGO_" + "SERVER__"  # split in two so the installer's replacement leaves it alone

VERSION = "0.2"
API_PREFIX = "/skill/v1"
MAX_PHOTOS = 30
MAX_PHOTO_BYTES = 25 * 1024 * 1024
CHUNK_FILES = 4
CHUNK_BYTES = 80 * 1024 * 1024
SCAN_WAIT_S = 480
TIMEOUT_S = 30
UPLOAD_TIMEOUT_S = 300
GET_RETRIES = 3
POLL_DEFAULT_S, POLL_MIN_S, POLL_MAX_S = 5.0, 1.0, 60.0
IDS_PER_QUERY = 100
ROWS_SHOWN = 10
TERMINAL = ("done", "failed")
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif")
MIME_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
              ".webp": "image/webp", ".heic": "image/heic", ".heif": "image/heif"}
EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_PENDING, EXIT_MOC_FAILED = 0, 1, 2, 3, 4
# A failed upload request with one of these codes loses only that request's
# files; any other failure (quota, full queue, network, server error) stops the upload.
SKIP_CHUNK_CODES = ("bad_request", "bad_image", "no_photos", "photo_too_large", "too_many_photos")
HINTS = {
    "quota_user_daily": "the daily photo limit for this user ID is used up; it resets at 00:00 UTC",
    "quota_ip_ids": "this network has created too many new user IDs today; try again tomorrow",
    "queue_full": "the server's queue is full; try again later",
    "photo_too_large": "each photo must be at most 25 MB",
    "too_many_photos": "at most 30 photos per scan",
    "no_photos": "none of the files was a usable JPEG/PNG/WEBP/HEIC photo",
    "network": "check the network or proxy, and that the server address is right",
    "quota_moc_daily": "the daily MOC submission limit for this user ID is used up; it resets at 00:00 UTC",
    "moc_queue_full": "the server's MOC check queue is full; try again in a few minutes",
    "bad_model": "the model must be an LDraw text file of at most 1 MB with only 0 and 1 lines, no sub-models",
    "bad_parts": "the parts file must be an inventory in the parts.json format written by lego.py parts",
    "not_ready": "the server is still generating connector sheets; run moc guide again in a few minutes",
}

# Stage 2 (MOC check), coord/api-skill.md section 3.
MOC_WAIT_S = 300
MAX_MODEL_BYTES = 1024 * 1024
MAX_PARTS_BYTES = 2 * 1024 * 1024
MAX_TITLE_CHARS = 80
CONNECTOR_BATCH = 400
NOT_READY_RETRY_S = 10.0  # on not_ready (503): retry every 10 s ...
NOT_READY_MAX_S = 180.0   # ... for at most 3 minutes over the whole command
ISSUE_PARTS_SHOWN = 12
MOC_TERMINAL = ("passed", "failed", "error")
PART_LINE_RE = re.compile(r"^[ \t]*1[ \t]")
UNSUPPORTED_RE = re.compile(r"^(\S+): not a supported part[ \t]*$", re.M)


class Fatal(Exception):
    """Ends the command with exit code 1; the message is shown to the user."""


class UsageError(Exception):
    """Ends the command with exit code 2."""


class ApiError(Exception):
    """A request failed: an HTTP error (status set) or a network failure (status None)."""

    def __init__(self, status, code, message, refused=False):
        Exception.__init__(self, message)
        self.status = status
        self.code = code
        self.message = message
        self.refused = refused  # connection refused: nothing was sent, any method may be retried


def describe(err):
    text = "[{}] {}".format(err.code, err.message)
    hint = HINTS.get(err.code)
    return text + " (" + hint + ")" if hint else text


# ---------------------------------------------------------------- output

def say(*parts):
    print(*parts, flush=True)


def setup_stdout():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # never die on a console that cannot show a character
        except (AttributeError, ValueError):
            pass


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def elapsed(ctx):
    s = int(time.monotonic() - ctx["start"])
    return "{:02d}:{:02d}".format(s // 60, s % 60)


def add_unique(target, items):
    for item in items:
        if item not in target:
            target.append(item)


def valid_id(value):
    return isinstance(value, str) and bool(ID_RE.match(value))


def safe_name(value):
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(value)).strip(".") or "_"


def python_cmd():
    return "python" if os.name == "nt" else "python3"


def script_cmd(args):
    return '{} "{}" {}'.format(python_cmd(), os.path.abspath(__file__), args)


# ---------------------------------------------------------------- local files

def home_dir():
    raw = os.environ.get("LEGO_PARTS_HOME", "").strip() or os.path.join("~", ".lego-parts")
    return Path(os.path.abspath(os.path.expanduser(raw)))


def parts_path(ctx):
    return ctx["home"] / "parts.json"


def tasks_dir(ctx):
    return ctx["home"] / "tasks"


def write_atomic(path, data):
    """Write bytes, or an object as pretty JSON, to path via a temp file and os.replace."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not isinstance(data, bytes):
        data = (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix="." + path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(5):
            try:
                os.replace(tmp, str(path))
                break
            except PermissionError:  # Windows: the target is briefly open in another process
                if attempt == 4:
                    raise
                time.sleep(0.2)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def read_json(path):
    """Parsed JSON at path, or None when the file does not exist. Raises ValueError/OSError."""
    try:
        with open(str(path), "r", encoding="utf-8-sig") as f:  # -sig: tolerate a BOM from Notepad
            return json.loads(f.read())
    except FileNotFoundError:
        return None


@contextmanager
def file_lock(path, timeout=15.0, stale=300.0):
    """Cross-platform exclusive lock: a <path>.lock file created with O_EXCL."""
    lock = str(path) + ".lock"
    Path(lock).parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    while True:
        try:
            os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            break
        except (FileExistsError, PermissionError):
            try:
                if time.time() - os.path.getmtime(lock) > stale:
                    os.unlink(lock)  # left behind by a killed process
                    continue
            except OSError:
                continue
            if time.monotonic() > deadline:
                raise Fatal("{} is held by another lego.py run; if none is running, delete that file".format(lock))
            time.sleep(0.05)
    try:
        yield
    finally:
        try:
            os.unlink(lock)
        except OSError:
            pass


def load_ids(path, key):
    """(IDs listed under key in the JSON file at path, file was readable). Missing or empty file = no IDs."""
    try:
        with open(str(path), "r", encoding="utf-8-sig") as f:
            text = f.read()
    except FileNotFoundError:
        return [], True
    except OSError:
        return [], False
    if not text.strip():
        return [], True
    try:
        data = json.loads(text)
    except ValueError:
        return [], False
    ids = data.get(key) if isinstance(data, dict) else data
    if not isinstance(ids, list):
        return [], False
    return list(dict.fromkeys(i for i in ids if valid_id(i))), True


def read_ids(ctx, path, key):
    ids, ok = load_ids(path, key)
    warned = ctx.setdefault("ids_warned", set())
    if not ok and str(path) not in warned:
        warned.add(str(path))
        say("WARNING: {} is unreadable; treating it as empty (the old file is kept as "
            "{}.corrupt when {} is next rewritten)".format(path, path.name, path.name))
    return ids


def update_ids(path, key, add=(), remove=()):
    """Add/remove IDs in a JSON ID list under a lock, so concurrent agents don't lose IDs."""
    with file_lock(path):
        ids, ok = load_ids(path, key)
        if not ok:
            try:
                write_atomic(path.with_name(path.name + ".corrupt"), path.read_bytes())
            except OSError:
                pass
        drop = set(remove)
        new = [i for i in ids if i not in drop]
        add_unique(new, [i for i in add if i not in drop])
        if new != ids or not ok or not path.exists():
            write_atomic(path, {key: new})


def load_pending(ctx):
    """(task IDs in pending.json, file was readable)."""
    return load_ids(ctx["home"] / "pending.json", "task_ids")


def read_pending(ctx):
    return read_ids(ctx, ctx["home"] / "pending.json", "task_ids")


def update_pending(ctx, add=(), remove=()):
    update_ids(ctx["home"] / "pending.json", "task_ids", add, remove)


def moc_dir(ctx):
    return ctx["home"] / "moc"


def moc_pending_path(ctx):
    return moc_dir(ctx) / "pending.json"


def read_moc_pending(ctx):
    return read_ids(ctx, moc_pending_path(ctx), "moc_ids")


def update_moc_pending(ctx, add=(), remove=()):
    update_ids(moc_pending_path(ctx), "moc_ids", add, remove)


def load_user(ctx):
    path = ctx["home"] / "user.json"
    try:
        data = read_json(path)
    except (OSError, ValueError) as e:
        raise Fatal("{} is unreadable ({}). It holds your user ID: repair it by hand, or move it away "
                    "to start a new, empty inventory.".format(path, e))
    if data is None:
        return None
    if not isinstance(data, dict) or not valid_id(data.get("user_id")):
        raise Fatal("{} has no valid user_id. Repair it by hand, or move it away to start a new, "
                    "empty inventory.".format(path))
    return data


def load_inventory(ctx, path=None):
    """(rows, total quantity, problem text or None) from the local parts.json (or path)."""
    path = Path(path) if path is not None else parts_path(ctx)
    try:
        data = read_json(path)
    except (OSError, ValueError) as e:
        return [], 0, "{} is not valid JSON ({})".format(path.name, e)
    if data is None:
        return [], 0, "no {} yet".format(path.name)
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return [], 0, "{} has no items list".format(path.name)
    rows = [r for r in items if isinstance(r, dict)]
    total = sum(r["quantity"] for r in rows
                if isinstance(r.get("quantity"), int) and not isinstance(r.get("quantity"), bool))
    return rows, total, None


# ---------------------------------------------------------------- server address and HTTP

def normalize_server(value):
    value = (value or "").strip().rstrip("/")
    if value.endswith(API_PREFIX):
        value = value[:-len(API_PREFIX)].rstrip("/")
    if value and "://" not in value and PLACEHOLDER not in value:
        value = "http://" + value
    return value


def resolve_server(user):
    env = os.environ.get("LEGO_PARTS_SERVER", "").strip()
    if env:
        return normalize_server(env), "LEGO_PARTS_SERVER"
    if user and isinstance(user.get("server"), str) and user["server"].strip():
        return normalize_server(user["server"]), "user.json"
    return normalize_server(DEFAULT_SERVER), "built-in default"


def server_configured(server):
    return bool(server) and PLACEHOLDER not in server


def no_server_message():
    return ("No recognition server address is configured. Set the environment variable "
            "LEGO_PARTS_SERVER to the server's address, for example LEGO_PARTS_SERVER=http://203.0.113.7 "
            "(without /skill/v1), or download the client from <server>/skill/install/lego.py, which has "
            "the address built in.")


def bad_user_message(ctx):
    return ("The server {} does not recognise user ID {} (from {}). The results stored in {} belong to "
            "that ID, so no new ID was created. Check that the server address is right "
            "(LEGO_PARTS_SERVER, or \"server\" in user.json). To start over with a new, empty inventory, "
            "move {} somewhere else and run scan again.").format(
                ctx["server"], ctx["user_id"], ctx["home"] / "user.json", ctx["home"], ctx["home"])


def is_refused(reason):
    return isinstance(reason, ConnectionRefusedError) or getattr(reason, "errno", None) == errno.ECONNREFUSED


def error_from_response(status, raw):
    code = message = None
    try:
        err = json.loads(raw.decode("utf-8"))["error"]
        code, message = err.get("code"), err.get("message")
    except (ValueError, KeyError, TypeError, AttributeError):
        pass
    return ApiError(status, code or "http_{}".format(status), message or "HTTP {} from the server".format(status))


def http_request(method, url, body=None, headers=None, timeout=TIMEOUT_S):
    """One HTTP request -> (status, body bytes). Raises ApiError for HTTP errors and network failures."""
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("User-Agent", "lego-moc-skill/" + VERSION)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        try:
            raw = e.read()
        except (OSError, http.client.HTTPException):
            raw = b""
        raise error_from_response(e.code, raw)
    except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
        reason = getattr(e, "reason", e)
        host = urllib.parse.urlparse(url).netloc
        raise ApiError(None, "network", "cannot reach {}: {}".format(host, reason), refused=is_refused(reason))


def request_with_retry(method, url, body=None, headers=None, timeout=TIMEOUT_S, retry="get"):
    """retry="get": retry network errors and 5xx (idempotent requests only);
    retry="refused": retry only when the connection was refused, i.e. nothing was sent.
    A 503 not_ready is never retried here: the caller has its own, slower policy for it."""
    for attempt in range(GET_RETRIES + 1):
        try:
            return http_request(method, url, body, headers, timeout)
        except ApiError as e:
            if retry == "get":
                retryable = (e.status is None or e.status >= 500) and e.code != "not_ready"
            else:
                retryable = e.refused
            if not retryable or attempt == GET_RETRIES:
                raise
            time.sleep(2 ** attempt)  # 1, 2, 4 s


def api_raw(ctx, method, path, body=None, content_type=None, timeout=TIMEOUT_S, retry="get", auth=True,
            accept="application/json"):
    """Call {server}/skill/v1{path} -> (status, body bytes). A 401 becomes Fatal (never re-mint)."""
    headers = {"Accept": accept}
    if content_type:
        headers["Content-Type"] = content_type
    if auth:
        headers["X-Lego-User"] = ctx["user_id"]
    try:
        return request_with_retry(method, ctx["server"] + API_PREFIX + path, body, headers, timeout, retry)
    except ApiError as e:
        if auth and (e.status == 401 or e.code == "bad_user"):
            raise Fatal(bad_user_message(ctx))
        raise


def api(ctx, method, path, body=None, content_type=None, timeout=TIMEOUT_S, retry="get", auth=True):
    """Call {server}/skill/v1{path} and return the JSON object. A 401 becomes Fatal (never re-mint)."""
    status, raw = api_raw(ctx, method, path, body, content_type, timeout, retry, auth)
    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise ApiError(status, "bad_response", "the server sent an unexpected reply to {} {}".format(
            method, path.split("?")[0]))
    return data


# ---------------------------------------------------------------- user ID

def setup(ctx, need_server=True):
    ctx["user"] = load_user(ctx)
    ctx["user_id"] = ctx["user"]["user_id"] if ctx["user"] else None
    ctx["server"], ctx["server_source"] = resolve_server(ctx["user"])
    if need_server and not server_configured(ctx["server"]):
        raise Fatal(no_server_message())


def ensure_user(ctx):
    """Use the local user ID; create one on the server only when none exists locally."""
    if ctx["user_id"]:
        return
    path = ctx["home"] / "user.json"
    with file_lock(path, timeout=180.0):
        user = load_user(ctx)  # another agent may have created it meanwhile
        if user is None:
            say("No user ID on this computer yet; requesting one from", ctx["server"])
            resp = api(ctx, "POST", "/users", body=b"", retry="refused", auth=False)
            if not valid_id(resp.get("user_id")):
                raise Fatal("the server did not return a valid user ID")
            user = {"user_id": resp["user_id"], "server": ctx["server"],
                    "created_at": resp.get("created_at") or now_iso()}
            write_atomic(path, user)
            say("New user ID {} saved to {}".format(user["user_id"], path))
    ctx["user"], ctx["user_id"] = user, user["user_id"]


# ---------------------------------------------------------------- scan: photos and upload

def expand_photos(args):
    """PHOTO arguments (files, folders, glob patterns) -> unique list of files. Raises UsageError."""
    found = []
    for arg in args:
        raw = os.path.expanduser(arg)
        path = Path(raw)
        if path.is_dir():
            found += sorted(p for p in path.iterdir()
                            if p.is_file() and not p.name.startswith(".") and p.suffix.lower() in IMAGE_SUFFIXES)
        elif path.is_file():
            found.append(path)
        elif any(ch in arg for ch in "*?["):  # Windows shells do not expand globs
            matches = sorted(Path(m) for m in glob.glob(raw) if os.path.isfile(m))
            if not matches:
                raise UsageError("no files match " + arg)
            found += matches
        else:
            raise UsageError("file not found: " + arg)
    unique, seen = [], set()
    for path in found:
        key = os.path.normcase(os.path.abspath(str(path)))
        if key not in seen:
            seen.add(key)
            unique.append(path)
    if not unique:
        raise UsageError("no photos found in: " + " ".join(args))
    if len(unique) > MAX_PHOTOS:
        raise UsageError("{} photos given; one scan takes at most {}. Split them over several scan "
                         "commands.".format(len(unique), MAX_PHOTOS))
    return unique


def make_chunks(sized):
    """Group (path, size) into upload requests of at most CHUNK_FILES files / CHUNK_BYTES bytes."""
    chunks, current, current_bytes = [], [], 0
    for path, size in sized:
        if current and (len(current) >= CHUNK_FILES or current_bytes + size > CHUNK_BYTES):
            chunks.append(current)
            current, current_bytes = [], 0
        current.append(path)
        current_bytes += size
    if current:
        chunks.append(current)
    return chunks


def mime_type(name):
    ext = os.path.splitext(name)[1].lower()
    return MIME_TYPES.get(ext) or mimetypes.guess_type(name)[0] or "application/octet-stream"


def multipart_form(fields):
    """multipart/form-data body from (field name, filename or None, content type or None, bytes)
    -> (body, content type). A field with a filename is sent as a file part."""
    boundary = "lego-moc-" + uuid.uuid4().hex
    while any(boundary.encode("ascii") in data for _, _, _, data in fields):
        boundary = "lego-moc-" + uuid.uuid4().hex
    sep = b"--" + boundary.encode("ascii")
    out = []
    for name, filename, content_type, data in fields:
        disposition = 'Content-Disposition: form-data; name="' + name + '"'
        if filename is not None:
            quoted = (unicodedata.normalize("NFC", filename)
                      .replace('"', "%22").replace("\r", "%0D").replace("\n", "%0A"))
            disposition += '; filename="' + quoted + '"'
        out += [sep, b"\r\n", (disposition + "\r\n").encode("utf-8")]
        if content_type:
            out.append(("Content-Type: " + content_type + "\r\n").encode("ascii"))
        out += [b"\r\n", data, b"\r\n"]
    out += [sep, b"--\r\n"]
    return b"".join(out), "multipart/form-data; boundary=" + boundary


def multipart_body(files):
    """multipart/form-data body with one "photos" field per (filename, bytes) -> (body, content type)."""
    return multipart_form([("photos", name, mime_type(name), data) for name, data in files])


def read_files(rep, paths):
    files = []
    for path in paths:
        try:
            files.append((path.name, path.read_bytes()))
        except OSError as e:
            rep["rejected"].append({"filename": path.name, "code": "unreadable", "message": str(e)})
    return files


def upload(ctx, rep, photos):
    sized = []
    for path in photos:
        try:
            size = path.stat().st_size
        except OSError as e:
            rep["rejected"].append({"filename": path.name, "code": "unreadable", "message": str(e)})
            continue
        if size > MAX_PHOTO_BYTES:
            rep["rejected"].append({"filename": path.name, "code": "photo_too_large",
                                    "message": "larger than 25 MB; not uploaded"})
        else:
            sized.append((path, size))
    chunks = make_chunks(sized)
    if chunks:
        say("Uploading {} photo(s) in {} request(s) to {} ...".format(len(sized), len(chunks), ctx["server"]))
    for n, chunk in enumerate(chunks, 1):
        label = "request {}/{}".format(n, len(chunks))
        files = read_files(rep, chunk)
        if not files:
            continue
        body, content_type = multipart_body(files)
        try:
            resp = api(ctx, "POST", "/scans", body, content_type, timeout=UPLOAD_TIMEOUT_S, retry="refused")
        except ApiError as e:
            names = [name for name, _ in files]
            if e.status in (400, 413) and e.code in SKIP_CHUNK_CODES:
                rep["rejected"] += [{"filename": name, "code": e.code, "message": e.message} for name in names]
                say("  {}: all {} file(s) rejected: {}".format(label, len(names), describe(e)))
                continue
            upload_stopped(rep, label, e, names + [p.name for c in chunks[n:] for p in c])
            return
        record_scan(ctx, rep, resp, label)


def upload_stopped(rep, label, err, not_sent):
    rep["not_uploaded"] += not_sent
    msg = "upload stopped at {}: {}. {} photo(s) were not uploaded: {}.".format(
        label, describe(err), len(not_sent), ", ".join(not_sent))
    if rep["submitted"]:
        msg += " The {} photo(s) accepted before that are being processed.".format(len(rep["submitted"]))
    if (err.status is None and not err.refused) or (err.status or 0) >= 500 or err.code == "bad_response":
        msg += (" The server may still have received {}; if so, its parts will show up in parts.json later, "
                "and uploading those photos again would count them twice.".format(label))
    rep["errors"].append(msg)
    say("ERROR:", msg)


def record_scan(ctx, rep, resp, label):
    tasks = [t for t in resp.get("tasks") or [] if isinstance(t, dict) and valid_id(t.get("task_id"))]
    ids = [t["task_id"] for t in tasks]
    if ids:
        update_pending(ctx, add=ids)  # first: the IDs must survive anything that happens next
    for task in tasks:
        rep["tasks"][task["task_id"]] = task
    add_unique(rep["submitted"], ids)
    add_unique(rep["scope"], ids)
    rejected = [r for r in resp.get("rejected") or [] if isinstance(r, dict)]
    rep["rejected"] += [{"filename": r.get("filename"), "code": r.get("code"), "message": r.get("message")}
                        for r in rejected]
    if isinstance(resp.get("gpu"), dict):
        rep["gpu"] = resp["gpu"]
    line = "  {}: {} accepted".format(label, len(ids))
    if rejected:
        line += ", {} rejected".format(len(rejected))
    if resp.get("queue_ahead") is not None:
        line += ", {} photo(s) ahead in the queue".format(resp["queue_ahead"])
    if resp.get("eta_s") is not None:
        line += ", estimate ~{} s".format(resp["eta_s"])
    say(line)


# ---------------------------------------------------------------- polling and collecting

def poll_seconds(value, default):
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return default
    return min(max(seconds, POLL_MIN_S), POLL_MAX_S)


def query_tasks(ctx, ids):
    """GET /tasks in batches -> (tasks by id, missing ids, gpu, seconds until next poll)."""
    tasks, missing, gpu, poll_s = {}, [], None, POLL_DEFAULT_S
    for i in range(0, len(ids), IDS_PER_QUERY):
        batch = ids[i:i + IDS_PER_QUERY]
        resp = api(ctx, "GET", "/tasks?ids=" + ",".join(batch))
        for task in resp.get("tasks") or []:
            if isinstance(task, dict) and task.get("task_id") in batch:
                tasks[task["task_id"]] = task
        missing += [m for m in resp.get("missing") or [] if m in batch]
        if isinstance(resp.get("gpu"), dict):
            gpu = resp["gpu"]
        poll_s = poll_seconds(resp.get("poll_after_s"), POLL_DEFAULT_S)
    return tasks, missing, gpu, poll_s


def forget_task(ctx, rep, tid):
    if tid in rep["missing"]:
        return
    update_pending(ctx, remove=[tid])
    rep["missing"].append(tid)
    msg = "task {} is unknown to the server for this user ID; removed it from pending.json".format(tid)
    rep["errors"].append(msg)
    say("ERROR:", msg)


def collect(ctx, rep, tid):
    """Fetch a finished task's full result into tasks/<id>.json and drop it from pending.json."""
    try:
        detail = api(ctx, "GET", "/tasks/" + tid)
    except ApiError as e:
        if e.status == 404:
            forget_task(ctx, rep, tid)
        else:
            say("WARNING: could not fetch the result of task {}: {}; will retry".format(tid, describe(e)))
        return
    if detail.get("status") not in TERMINAL:
        return
    write_atomic(tasks_dir(ctx) / (tid + ".json"), detail)
    update_pending(ctx, remove=[tid])
    rep["tasks"][tid] = detail
    add_unique(rep["collected"], [tid])
    add_unique(rep["scope"], [tid])


def poll_round(ctx, rep, open_ids, untracked):
    """One status query over open_ids plus pending.json; collects what finished. Returns poll interval."""
    ids = list(dict.fromkeys(open_ids + read_pending(ctx)))
    try:
        tasks, missing, gpu, poll_s = query_tasks(ctx, ids)
    except ApiError as e:
        if rep["poll_error"] != describe(e):
            say("WARNING: could not check task status:", describe(e))
        rep["poll_error"] = describe(e)
        return POLL_DEFAULT_S
    rep["poll_error"] = None
    if gpu:
        rep["gpu"] = gpu
    for tid in missing:
        forget_task(ctx, rep, tid)
    for tid, task in tasks.items():
        if tid not in rep["collected"]:
            rep["tasks"][tid] = task
        if task.get("status") in TERMINAL:
            collect(ctx, rep, tid)
        elif tid in untracked:  # named on the command line but not in pending.json: track it from now on
            update_pending(ctx, add=[tid])
            untracked.discard(tid)
    return poll_s


def still_open(rep, ids):
    return [t for t in ids if t not in rep["collected"] and t not in rep["missing"]]


def track(ctx, rep, watch, wait_s):
    """Poll until every task in watch is collected or wait_s seconds have passed."""
    add_unique(rep["scope"], watch)
    untracked = set(watch) - set(read_pending(ctx))
    deadline = time.monotonic() + wait_s
    shown = {}
    while still_open(rep, watch):
        poll_s = poll_round(ctx, rep, still_open(rep, watch), untracked)
        if wait_s > 0:
            show_progress(ctx, rep, watch, shown)
        left = deadline - time.monotonic()
        if not still_open(rep, watch) or left <= 0:
            break
        time.sleep(min(poll_s, left))


def show_progress(ctx, rep, watch, shown):
    """Print one line, only when a status, queue position or the GPU state changed."""
    tasks = [rep["tasks"].get(t, {"task_id": t}) for t in watch if t not in rep["missing"]]
    gpu = rep.get("gpu") or {}
    signature = (tuple((t.get("task_id"), t.get("status"), t.get("queue_ahead")) for t in tasks), gpu.get("state"))
    if signature == shown.get("signature"):
        return
    shown["signature"] = signature
    finished = [t for t in tasks if t.get("status") in TERMINAL]
    failed = [t for t in finished if t.get("status") == "failed"]
    bits = ["[{}] {}/{} done".format(elapsed(ctx), len(finished), len(tasks))
            + (" ({} failed)".format(len(failed)) if failed else "")]
    active = [t for t in tasks if t.get("status") not in TERMINAL + ("queued", None)]
    for task in active[:2]:
        text = "{} {}".format(task.get("filename") or task.get("task_id"), task.get("status"))
        progress = task.get("progress")
        if isinstance(progress, dict) and progress.get("total"):
            text += " {}/{}".format(progress.get("done"), progress.get("total"))
        bits.append(text)
    if len(active) > 2:
        bits.append("+{} more in progress".format(len(active) - 2))
    queued = [t for t in tasks if t.get("status") == "queued"]
    if queued:
        ahead = [t.get("queue_ahead") for t in queued if isinstance(t.get("queue_ahead"), int)]
        bits.append("{} queued".format(len(queued)) + (" ({} ahead)".format(min(ahead)) if ahead else ""))
    if gpu.get("state"):
        bits.append("GPU {}".format(gpu["state"]) + (" ~{}s".format(gpu["eta_s"]) if gpu.get("eta_s") else ""))
    say(" · ".join(bits))


# ---------------------------------------------------------------- parts.json and images

def refresh_parts(ctx, rep, images):
    data = api(ctx, "GET", "/parts")
    if not isinstance(data.get("items"), list):
        raise ApiError(200, "bad_response", "GET /parts returned no items list")
    data["fetched_at"] = now_iso()
    data["local_pending_tasks"] = len(read_pending(ctx))
    if images:
        download_images(ctx, rep, data["items"])
    path = parts_path(ctx)
    try:
        old = path.read_bytes()
    except FileNotFoundError:
        old = None
    if old is not None and parts_differ(old, data):
        write_atomic(path.with_name("parts.json.bak"), old)
        say("Previous parts.json kept as", path.with_name("parts.json.bak"))
    write_atomic(path, data)
    rep["parts_refreshed"] = True
    rep["server_pending"] = data.get("pending_tasks")


def parts_differ(old_bytes, new):
    try:
        old = json.loads(old_bytes.decode("utf-8-sig"))
    except ValueError:
        return True
    if not isinstance(old, dict):
        return True
    return ({k: v for k, v in old.items() if k != "fetched_at"}
            != {k: v for k, v in new.items() if k != "fetched_at"})


def ldraw_image_name(row, url):
    part, color = row.get("ldraw_part"), row.get("ldraw_color")
    if part is None or color is None:
        match = re.search(r"/ldraw/([^/]+)/([^/]+?)\.png$", urllib.parse.urlparse(url).path)
        if not match:
            return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16] + ".png"
        part, color = urllib.parse.unquote(match.group(1)), match.group(2)
    return "{}_{}.png".format(safe_name(part), safe_name(color))


def crop_image_name(row, url):
    match = re.search(r"/tasks/([^/]+)/crops/([^/]+?)\.[A-Za-z0-9]+$", urllib.parse.urlparse(url).path)
    if match:
        return "{}_{}.jpg".format(safe_name(match.group(1)), safe_name(match.group(2)))
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16] + ".jpg"


def fetch_image(ctx, rep, url, folder, name_for):
    """Download url into folder (skipped when present) -> absolute path, or None on any failure."""
    if not isinstance(url, str) or not url:
        return None
    url = urllib.parse.urljoin(ctx["server"] + "/", url)
    if urllib.parse.urlparse(url).scheme not in ("http", "https"):
        return None
    dest = folder / name_for(url)
    if dest.is_file() and dest.stat().st_size > 0:
        rep["images"]["present"] += 1
        return str(dest)
    try:
        status, body = request_with_retry("GET", url)  # media needs no user header
        if not body:
            raise ApiError(status, "empty", "empty file")
        write_atomic(dest, body)
    except (ApiError, OSError) as e:
        rep["images"]["failed"].append("{} ({})".format(url, describe(e) if isinstance(e, ApiError) else e))
        return None
    rep["images"]["downloaded"] += 1
    return str(dest)


def download_images(ctx, rep, items):
    root = ctx["home"] / "images"
    for row in items:
        if isinstance(row, dict):
            row["local_ldraw_image"] = fetch_image(ctx, rep, row.get("ldraw_image_url"), root / "ldraw",
                                                   lambda url, r=row: ldraw_image_name(r, url))
            row["local_crop"] = fetch_image(ctx, rep, row.get("crop_url"), root / "crops",
                                            lambda url, r=row: crop_image_name(r, url))
    info = rep["images"]
    say("Images: {} downloaded, {} already present, {} failed (in {})".format(
        info["downloaded"], info["present"], len(info["failed"]), root))
    for failure in info["failed"][:3]:
        say("WARNING: image not downloaded:", failure)
    if len(info["failed"]) > 3:
        say("WARNING: ... and {} more images not downloaded".format(len(info["failed"]) - 3))


# ---------------------------------------------------------------- summary and RESULT

def new_report(command):
    return {"command": command, "scope": [], "tasks": {}, "collected": [], "missing": [], "submitted": [],
            "rejected": [], "not_uploaded": [], "errors": [], "poll_error": None, "gpu": None,
            "parts_refreshed": False, "server_pending": None,
            "images": {"downloaded": 0, "present": 0, "failed": []},
            "moc": {"targets": [], "guide_file": None, "connectors_file": None, "connector_parts": None,
                    "connector_requests": 0, "unsupported_parts": []}}


def classify(rep):
    done = [t for t in rep["collected"] if rep["tasks"][t].get("status") == "done"]
    failed = [t for t in rep["collected"] if rep["tasks"][t].get("status") == "failed"]
    return done, failed, still_open(rep, rep["scope"])


def task_messages(rep, field, status):
    out = []
    for tid in rep["collected"]:
        task = rep["tasks"][tid]
        info = task.get(field)
        if task.get("status") == status and isinstance(info, dict):
            out.append({"task_id": tid, "filename": task.get("filename"),
                        "code": info.get("code"), "message": info.get("message")})
    return out


def task_line(rep, tid):
    task = rep["tasks"].get(tid, {})
    status = task.get("status") or "not checked"
    detail = ""
    if tid in rep["collected"] and status == "done":
        detail = "{} pieces found, {} accepted".format(task.get("pieces_found"), task.get("pieces_accepted"))
    elif tid not in rep["collected"]:
        progress = task.get("progress")
        if isinstance(progress, dict) and progress.get("total"):
            detail = "{}/{}".format(progress.get("done"), progress.get("total"))
        elif status == "queued" and task.get("queue_ahead") is not None:
            detail = "{} ahead in queue".format(task["queue_ahead"])
        if status in TERMINAL:
            status += " (result not fetched yet)"
    lines = ["  " + "  ".join(s for s in ("{:<13}".format(status), task.get("filename") or "?", detail,
                                         "[{}]".format(tid)) if s)]
    for field, label in (("notice", "NOTICE"), ("error", "ERROR")):
        info = task.get(field)
        if tid in rep["collected"] and isinstance(info, dict):
            lines.append("      {} [{}] {}".format(label, info.get("code"), info.get("message")))
    return lines


def row_line(row):
    flags = ""
    if row.get("low_confidence"):
        flags += " [low confidence]"
    if row.get("designable") is False:
        flags += " [not designable]"
    return "  {:>4} x {:<18} {} ({}){}".format(row.get("quantity"), str(row.get("key")), row.get("name") or "?",
                                               row.get("color_name") or "colour ?", flags)


def print_summary(ctx, rep):
    done, failed, pending = classify(rep)
    say("")
    say("== Summary ==")
    if rep["scope"]:
        say("Tasks: {} done, {} failed, {} still running".format(len(done), len(failed), len(pending)))
        for tid in rep["scope"]:
            if tid not in rep["missing"]:
                for line in task_line(rep, tid):
                    say(line)
    for r in rep["rejected"]:
        say("  REJECTED {}: [{}] {}".format(r.get("filename"), r.get("code"), r.get("message")))
    if rep["not_uploaded"]:
        say("  NOT UPLOADED:", ", ".join(rep["not_uploaded"]))
    rows, total, problem = load_inventory(ctx)
    if problem:
        say("Inventory:", problem)
    else:
        say("Inventory ({}): {} pieces in {} rows; {} low-confidence, {} not designable".format(
            "refreshed from server" if rep["parts_refreshed"] else "local file, not refreshed", total, len(rows),
            sum(1 for r in rows if r.get("low_confidence")), sum(1 for r in rows if r.get("designable") is False)))
        for row in rows[:ROWS_SHOWN]:
            say(row_line(row))
        if len(rows) > ROWS_SHOWN:
            say("  ... {} more rows in parts.json".format(len(rows) - ROWS_SHOWN))
    if isinstance(rep["server_pending"], int) and rep["server_pending"] > len(pending):
        say("Note: the server reports {} unfinished task(s) for this user ID.".format(rep["server_pending"]))
    say("Files:")
    say("  parts.json:", parts_path(ctx))
    say("  tasks dir: ", tasks_dir(ctx))
    if rep["errors"]:
        say("FINISHED WITH ERRORS: see the ERROR lines above.")
    if pending:
        say("STILL RUNNING: {} of {} task(s) not finished; parts.json is incomplete. Run: {}".format(
            len(pending), len(rep["scope"]), script_cmd("status --wait 300")))
    elif rep["scope"] and not rep["errors"]:
        say("All tasks finished.")


def exit_code(rep):
    if rep["errors"]:
        return EXIT_ERROR
    return EXIT_PENDING if classify(rep)[2] else EXIT_OK


def emit_result(ctx, rep, code):
    try:
        if (rep.get("command") or "").startswith("moc"):
            obj = moc_result(ctx, rep, code)
        else:
            obj = scan_result(ctx, rep, code)
    except Exception as e:  # the RESULT line must always be printed
        obj = {"exit_code": code, "errors": rep.get("errors", []) + ["could not build RESULT: {}".format(e)]}
    print("RESULT " + json.dumps(obj, ensure_ascii=True, separators=(",", ":")), flush=True)


def scan_result(ctx, rep, code):
    done, failed, pending = classify(rep)
    rows, total, _ = load_inventory(ctx)
    return {
        "exit_code": code, "command": rep["command"],
        "user_id": ctx.get("user_id"), "server": ctx.get("server"),
        "done": len(done), "failed": len(failed), "pending": len(pending),
        "pending_task_ids": pending, "task_ids": rep["scope"], "missing_task_ids": rep["missing"],
        "local_pending_tasks": len(load_pending(ctx)[0]), "server_pending_tasks": rep["server_pending"],
        "rejected": rep["rejected"], "not_uploaded": rep["not_uploaded"],
        "notices": task_messages(rep, "notice", "done"), "task_errors": task_messages(rep, "error", "failed"),
        "errors": rep["errors"],
        "parts_file": str(parts_path(ctx)), "parts_refreshed": rep["parts_refreshed"],
        "tasks_dir": str(tasks_dir(ctx)), "home": str(ctx["home"]),
        "total_quantity": total, "distinct_parts": len(rows),
    }


# ---------------------------------------------------------------- moc: local input files

def arg_path(text):
    return Path(os.path.abspath(os.path.expanduser(text)))


def positive_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def num(value):
    """A coordinate as short text: 40.0 -> "40", -12.5 -> "-12.5"."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "?"
    if float(value).is_integer():
        return str(int(value))
    return ("%.3f" % value).rstrip("0").rstrip(".")


def read_model(arg):
    """Check the model file named on the command line -> (path, bytes to send, number of part lines).
    Problems are usage errors: the file is the command's argument."""
    path = arg_path(arg)
    if not path.is_file():
        raise UsageError("model file not found: " + arg)
    size = path.stat().st_size
    if size > MAX_MODEL_BYTES:
        raise UsageError("{} is {} bytes; a model file may be at most 1 MB ({} bytes)".format(
            arg, size, MAX_MODEL_BYTES))
    try:
        data = path.read_bytes()
    except OSError as e:
        raise UsageError("cannot read {}: {}".format(arg, e))
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]  # a BOM would hide a "1" at the start of the first line
    try:
        if b"\x00" in data:
            raise ValueError("NUL byte")
        text = data.decode("utf-8")
    except ValueError:
        raise UsageError("{} is not a text file; an LDraw model is plain UTF-8 text".format(arg))
    count = sum(1 for line in text.splitlines() if PART_LINE_RE.match(line))
    if not count:
        raise UsageError("{} has no part lines. Each part is one line: "
                         "1 <colour> x y z a b c d e f g h i <part>.dat".format(arg))
    return path, data, count


def read_design(arg):
    path = arg_path(arg)
    if not path.is_file():
        raise UsageError("design file not found: " + arg)
    try:
        data = read_json(path)
    except (OSError, ValueError) as e:
        raise UsageError("{} is not valid JSON ({})".format(arg, e))
    if not isinstance(data, dict):
        raise UsageError('{} must be a JSON object, e.g. {{"title": "...", "motions": [...]}}'.format(arg))
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


def read_parts_upload(ctx, arg):
    """The inventory sent with a model -> JSON bytes, without the local-only local_* fields.
    A file named with --parts-file is an argument (usage error); the default parts.json is local state (error)."""
    explicit = arg is not None
    path = arg_path(arg) if explicit else parts_path(ctx)
    problem = UsageError if explicit else Fatal
    if not path.is_file():
        if explicit:
            raise UsageError("parts file not found: " + arg)
        raise Fatal("no inventory at {}. A model is checked against the parts you own: scan them first ({}), "
                    "or pass --parts-file.".format(path, script_cmd("scan PHOTO...")))
    try:
        data = read_json(path)
    except (OSError, ValueError) as e:
        raise problem("{} is not valid JSON ({})".format(path, e))
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise problem("{} has no items list, so it is not an inventory".format(path))
    data = dict(data, items=[{k: v for k, v in row.items() if not str(k).startswith("local_")}
                             if isinstance(row, dict) else row for row in items])
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    if len(body) > MAX_PARTS_BYTES:
        raise problem("{} is larger than 2 MB, the server's limit for an inventory".format(path))
    return body


# ---------------------------------------------------------------- moc: guide and connector sheets

def not_ready_retry(ctx, call):
    """call() -> its result. While the server answers 503 not_ready, retry every NOT_READY_RETRY_S
    seconds, for at most NOT_READY_MAX_S seconds counted over the whole command."""
    while True:
        try:
            return call()
        except ApiError as e:
            if e.code != "not_ready":
                raise
            now = time.monotonic()
            since = ctx.setdefault("not_ready_since", now)
            if now - since >= NOT_READY_MAX_S:
                raise ApiError(e.status, e.code, "{} (still not ready after {} s of retrying)".format(
                    e.message, int(NOT_READY_MAX_S)))
            say("[{}] the server is not ready yet ({}); retrying in {} s, for up to {} s".format(
                elapsed(ctx), e.message, num(NOT_READY_RETRY_S), int(NOT_READY_MAX_S)))
            time.sleep(NOT_READY_RETRY_S)


def connector_part_ids(rows):
    """Distinct LDraw part numbers of the inventory rows, in inventory order; rows without one are skipped."""
    ids = []
    for row in rows:
        part = row.get("ldraw_part")
        if isinstance(part, int) and not isinstance(part, bool):
            part = str(part)
        if isinstance(part, str) and part.strip():
            ids.append(part.strip())
    return list(dict.fromkeys(ids))


def cmd_moc_guide(ctx, rep, args):
    m = rep["moc"]
    inventory = arg_path(args.parts_file) if args.parts_file is not None else parts_path(ctx)
    if args.parts_file is not None and not inventory.is_file():
        raise UsageError("parts file not found: " + args.parts_file)
    setup(ctx)
    folder = moc_dir(ctx)
    _, guide = not_ready_retry(ctx, lambda: api_raw(ctx, "GET", "/moc/guide", auth=False,
                                                    accept="text/markdown, text/plain, */*"))
    if not guide.strip():
        raise ApiError(200, "bad_response", "the server sent an empty design guide")
    write_atomic(folder / "DESIGN_GUIDE.md", guide)
    m["guide_file"] = str(folder / "DESIGN_GUIDE.md")
    say("Design guide saved to", m["guide_file"])
    rows, _, problem = load_inventory(ctx, inventory)
    ids = connector_part_ids(rows)
    if not problem and not ids:
        problem = "{} has no rows with an LDraw part number".format(inventory.name)
    if problem:
        msg = ("connectors.txt was not fetched: connector sheets are fetched for the parts in an inventory, and "
               "{} ({}). Scan your parts first ({}), or pass --parts-file.".format(
                   problem, inventory, script_cmd("scan PHOTO...")))
        rep["errors"].append(msg)
        say("ERROR:", msg)
        return EXIT_ERROR
    batches = [ids[i:i + CONNECTOR_BATCH] for i in range(0, len(ids), CONNECTOR_BATCH)]
    say("Fetching connector sheets for {} distinct part(s) in {} request(s) ...".format(len(ids), len(batches)))
    texts = []
    for n, batch in enumerate(batches, 1):
        body = json.dumps({"parts": batch}).encode("utf-8")
        try:
            _, raw = not_ready_retry(ctx, lambda b=body: api_raw(
                ctx, "POST", "/moc/connectors", b, "application/json", auth=False, accept="text/plain, */*"))
        except ApiError as e:
            raise Fatal("could not fetch connector sheets (request {}/{}): {}. connectors.txt was not "
                        "updated.".format(n, len(batches), describe(e)))
        m["connector_requests"] += 1
        texts.append(raw.decode("utf-8", "replace").strip("\r\n"))
    text = "\n\n".join(t for t in texts if t) + "\n"
    path = folder / "connectors.txt"
    write_atomic(path, text.encode("utf-8"))
    unsupported = list(dict.fromkeys(UNSUPPORTED_RE.findall(text)))
    m.update(connectors_file=str(path), connector_parts=len(ids), unsupported_parts=unsupported)
    say("Connector sheets for {} part(s) saved to {}".format(len(ids) - len(unsupported), path))
    if unsupported:
        say("WARNING: {} part(s) in the inventory are not supported for design (no connector sheet; don't build "
            "with them): {}{}".format(len(unsupported), ", ".join(unsupported[:20]),
                                      " ..." if len(unsupported) > 20 else ""))
    say("Next: read both files, write an LDraw model using only designable rows of {} (at most `quantity` of "
        "each), then run: {}".format(inventory, script_cmd("moc submit model.ldr" + (
            ' --parts-file "{}"'.format(inventory) if args.parts_file is not None else ""))))
    return EXIT_OK


# ---------------------------------------------------------------- moc: submit and track

def new_target(moc_id, version=None, data=None, pinned=False, poll_s=POLL_DEFAULT_S):
    """One MOC being checked. pinned: poll exactly this version (submit) rather than the latest (status)."""
    return {"moc_id": moc_id, "version": version, "pinned": pinned, "data": data, "done": False,
            "missing": False, "untracked": False, "poll_error": None, "poll_s": poll_s,
            "report_file": None, "model_file": None}


def submit_failed(err, moc_id):
    if err.status == 404 and moc_id:
        return ("MOC {} is not one of this user ID's MOCs ({}). Check the ID, or leave out --moc to start "
                "a new MOC.".format(moc_id, describe(err)))
    if (err.status is None and not err.refused) or (err.status or 0) >= 500 or err.code == "bad_response":
        msg = "submitting failed: {}. The server may still have received the model".format(describe(err))
        return msg + ("; check with {} before submitting again".format(script_cmd("moc status " + moc_id))
                      if moc_id else "; submitting it again may create a second MOC")
    return "the model was not accepted: " + describe(err)


def cmd_moc_submit(ctx, rep, args):
    if args.moc is not None and not valid_id(args.moc):
        raise UsageError("not a MOC ID: " + args.moc)
    title = args.title.strip() if args.title is not None else None
    if title and len(title) > MAX_TITLE_CHARS:
        raise UsageError("--title has {} characters; at most {}".format(len(title), MAX_TITLE_CHARS))
    model, model_bytes, part_lines = read_model(args.model)
    design = read_design(args.design) if args.design is not None else None
    parts_body = read_parts_upload(ctx, args.parts_file)
    setup(ctx)
    ensure_user(ctx)
    fields = [("model", model.name, "text/plain; charset=utf-8", model_bytes),
              ("parts", "parts.json", "application/json", parts_body)]
    if design is not None:
        fields.append(("design", "design.json", "application/json", design))
    if args.moc:
        fields.append(("moc_id", None, None, args.moc.encode("ascii")))
    if title:
        fields.append(("title", None, None, title.encode("utf-8")))
    body, content_type = multipart_form(fields)
    say("Submitting {} ({} part line(s)){} to {} ...".format(
        model, part_lines, " as a new version of MOC " + args.moc if args.moc else "", ctx["server"]))
    try:
        resp = api(ctx, "POST", "/mocs", body, content_type, timeout=UPLOAD_TIMEOUT_S, retry="refused")
    except ApiError as e:
        raise Fatal(submit_failed(e, args.moc))
    moc_id, version = resp.get("moc_id"), resp.get("version")
    if not valid_id(moc_id):
        raise Fatal("the server answered the submission without a valid MOC ID")
    target = new_target(moc_id, version if positive_int(version) else None, resp, pinned=True,
                        poll_s=poll_seconds(resp.get("poll_after_s"), POLL_DEFAULT_S))
    target["resubmit_model"] = str(model)
    rep["moc"]["targets"].append(target)  # so that RESULT names the MOC whatever happens next
    update_moc_pending(ctx, add=[moc_id])  # first on disk: the ID must survive anything that happens next
    if target["version"] is None:
        raise Fatal("the server accepted the model as MOC {} but sent no version number; check it with {}".format(
            moc_id, script_cmd("moc status " + moc_id)))
    copy = moc_dir(ctx) / moc_id / "v{}.ldr".format(version)
    write_atomic(copy, model_bytes)
    target["model_file"] = str(copy)
    line = "Accepted as MOC {} version {}".format(moc_id, version)
    if resp.get("queue_ahead") is not None:
        line += ", {} ahead in the queue".format(resp["queue_ahead"])
    if resp.get("eta_s") is not None:
        line += ", estimate ~{} s".format(resp["eta_s"])
    say(line)
    say("Copy of the submitted file (the report's line numbers refer to it):", copy)
    if args.no_wait:
        say("Not waiting (--no-wait). The MOC ID is saved in", moc_pending_path(ctx))
    else:
        say("Waiting for the check (up to {} s; stopping early is safe, the MOC ID is saved in "
            "moc/pending.json) ...".format(args.wait))
        track_mocs(ctx, rep, args.wait)
    finish_moc(ctx, rep)
    return moc_exit_code(rep)


def cmd_moc_status(ctx, rep, args):
    if args.moc_id is not None and not valid_id(args.moc_id):
        raise UsageError("not a MOC ID: " + args.moc_id)
    setup(ctx)
    pending = read_moc_pending(ctx)
    ids = [args.moc_id] if args.moc_id is not None else pending
    if not ctx["user_id"]:
        if ids:
            raise Fatal("{} is missing, so MOC {} cannot be checked: a MOC belongs to the user ID that "
                        "submitted it".format(ctx["home"] / "user.json", ", ".join(ids)))
        say("No user ID on this computer yet ({}), so there is no MOC to check.".format(ctx["home"] / "user.json"))
        return EXIT_OK
    if not ids:
        say("No pending MOC submissions; nothing was checked.")
        show_recent_mocs(ctx)
        return EXIT_OK
    for moc_id in ids:
        target = new_target(moc_id)
        target["untracked"] = moc_id not in pending
        rep["moc"]["targets"].append(target)
    track_mocs(ctx, rep, args.wait)
    finish_moc(ctx, rep)
    return moc_exit_code(rep)


def open_targets(rep):
    return [t for t in rep["moc"]["targets"] if not t["done"] and not t["missing"]]


def track_mocs(ctx, rep, wait_s):
    """Poll the open MOCs until each reached a terminal state or wait_s seconds have passed."""
    deadline = time.monotonic() + wait_s
    shown = {}
    while open_targets(rep):
        for target in open_targets(rep):
            fetch_moc(ctx, rep, target)
        if wait_s > 0:
            show_moc_progress(ctx, rep, shown)
        left = deadline - time.monotonic()
        waiting = open_targets(rep)
        if not waiting or left <= 0:
            break
        time.sleep(min(min(t["poll_s"] for t in waiting), left))


def fetch_moc(ctx, rep, t):
    path = "/mocs/" + t["moc_id"] + ("?version={}".format(t["version"]) if t["pinned"] else "")
    try:
        data = api(ctx, "GET", path)
    except ApiError as e:
        if e.status == 404:
            return forget_moc(ctx, rep, t)
        if t["poll_error"] != describe(e):
            say("WARNING: could not check MOC {}: {}".format(t["moc_id"], describe(e)))
        t["poll_error"] = describe(e)
        return
    t["poll_error"] = None
    t["data"] = data
    if positive_int(data.get("version")):
        t["version"] = data["version"]
    t["poll_s"] = poll_seconds(data.get("poll_after_s"), t["poll_s"])
    if data.get("status") in MOC_TERMINAL:
        collect_moc(ctx, t, data)
    elif t["untracked"]:  # named on the command line but not in moc/pending.json: track it from now on
        update_moc_pending(ctx, add=[t["moc_id"]])
        t["untracked"] = False


def forget_moc(ctx, rep, t):
    t["missing"] = True
    tracked = t["moc_id"] in read_moc_pending(ctx)
    update_moc_pending(ctx, remove=[t["moc_id"]])
    msg = "MOC {}{} is unknown to the server for this user ID".format(
        t["moc_id"], " version {}".format(t["version"]) if t["pinned"] else "")
    if tracked:
        msg += "; removed it from moc/pending.json"
    rep["errors"].append(msg)
    say("ERROR:", msg)


def collect_moc(ctx, t, data):
    """A terminal answer: save it as report.v<N>.json and result.json, then stop tracking the MOC."""
    folder = moc_dir(ctx) / t["moc_id"]
    if t["version"]:
        report = folder / "report.v{}.json".format(t["version"])
        write_atomic(report, data)
        t["report_file"] = str(report)
    write_atomic(folder / "result.json", data)
    latest = data.get("latest_version")
    if not (positive_int(latest) and t["version"] and latest > t["version"]):
        update_moc_pending(ctx, remove=[t["moc_id"]])  # kept while a newer version still has to be fetched
    t["done"] = True


def moc_state_text(data):
    status = data.get("status") or "unknown"
    extra = []
    if status == "queued" and data.get("queue_ahead") is not None:
        extra.append("{} ahead".format(data["queue_ahead"]))
    if status not in MOC_TERMINAL and data.get("eta_s") is not None:
        extra.append("~{} s".format(data["eta_s"]))
    return status + (" (" + ", ".join(extra) + ")" if extra else "")


def show_moc_progress(ctx, rep, shown):
    """Print a line for each MOC whose status or queue position changed."""
    for t in rep["moc"]["targets"]:
        data = t["data"] or {}
        signature = (data.get("status"), data.get("queue_ahead"), t["version"])
        if t["missing"] or shown.get(t["moc_id"]) == signature:
            continue
        shown[t["moc_id"]] = signature
        say("[{}] MOC {} v{}: {}".format(elapsed(ctx), t["moc_id"], t["version"] or "?", moc_state_text(data)))


def issues_of(data):
    """(report dict or None, fail issues, other issues) of a GET /mocs answer."""
    report = data.get("report") if isinstance(data.get("report"), dict) else None
    issues = [i for i in (report or {}).get("issues") or [] if isinstance(i, dict)]
    fails = [i for i in issues if i.get("severity") == "fail"]
    return report, fails, [i for i in issues if i.get("severity") != "fail"]


def part_label(p):
    if positive_int(p.get("line")):
        return "line {}".format(p["line"])
    return "part #{}".format(p["index"]) if isinstance(p.get("index"), int) else "part"


def part_text(p):
    pos = p.get("pos")
    where = ("({})".format(", ".join(num(v) for v in pos)) if isinstance(pos, list) and len(pos) == 3
             else "(position not given)")
    color = p.get("color")
    return "{}: {} colour {} at {}".format(part_label(p), p.get("part") or "?", "?" if color is None else color,
                                           where)


def issue_parts(issue):
    return [p for p in issue.get("parts") or [] if isinstance(p, dict)]


def print_warnings(warnings):
    if not warnings:
        return
    say("Warnings ({}): they do not fail the model, but tell the user:".format(len(warnings)))
    for issue in warnings:
        parts = issue_parts(issue)
        labels = [part_label(p) for p in parts[:ISSUE_PARTS_SHOWN]]
        if len(parts) > ISSUE_PARTS_SHOWN:
            labels.append("+{} more".format(len(parts) - ISSUE_PARTS_SHOWN))
        say("  WARNING {}: {}{}".format(issue.get("rule") or "?", issue.get("message") or "",
                                        " (" + ", ".join(labels) + ")" if labels else ""))


def error_detail(data, report):
    err = data.get("error")
    if isinstance(err, dict) and err.get("message"):
        return "[{}] {}".format(err.get("code"), err["message"]) if err.get("code") else err["message"]
    if isinstance(err, str) and err:
        return err
    return (report or {}).get("summary") or ""


def print_moc(ctx, t):
    data = t["data"] or {}
    status = data.get("status")
    report, fails, warnings = issues_of(data)
    head = "MOC {} version {}".format(t["moc_id"], t["version"] or "?")
    model = '"{}"'.format(t["resubmit_model"]) if t.get("resubmit_model") else "<model.ldr>"
    resubmit = script_cmd("moc submit {} --moc {}".format(model, t["moc_id"]))
    say("")
    if status == "passed":
        say("== {}: PASSED ==".format(head))
    elif status == "failed":
        say("== {}: NOT PASSED - {} ==".format(head, (report or {}).get("summary") or "{} fail(s), {} warning(s)".format(
            len(fails), len(warnings))))
    elif status == "error":
        say("== {}: SERVER ERROR ==".format(head))
    else:
        say("== {}: not finished ({}) ==".format(head, moc_state_text(data)))
    if data.get("title"):
        say("Title:", data["title"])
    if status == "failed":
        stats = (report or {}).get("stats")
        if isinstance(stats, dict) and stats:
            say("Checked: " + ", ".join("{} {}".format(v, k) for k, v in stats.items()))
        for n, issue in enumerate(fails, 1):
            say("FAIL {}/{} {}: {}".format(n, len(fails), issue.get("rule") or "?", issue.get("message") or ""))
            if issue.get("hint"):
                say("  Hint:", issue["hint"])
            parts = issue_parts(issue)
            for p in parts[:ISSUE_PARTS_SHOWN]:
                say("  " + part_text(p))
            if len(parts) > ISSUE_PARTS_SHOWN:
                say("  +{} more (all are listed in the report file)".format(len(parts) - ISSUE_PARTS_SHOWN))
        if not fails:
            say("The server listed no fail issue{}; read the report file.".format(
                ": " + error_detail(data, report) if error_detail(data, report) else ""))
    if status in ("passed", "failed"):
        print_warnings(warnings)
    model_file = t["model_file"] or local_model_copy(ctx, t)
    if model_file:
        say("Submitted file{}: {}".format(" (line numbers count its lines from 1)" if status == "failed" else "",
                                          model_file))
    if t["report_file"]:
        say("Report:", t["report_file"])
    if status == "passed":
        say("{} parts, {} building steps. Passing means the model is physically consistent in the verifier's "
            "simulation; it does not guarantee the real build holds.".format(data.get("parts", "?"),
                                                                              data.get("steps", "?")))
        say("MODEL PAGE (public, anyone with the link can view it; 3D view and step-by-step building "
            "instructions): {}".format(data.get("url") or "(the server sent no URL)"))
    elif status == "failed":
        say("NOT PASSED: fix the lines named above, then submit a new version of this MOC: " + resubmit)
    elif status == "error":
        detail = error_detail(data, report)
        say("The server failed while checking this version{}. This is a server-side error, not a problem with "
            "the design. The same file can be submitted again unchanged: {}".format(
                " (" + detail + ")" if detail else "", resubmit))
    else:
        say("STILL RUNNING: the check has not finished. Run: " + script_cmd("moc status {} --wait {}".format(
            t["moc_id"], MOC_WAIT_S)))


def local_model_copy(ctx, t):
    if not t["version"]:
        return None
    copy = moc_dir(ctx) / t["moc_id"] / "v{}.ldr".format(t["version"])
    return str(copy) if copy.is_file() else None


def finish_moc(ctx, rep):
    for t in rep["moc"]["targets"]:
        if t["poll_error"] and not t["done"]:  # only the last poll counts; earlier ones were just warnings
            msg = "could not check MOC {}: {}".format(t["moc_id"], t["poll_error"])
            rep["errors"].append(msg)
            say("ERROR:", msg)
        if t["done"] and (t["data"] or {}).get("status") == "error":
            rep["errors"].append("MOC {} version {}: server-side error while checking; the same file can be "
                                 "submitted again".format(t["moc_id"], t["version"]))
    for t in rep["moc"]["targets"]:
        if not t["missing"]:
            print_moc(ctx, t)


def show_recent_mocs(ctx, limit=5):
    found = []
    for path in moc_dir(ctx).glob("*/result.json"):
        try:
            data = read_json(path)
            found.append((path.stat().st_mtime, path.parent.name, data if isinstance(data, dict) else {}))
        except (OSError, ValueError):
            continue
    if not found:
        return
    found.sort(key=lambda item: item[0], reverse=True)
    say("Latest results saved on this computer (re-check one with {}):".format(script_cmd("moc status MOC_ID")))
    for _, moc_id, data in found[:limit]:
        say("  {}  v{}  {}{}".format(moc_id, data.get("version", "?"), data.get("status", "?"),
                                     "  " + data["url"] if data.get("url") else ""))


def moc_exit_code(rep):
    if rep["errors"]:
        return EXIT_ERROR
    statuses = [(t["data"] or {}).get("status") for t in rep["moc"]["targets"] if not t["missing"]]
    if "error" in statuses:
        return EXIT_ERROR
    if any(s not in MOC_TERMINAL for s in statuses):
        return EXIT_PENDING
    return EXIT_MOC_FAILED if "failed" in statuses else EXIT_OK


MOC_KEYS = ("moc_id", "version", "status", "url", "fails", "warnings", "report_file", "result_file", "title",
            "parts", "steps", "model_file")


def moc_fields(ctx, t):
    data = t["data"] or {}
    report, fails, warnings = issues_of(data)
    return {"moc_id": t["moc_id"], "version": t["version"], "status": None if t["missing"] else data.get("status"),
            "url": data.get("url"), "fails": len(fails) if report is not None else None,
            "warnings": len(warnings) if report is not None else None, "report_file": t["report_file"],
            "result_file": str(moc_dir(ctx) / t["moc_id"] / "result.json") if t["done"] else None,
            "title": data.get("title"), "parts": data.get("parts"), "steps": data.get("steps"),
            "model_file": t["model_file"] or local_model_copy(ctx, t)}


def moc_result(ctx, rep, code):
    m = rep["moc"]
    mocs = [moc_fields(ctx, t) for t in m["targets"]]
    # Top-level fields describe one MOC: the one that decides the exit code (error/unknown, then still
    # running, then failed, then passed), the latest of those when several tie. All are in "mocs".
    rank = {None: 0, "error": 0, "failed": 2, "passed": 3}
    main_moc = (min(reversed(mocs), key=lambda f: rank.get(f["status"], 1)) if mocs
                else dict.fromkeys(MOC_KEYS))
    obj = {"exit_code": code, "command": rep["command"]}
    obj.update(main_moc)
    obj.update({
        "mocs": mocs, "pending_moc_ids": load_ids(moc_pending_path(ctx), "moc_ids")[0],
        "guide_file": m["guide_file"], "connectors_file": m["connectors_file"],
        "connector_parts": m["connector_parts"], "connector_requests": m["connector_requests"],
        "unsupported_parts": m["unsupported_parts"], "errors": rep["errors"],
        "user_id": ctx.get("user_id"), "server": ctx.get("server"), "home": str(ctx["home"]),
        "moc_dir": str(moc_dir(ctx)),
    })
    return obj


# ---------------------------------------------------------------- commands

def finish(ctx, rep, images, force_refresh=False):
    """Refresh parts.json when something was collected (or asked for), then print the summary."""
    if rep["poll_error"]:  # only the last poll round counts; earlier transient failures were just warnings
        rep["errors"].append("could not check task status: " + rep["poll_error"])
        say("ERROR:", rep["errors"][-1])
    if ctx.get("user_id") and (rep["collected"] or images or force_refresh):
        try:
            refresh_parts(ctx, rep, images)
        except ApiError as e:
            msg = "could not refresh parts.json: {} (results are saved in tasks/; run parts later)".format(
                describe(e))
            rep["errors"].append(msg)
            say("ERROR:", msg)
    print_summary(ctx, rep)


def no_user_yet(ctx):
    if read_pending(ctx):
        raise Fatal("pending.json lists tasks but {} is missing, so they cannot be checked".format(
            ctx["home"] / "user.json"))
    say("No user ID on this computer yet ({}), so there is nothing to check. Run scan first.".format(
        ctx["home"] / "user.json"))


def cmd_scan(ctx, rep, args):
    photos = expand_photos(args.photos)
    setup(ctx)
    ensure_user(ctx)
    earlier = read_pending(ctx)
    if earlier:
        say("Checking {} task(s) left from earlier runs ...".format(len(earlier)))
        track(ctx, rep, earlier, 0)
    upload(ctx, rep, photos)
    if not rep["submitted"] and not rep["errors"]:
        rep["errors"].append("no photo was accepted by the server (see REJECTED lines)")
    if args.no_wait:
        if rep["submitted"]:
            say("Not waiting (--no-wait). Task IDs are saved in", ctx["home"] / "pending.json")
    else:
        watch = list(dict.fromkeys(rep["submitted"] + read_pending(ctx)))
        if watch:
            say("Waiting for results (up to {} s; stopping early is safe, task IDs are saved in "
                "pending.json) ...".format(args.wait))
            track(ctx, rep, watch, args.wait)
    finish(ctx, rep, args.images)


def cmd_status(ctx, rep, args):
    for tid in args.task_ids:
        if not valid_id(tid):
            raise UsageError("not a task ID: " + tid)
    setup(ctx)
    if not ctx["user_id"]:
        return no_user_yet(ctx)
    watch = list(dict.fromkeys(args.task_ids)) or read_pending(ctx)
    if watch:
        track(ctx, rep, watch, args.wait)
    else:
        say("No pending tasks.")
    finish(ctx, rep, args.images)


def cmd_parts(ctx, rep, args):
    setup(ctx)
    if not ctx["user_id"]:
        return no_user_yet(ctx)
    pending = read_pending(ctx)
    if pending:
        track(ctx, rep, pending, 0)
    finish(ctx, rep, args.images, force_refresh=True)


def cmd_whoami(ctx, rep, args):
    setup(ctx, need_server=False)
    configured = server_configured(ctx["server"])
    say("User ID:    {}".format(ctx["user_id"] or "(none yet; the first scan creates one)"))
    say("Server:     {}".format("{} (from {})".format(ctx["server"], ctx["server_source"]) if configured
                                else "(not configured)"))
    say("Local dir:  {}".format(ctx["home"]))
    say("Pending:    {} task(s)".format(len(read_pending(ctx))))
    rows, total, problem = load_inventory(ctx)
    say("parts.json: {}".format(problem or "{} pieces in {} rows".format(total, len(rows))))
    say("MOCs:       {} submission(s) waiting for their check result".format(len(read_moc_pending(ctx))))
    add_unique(rep["scope"], read_pending(ctx))
    if not configured:
        rep["errors"].append(no_server_message())
        say("ERROR:", no_server_message())
        return EXIT_ERROR
    return EXIT_OK


def wait_seconds(text):
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a whole number of seconds")
    if value < 0:
        raise argparse.ArgumentTypeError("must be 0 or more")
    return value


def build_parser():
    parser = argparse.ArgumentParser(
        prog="lego.py", description="Recognise loose LEGO parts from photos and check LEGO models (MOCs) "
                                    "designed from them; results go to ~/.lego-parts/.",
        epilog="Exit codes: 0 all finished / MOC passed, 4 MOC did not pass, 3 still running (run status --wait "
               "or moc status --wait), 1 error, 2 usage error.")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True
    scan = sub.add_parser("scan", help="upload 1-30 photos and wait for the result")
    scan.add_argument("photos", nargs="+", metavar="PHOTO", help="image files, folders or glob patterns")
    scan.add_argument("--no-wait", action="store_true", help="return right after uploading")
    scan.add_argument("--wait", type=wait_seconds, default=SCAN_WAIT_S, metavar="SECONDS",
                      help="wait at most this long (default %(default)s)")
    scan.add_argument("--images", action="store_true", help="also download part images into images/")
    scan.set_defaults(func=cmd_scan)
    status = sub.add_parser("status", help="check pending (or the given) tasks and collect finished ones")
    status.add_argument("task_ids", nargs="*", metavar="TASK_ID")
    status.add_argument("--wait", type=wait_seconds, default=0, metavar="SECONDS",
                        help="keep polling up to this long (default 0: check once)")
    status.add_argument("--images", action="store_true", help="also download part images into images/")
    status.set_defaults(func=cmd_status)
    parts = sub.add_parser("parts", help="collect finished tasks, then refresh parts.json from the server")
    parts.add_argument("--images", action="store_true", help="also download part images into images/")
    parts.set_defaults(func=cmd_parts)
    whoami = sub.add_parser("whoami", help="show user ID, server and local directory")
    whoami.set_defaults(func=cmd_whoami)

    moc = sub.add_parser("moc", help="design check: guide, submit a model, check a submission")
    moc_sub = moc.add_subparsers(dest="moc_command", metavar="MOC_COMMAND")
    moc_sub.required = True
    guide = moc_sub.add_parser("guide", help="download the design guide and the connector sheets of your parts")
    guide.add_argument("--parts-file", metavar="PATH", help="inventory to use instead of parts.json")
    guide.set_defaults(func=cmd_moc_guide)
    submit = moc_sub.add_parser("submit", help="submit an LDraw model for the check and wait for the result")
    submit.add_argument("model", metavar="MODEL.ldr", help="the LDraw model file")
    submit.add_argument("--title", metavar="NAME", help="name of the model, at most 80 characters")
    submit.add_argument("--design", metavar="design.json", help="optional design file (title, description, motions)")
    submit.add_argument("--moc", metavar="MOC_ID", help="submit a new version of this existing MOC")
    submit.add_argument("--parts-file", metavar="PATH", help="inventory to send instead of parts.json")
    submit.add_argument("--no-wait", action="store_true", help="return right after submitting")
    submit.add_argument("--wait", type=wait_seconds, default=MOC_WAIT_S, metavar="SECONDS",
                        help="wait at most this long (default %(default)s)")
    submit.set_defaults(func=cmd_moc_submit)
    mstatus = moc_sub.add_parser("status", help="check pending (or the given) MOC submission and fetch its report")
    mstatus.add_argument("moc_id", nargs="?", metavar="MOC_ID")
    mstatus.add_argument("--wait", type=wait_seconds, default=0, metavar="SECONDS",
                         help="keep polling up to this long (default 0: check once)")
    mstatus.set_defaults(func=cmd_moc_status)
    return parser


def main(argv=None):
    setup_stdout()
    ctx = {"start": time.monotonic(), "home": home_dir(), "user_id": None, "server": None}
    rep = new_report(None)
    raw_args = sys.argv[1:] if argv is None else list(argv)
    if raw_args[:1] == ["moc"]:  # so that even a usage error gets the moc RESULT fields
        rep["command"] = "moc"
    try:
        try:
            args = build_parser().parse_args(argv)
        except SystemExit as e:
            if e.code in (0, None):  # --help
                return EXIT_OK
            raise UsageError("invalid arguments (see the usage message above)")
        rep["command"] = args.command if args.command != "moc" else "moc " + args.moc_command
        code = args.func(ctx, rep, args)
        if code is None:
            code = exit_code(rep)
    except UsageError as e:
        say("USAGE ERROR:", e)
        rep["errors"].append(str(e))
        code = EXIT_USAGE
    except Fatal as e:
        say("ERROR:", e)
        rep["errors"].append(str(e))
        code = EXIT_ERROR
    except ApiError as e:
        say("ERROR:", describe(e))
        rep["errors"].append(describe(e))
        code = EXIT_ERROR
    except KeyboardInterrupt:
        msg = "interrupted. Submitted task IDs are kept in pending.json; run status to collect them."
        if (rep["command"] or "").startswith("moc"):
            msg = "interrupted. A submitted MOC ID is kept in moc/pending.json; run moc status to fetch its result."
        say("ERROR:", msg)
        rep["errors"].append(msg)
        code = EXIT_ERROR
    except Exception as e:  # a bug: show it, but still end with a RESULT line
        traceback.print_exc(file=sys.stdout)
        rep["errors"].append("internal error: {!r}".format(e))
        code = EXIT_ERROR
    emit_result(ctx, rep, code)
    return code


if __name__ == "__main__":
    sys.exit(main())
