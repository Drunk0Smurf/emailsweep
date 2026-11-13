#!/usr/bin/env python3
"""
Inbox Sweep - bulk delete Gmail with an App Password.

    python3 inbox_sweep.py          # opens http://localhost:8080
    python3 inbox_sweep.py 9000     # different port

Standard library only. Talks to Gmail over IMAP (imap.gmail.com:993, TLS).
Your password is held in memory only and never written to disk.
The web page is served on 127.0.0.1 only and requires a per-run token.
"""
import email
import email.parser
import email.utils
import http.client
import os
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path
import imaplib
import json
import queue
import random
import time
import re
import secrets
import socket
import ssl
import sys
import threading
import webbrowser
from email.header import decode_header, make_header
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "127.0.0.1"
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
TOKEN = secrets.token_urlsafe(24)
PAGE = 100
FETCH_CHUNK = 400
BATCH = 750         # emails per IMAP command (sent as compact ranges); halves automatically if Gmail struggles
WORKERS = 3         # parallel Gmail connections used for deleting (Gmail allows ~15)
MAX_TRIES = 6       # retries per batch before giving up on those emails

PROGRESS = {"text": "", "frac": None}


def set_progress(text, frac=None):
    PROGRESS["text"], PROGRESS["frac"] = text, frac


def chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def expand_uidset(s):
    out = []
    for part in s.split(","):
        if ":" in part:
            a, b = part.split(":")
            a, b = int(a), int(b)
            out.extend(range(min(a, b), max(a, b) + 1))
        elif part:
            out.append(int(part))
    return out


def dh(v):
    try:
        return str(make_header(decode_header(v or "")))
    except Exception:
        return v or ""


class GmailError(Exception):
    pass


class Gmail:
    def __init__(self):
        self.lock = threading.RLock()
        self.imap = None
        self.user = None
        self.pw = None
        self.folders = {}
        self.selected = None
        self.last = {"folder": None, "uids": []}

    # ---- connection ----
    def connect(self):
        try:
            m = imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=60)
        except (OSError, socket.gaierror) as e:
            raise GmailError(f"Can't reach imap.gmail.com: {e}")
        try:
            m.login(self.user, self.pw)
        except imaplib.IMAP4.error as e:
            msg = str(e)
            if "AUTHENTICATIONFAILED" in msg or "Invalid credentials" in msg:
                raise GmailError("Gmail rejected the login. Check the email address and that you're using an "
                                 "App Password (16 letters), not your normal password.")
            raise GmailError(f"Login failed: {msg}")
        self.imap, self.selected = m, None
        self._discover_folders()

    def login(self, user, pw):
        self.user = user.strip()
        self.pw = re.sub(r"\s+", "", pw)
        self.connect()

    def logout(self):
        try:
            if self.imap:
                self.imap.logout()
        except Exception:
            pass
        self.imap = self.user = self.pw = None
        self.last = {"folder": None, "uids": []}

    def run(self, fn):
        """Run fn with one automatic reconnect if Gmail dropped the connection."""
        if not self.user:
            raise GmailError("Not logged in")
        with self.lock:
            for attempt in (1, 2):
                try:
                    if self.imap is None:
                        self.connect()
                    return fn()
                except (imaplib.IMAP4.abort, OSError, ssl.SSLError):
                    if attempt == 2:
                        raise GmailError("Lost the connection to Gmail. Try again.")
                    self.imap = None

    def _discover_folders(self):
        typ, data = self.imap.list()
        f = {}
        for line in data or []:
            if not isinstance(line, bytes):
                continue
            s = line.decode("utf-8", "replace")
            m = re.match(r'\((?P<flags>[^)]*)\)\s+(?:"[^"]*"|NIL)\s+(?P<name>.+)$', s)
            if not m:
                continue
            name = m.group("name").strip()
            if not name.startswith('"'):
                name = f'"{name}"'
            flags = m.group("flags").split()
            for flag in ("\\All", "\\Trash", "\\Junk"):
                if flag in flags:
                    f[flag] = name
        f.setdefault("\\All", '"[Gmail]/All Mail"')
        f.setdefault("\\Trash", '"[Gmail]/Trash"')
        f.setdefault("\\Junk", '"[Gmail]/Spam"')
        self.folders = f

    def select(self, folder):
        if self.selected != folder:
            typ, data = self.imap.select(folder)
            if typ != "OK":
                raise GmailError(f"Couldn't open folder {folder}: {data}")
            self.selected = folder

    def folder_for(self, q):
        if re.search(r"\bin:trash\b", q, re.I):
            return self.folders["\\Trash"]
        if re.search(r"\bin:spam\b", q, re.I):
            return self.folders["\\Junk"]
        return self.folders["\\All"]

    # ---- operations ----
    def total(self):
        typ, data = self.imap.status(self.folders["\\All"], "(MESSAGES)")
        m = re.search(rb"MESSAGES (\d+)", data[0] or b"")
        return int(m.group(1)) if m else None

    def search(self, q, folder):
        self.select(folder)
        if q.strip():
            self.imap.literal = q.encode("utf-8")
            typ, data = self.imap.uid("SEARCH", "CHARSET", "UTF-8", "X-GM-RAW")
        else:
            typ, data = self.imap.uid("SEARCH", "ALL")
        if typ != "OK":
            raise GmailError(f"Search failed: {data}")
        uids = [int(u) for u in (data[0] or b"").split()]
        return uids[::-1]  # newest first

    def fetch_meta(self, uids, fields="FROM SUBJECT DATE", progress=None):
        out = {}
        done = 0
        for chunk in chunks(uids, FETCH_CHUNK):
            typ, data = self.imap.uid("FETCH", ",".join(map(str, chunk)),
                                      f"(UID RFC822.SIZE BODY.PEEK[HEADER.FIELDS ({fields})])")
            if typ != "OK":
                raise GmailError(f"Fetch failed: {data}")
            for i, item in enumerate(data):
                if not isinstance(item, tuple):
                    continue
                meta = item[0]
                nxt = data[i + 1] if i + 1 < len(data) and isinstance(data[i + 1], bytes) else b""
                both = meta + b" " + nxt
                mu = re.search(rb"UID (\d+)", both)
                ms = re.search(rb"RFC822\.SIZE (\d+)", both)
                if not mu:
                    continue
                msg = email.message_from_bytes(item[1])
                name, addr = email.utils.parseaddr(dh(msg.get("From", "")))
                ts = 0
                try:
                    d = email.utils.parsedate_to_datetime(msg.get("Date", ""))
                    ts = int(d.timestamp() * 1000)
                except Exception:
                    pass
                out[int(mu.group(1))] = {
                    "uid": int(mu.group(1)),
                    "name": name or addr or "(unknown)",
                    "addr": (addr or "").lower(),
                    "subject": dh(msg.get("Subject", "")) or "(no subject)",
                    "date": ts,
                    "size": int(ms.group(1)) if ms else 0,
                }
            done += len(chunk)
            if progress:
                progress(done, len(uids))
        return [out[u] for u in uids if u in out]

    def delete(self, uids, folder, permanent):
        """Returns (deleted, failed)."""
        trash = self.folders["\\Trash"]
        total = len(uids)
        if folder == trash:
            if not permanent:
                return 0, 0
            _, failed = self._parallel(trash, uids, self._purge_batch, "Deleting forever", 0, 1)
            return total - failed, failed

        verb = "Deleting" if permanent else "Moving to Trash"
        dest, failed = self._parallel(folder, uids, lambda c, b: self._move_batch(c, b, trash, permanent),
                                      verb, 0, 0.5 if permanent else 1)
        if permanent and total - failed > 0:
            if not dest:
                raise GmailError("Moved to Trash, but Gmail didn't confirm which messages to purge. "
                                 "Use the Trash filter to empty them from there.")
            _, f2 = self._parallel(trash, dest, self._purge_batch, "Deleting forever", 0.5, 0.5)
            failed += f2
        return total - failed, failed

    def important_subset(self, uids, folder, imp_q):
        self.select(folder)
        imp = set(self.search(imp_q, folder))
        return [u for u in uids if u in imp]

    def fetch_raw_to_zips(self, uids, folder, base_path, max_bytes):
        """Download full emails into one or more zips of .eml files, each kept under max_bytes.
        Returns (paths, count_written, count_failed)."""
        zlock, written = threading.Lock(), set()
        parser = email.parser.BytesHeaderParser()
        parts = []
        st = {"zf": None, "size": 0}

        def open_part():
            if st["zf"]:
                st["zf"].close()
            p = base_path.with_name(f"{base_path.stem}-part{len(parts) + 1}.zip")
            parts.append(p)
            st["zf"] = zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED, allowZip64=True)
            st["size"] = 0

        def add(name, raw):
            # Start a new part if this email would push the current one over the limit.
            if st["zf"] is None or (st["size"] and st["size"] + len(raw) + 512 > max_bytes):
                open_part()
            st["zf"].writestr(name, raw)
            info = st["zf"].infolist()[-1]
            st["size"] += info.compress_size + len(name) * 2 + 100

        def work(conn, batch):
            conn.untagged_responses.clear()
            typ, data = conn.uid("FETCH", uidset(batch), "(UID BODY.PEEK[])")
            if typ != "OK":
                raise GmailError(f"Download failed: {data}")
            for i, item in enumerate(data):
                if not isinstance(item, tuple):
                    continue
                nxt = data[i + 1] if i + 1 < len(data) and isinstance(data[i + 1], bytes) else b""
                mu = re.search(rb"UID (\d+)", item[0] + b" " + nxt)
                if not mu:
                    continue
                uid, raw = int(mu.group(1)), item[1]
                with zlock:
                    if uid in written:
                        continue
                    written.add(uid)
                    add(eml_name(uid, raw, parser), raw)
            conn.untagged_responses.clear()
            return []

        try:
            _, failed = self._parallel(folder, uids, work, "Downloading for the archive", 0, 1, batch=50)
        finally:
            if st["zf"]:
                st["zf"].close()
        missing = len(set(uids) - written)
        return parts, len(written), max(failed, missing)

    # ---- fast batch helpers ----
    @staticmethod
    def _move_batch(conn, batch, trash, permanent):
        conn.untagged_responses.clear()
        typ, data = conn.uid("MOVE", uidset(batch), trash)
        if typ != "OK":
            raise GmailError(f"Move to Trash failed: {data}")
        dest = []
        if permanent:
            _, cu = conn.response("COPYUID")
            entries = [e for e in (cu or []) if e]
            if not entries:  # some servers put COPYUID in the tagged OK instead
                entries = [m.group(1) for d in data if isinstance(d, bytes)
                           for m in [re.search(rb"COPYUID ([^\]]+)", d)] if m]
            for entry in entries:
                parts = entry.decode().split()
                if len(parts) >= 3:
                    dest.extend(expand_uidset(parts[2]))
        conn.untagged_responses.clear()
        return dest

    @staticmethod
    def _purge_batch(conn, batch):
        s = uidset(batch)
        conn.untagged_responses.clear()
        typ, data = conn.uid("STORE", s, "+FLAGS.SILENT", "(\\Deleted)")
        if typ != "OK":
            raise GmailError(f"Delete failed: {data}")
        typ, _ = conn.uid("EXPUNGE", s)
        if typ != "OK":
            conn.expunge()
        conn.untagged_responses.clear()
        return []

    def _new_conn(self, folder):
        m = imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=180)
        m.login(self.user, self.pw)
        typ, data = m.select(folder)
        if typ != "OK":
            raise GmailError(f"Couldn't open {folder}: {data}")
        return m

    def _parallel(self, folder, uids, work, label, frac_base, frac_span, batch=None):
        """Run batches across several IMAP connections. When Gmail errors (it throws
        'System Error' when overloaded), back off, reconnect, split the batch and retry.
        Returns (results, number_of_uids_given_up_on)."""
        batches = split_batches(uids, batch or BATCH)
        total = len(uids)
        jobs = queue.Queue()
        for bt in batches:
            jobs.put((bt, 0))
        lock = threading.Lock()
        state = {"pending": len(batches), "done": 0, "failed": 0, "results": []}
        set_progress(f"{label}… 0 / {total:,}", frac_base)

        def report(note=""):
            set_progress(f"{label}… {state['done']:,} / {total:,}{note}",
                         frac_base + frac_span * state["done"] / total)

        def worker(delay):
            time.sleep(delay)  # stagger logins
            conn = None
            while True:
                with lock:
                    if state["pending"] <= 0:
                        break
                try:
                    bt, tries = jobs.get(timeout=0.5)
                except queue.Empty:
                    continue
                try:
                    if conn is None:
                        conn = self._new_conn(folder)
                    r = work(conn, bt)
                except Exception as e:
                    print(f"  Gmail hiccup ({len(bt)} emails, try {tries + 1}): {e}")
                    try:
                        conn.logout()
                    except Exception:
                        pass
                    conn = None
                    if tries + 1 >= MAX_TRIES:
                        with lock:
                            state["failed"] += len(bt)
                            state["done"] += len(bt)
                            state["pending"] -= 1
                            report()
                        continue
                    with lock:
                        report(" (Gmail is busy, retrying…)")
                    time.sleep(min(30, 2 ** tries) + random.random() * 2)
                    if len(bt) > 50:
                        mid = len(bt) // 2
                        with lock:
                            state["pending"] += 1
                        jobs.put((bt[:mid], tries + 1))
                        jobs.put((bt[mid:], tries + 1))
                    else:
                        jobs.put((bt, tries + 1))
                    continue
                with lock:
                    state["results"].extend(r or [])
                    state["done"] += len(bt)
                    state["pending"] -= 1
                    report()
            if conn is not None:
                try:
                    conn.logout()
                except Exception:
                    pass

        n = max(1, min(WORKERS, len(batches)))
        threads = [threading.Thread(target=worker, args=(i * 0.3,), daemon=True) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return state["results"], state["failed"]


def uidset(uids):
    """[1,2,3,7,9,10] -> '1:3,7,9:10' (keeps commands short so batches can be big)."""
    s = sorted(set(uids))
    out, i = [], 0
    while i < len(s):
        j = i
        while j + 1 < len(s) and s[j + 1] == s[j] + 1:
            j += 1
        out.append(str(s[i]) if i == j else f"{s[i]}:{s[j]}")
        i = j + 1
    return ",".join(out)


def split_batches(uids, max_n=BATCH, max_len=7000):
    out = []
    def add(b):
        if len(b) > 1 and len(uidset(b)) > max_len:
            mid = len(b) // 2
            add(b[:mid]); add(b[mid:])
        else:
            out.append(b)
    s = sorted(set(uids))
    for i in range(0, len(s), max_n):
        add(s[i:i + max_n])
    return out


def eml_name(uid, raw, parser):
    try:
        h = parser.parsebytes(raw[:65536])
        subj = dh(h.get("Subject", "")) or "no subject"
        try:
            d = email.utils.parsedate_to_datetime(h.get("Date", ""))
        except Exception:
            d = None
    except Exception:
        subj, d = "no subject", None
    subj = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", subj).strip()[:80] or "no subject"
    folder = d.strftime("%Y-%m") if d else "undated"
    day = d.strftime("%Y-%m-%d") if d else "undated"
    return f"Inbox Sweep/{folder}/{day} {subj} [{uid}].eml"


# ---------------- settings ----------------
CONFIG_PATH = Path.home() / ".config" / "inbox-sweep" / "config.json"
ARCHIVE_DIR = Path.home() / "inbox-sweep-archives"
DEFAULT_CFG = {"oa_url": "", "oa_key": "", "oa_verify_tls": True, "important_q": "is:starred OR is:important", "oa_max_mb": 90, "oa_origin": ""}


def load_cfg():
    cfg = dict(DEFAULT_CFG)
    try:
        cfg.update(json.loads(CONFIG_PATH.read_text()))
    except Exception:
        pass
    return cfg


def save_cfg(cfg):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except Exception:
        pass


# ---------------- Open Archiver ----------------
def oa_base(cfg):
    u = (cfg.get("oa_url") or "").strip().rstrip("/")
    if not u:
        raise GmailError("Open Archiver isn't set up yet. Click Open Archiver at the top right.")
    if not re.match(r"https?://", u):
        u = "http://" + u
    u = re.sub(r"/api(/v1)?$", "", u)
    return u + "/api/v1"


def oa_ctx(cfg):
    return ssl.create_default_context() if cfg.get("oa_verify_tls", True) else ssl._create_unverified_context()


def oa_error(code, body):
    try:
        msg = json.loads(body).get("message") or body.decode()[:200]
    except Exception:
        msg = (body or b"").decode("utf-8", "replace")[:200]
    if code == 401:
        msg = "API key was rejected. Check it in Open Archiver > Settings > API Keys."
    elif code == 403 and b"Cross-site" in (body or b""):
        msg = ("upload refused because the Origin doesn't match Open Archiver's APP_URL. Set 'Open Archiver APP_URL' "
               "in the settings here to the APP_URL from Open Archiver's .env.")
    elif code == 403:
        msg = "that API key's user isn't allowed to create ingestion sources."
    elif code == 413:
        msg = "file too large for Open Archiver's BODY_SIZE_LIMIT (or a reverse proxy limit)."
    return GmailError(f"Open Archiver error {code}: {msg}")


def oa_request(cfg, method, path, payload=None, timeout=60):
    url = oa_base(cfg) + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "X-API-KEY": cfg.get("oa_key", ""), "Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=oa_ctx(cfg)) as r:
            body = r.read()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        raise oa_error(e.code, e.read())
    except (urllib.error.URLError, OSError) as e:
        raise GmailError(f"Can't reach Open Archiver at {oa_base(cfg)}: {getattr(e, 'reason', e)}")


def oa_conn(cfg, timeout=600):
    u = urllib.parse.urlparse(oa_base(cfg))
    if u.scheme == "https":
        return u, http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=timeout, context=oa_ctx(cfg))
    return u, http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout)


def url_origin(url):
    u = urllib.parse.urlparse(url if re.match(r"https?://", url or "") else "http://" + (url or ""))
    return f"{u.scheme}://{u.netloc}"


def oa_probe(cfg, origin):
    """Empty multipart POST to /upload. Open Archiver refuses form uploads whose Origin header doesn't match
    its APP_URL/ORIGIN with 403 'Cross-site POST form submissions are forbidden'. An empty form saves nothing."""
    u, conn = oa_conn(cfg, timeout=15)
    boundary = secrets.token_hex(8)
    body = f"--{boundary}--\r\n".encode()
    try:
        conn.request("POST", u.path + "/upload", body=body, headers={
            "X-API-KEY": cfg.get("oa_key", ""), "Origin": origin,
            "Content-Type": f"multipart/form-data; boundary={boundary}"})
        r = conn.getresponse()
        return r.status, r.read()
    except OSError as e:
        raise GmailError(f"Can't reach Open Archiver at {oa_base(cfg)}: {e}")
    finally:
        conn.close()


def oa_find_origin(cfg):
    """Return an Origin value Open Archiver accepts for uploads, trying the likely ones."""
    cands = []
    for c in (cfg.get("oa_origin"), url_origin(cfg.get("oa_url", "")), "http://localhost:3000"):
        c = (c or "").strip().rstrip("/")
        if c and c not in cands:
            cands.append(url_origin(c))
    for c in cands:
        status, body = oa_probe(cfg, c)
        if status == 403 and b"Cross-site" in body:
            continue
        if status == 401:
            raise oa_error(401, body)
        return c
    raise GmailError(
        "Open Archiver refuses uploads from Inbox Sweep because the Origin doesn't match its APP_URL. "
        "On the Open Archiver server run:  grep -E '^(APP_URL|ORIGIN)=' .env  and put that exact URL into "
        "'Open Archiver APP_URL' in the settings here. Nothing was deleted.")


def oa_upload(cfg, path, filename, progress):
    """Stream a file to POST /api/v1/upload as multipart/form-data. Returns the stored filePath."""
    origin = oa_find_origin(cfg)
    if origin != (cfg.get("oa_origin") or ""):
        c2 = load_cfg(); c2["oa_origin"] = origin; save_cfg(c2); cfg["oa_origin"] = origin
    u, conn = oa_conn(cfg)
    boundary = secrets.token_hex(16)
    head = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: application/zip\r\n\r\n").encode()
    tail = f"\r\n--{boundary}--\r\n".encode()
    size = os.path.getsize(path)
    try:
        conn.putrequest("POST", u.path + "/upload")
        conn.putheader("X-API-KEY", cfg.get("oa_key", ""))
        conn.putheader("Origin", origin)
        conn.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
        conn.putheader("Content-Length", str(len(head) + size + len(tail)))
        conn.endheaders()
        conn.send(head)
        sent = 0
        with open(path, "rb") as f:
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    break
                conn.send(chunk)
                sent += len(chunk)
                progress(sent, size)
        conn.send(tail)
        r = conn.getresponse()
        body = r.read()
    except OSError as e:
        raise GmailError(f"Upload to Open Archiver failed: {e}")
    finally:
        conn.close()
    if r.status != 200:
        raise oa_error(r.status, body)
    fp = json.loads(body).get("filePath")
    if not fp:
        raise GmailError("Open Archiver accepted the upload but didn't return a file path.")
    return fp


def oa_upload_retry(cfg, path, label):
    size_mb = os.path.getsize(path) / 1e6
    last = None
    for attempt in range(3):
        try:
            return oa_upload(cfg, path, path.name,
                             lambda s, t: set_progress(f"{label}… {s / 1e6:.0f} / {t / 1e6:.0f} MB", s / t))
        except GmailError as e:
            last = e
            if "error 401" in str(e) or "error 403" in str(e) or "APP_URL" in str(e):
                raise
            set_progress(f"{label}: upload interrupted, retrying ({attempt + 2}/3)…")
            time.sleep(3 * (attempt + 1))
    raise GmailError(
        f"Open Archiver cut off the upload of a {size_mb:.0f} MB file ({last}). That usually means the file is over "
        f"Open Archiver's BODY_SIZE_LIMIT (only 512K if it's missing from Open Archiver's .env) or a reverse proxy's "
        f"upload limit. Add BODY_SIZE_LIMIT=1G to Open Archiver's .env and run 'docker compose up -d'. Nothing was deleted.")


def archive_uids(uids, folder, wait_minutes=30):
    """Download emails, send them to Open Archiver as EML imports (split to fit its upload limit),
    and wait until every part is imported. Raises if anything goes wrong, so callers can skip deleting."""
    cfg = load_cfg()
    oa_base(cfg)
    max_bytes = max(5, int(cfg.get("oa_max_mb") or 90)) * 1_000_000
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    stamp = now.strftime("%Y-%m-%d_%H%M%S")
    base, n = ARCHIVE_DIR / f"inbox-sweep-{stamp}.zip", 2
    while list(ARCHIVE_DIR.glob(base.stem + "-part*.zip")):
        base, n = ARCHIVE_DIR / f"inbox-sweep-{stamp}-{n}.zip", n + 1
    parts, got, failed = G.fetch_raw_to_zips(uids, folder, base, max_bytes)
    if failed:
        raise GmailError(f"Couldn't download {failed:,} of the emails for the archive, so nothing was deleted. "
                         "Try again in a minute.")
    for p in parts:
        if os.path.getsize(p) > max_bytes:
            raise GmailError(f"One email is bigger than your Max upload size ({os.path.getsize(p) / 1e6:.0f} MB "
                             f"on its own). Raise BODY_SIZE_LIMIT in Open Archiver and 'Max upload size' here, "
                             "or skip that email. Nothing was deleted.")
    sids = []
    for i, p in enumerate(parts, 1):
        tag = f"part {i} of {len(parts)}" if len(parts) > 1 else ""
        fp = oa_upload_retry(cfg, p, f"Uploading to Open Archiver{' (' + tag + ')' if tag else ''}")
        set_progress(f"Creating the import in Open Archiver{' (' + tag + ')' if tag else ''}…")
        with zipfile.ZipFile(p) as z:
            count = len(z.namelist())
        src = oa_request(cfg, "POST", "/ingestion-sources", {
            "name": f"Inbox Sweep {now.strftime('%d %b %Y %H:%M:%S')}{' ' + tag if tag else ''} ({count:,} emails)",
            "provider": "eml_import",
            "providerConfig": {"type": "eml_import", "uploadedFileName": p.name, "uploadedFilePath": fp},
        })
        if not src.get("id"):
            raise GmailError("Open Archiver didn't create the import source. Nothing was deleted.")
        sids.append(src["id"])

    deadline = time.time() + wait_minutes * 60
    pending = set(sids)
    while pending:
        for sid in list(pending):
            s = oa_request(cfg, "GET", f"/ingestion-sources/{sid}")
            st = s.get("status")
            if st in ("imported", "active"):
                pending.discard(sid)
            elif st == "error":
                raise GmailError(f"Open Archiver couldn't import '{s.get('name')}' "
                                 f"({s.get('lastSyncStatusMessage') or 'no details'}). Nothing was deleted. "
                                 f"Copies are saved in {ARCHIVE_DIR}")
        if not pending:
            break
        if time.time() > deadline:
            raise GmailError(f"Open Archiver is still importing after {wait_minutes} minutes, so nothing was deleted yet. "
                             "Check it in Open Archiver, then run the delete again.")
        set_progress(f"Open Archiver is importing {got:,} emails… {len(sids) - len(pending)} of {len(sids)} parts done")
        time.sleep(3)
    return {"archived": got, "zip": str(parts[0].parent), "parts": len(parts)}


G = Gmail()


def full_query(q, protect):
    q = (q or "").strip()
    if protect:
        imp = load_cfg()["important_q"].strip() or DEFAULT_CFG["important_q"]
        q = (q + f" -({imp})").strip()
    return q


# ---------------- API ----------------
def api(path, body):
    if path == "login":
        with G.lock:
            G.logout()
            G.login(body.get("email", ""), body.get("password", ""))
            return {"email": G.user, "total": G.run(G.total)}

    if path == "logout":
        with G.lock:
            G.logout()
        return {}

    if path == "search":
        def fn():
            q = full_query(body.get("q"), body.get("protect"))
            folder = G.folder_for(q)
            set_progress("Searching…")
            uids = G.search(q, folder)
            G.last = {"folder": folder, "uids": uids}
            set_progress("Loading details…")
            return {"total": len(uids), "items": G.fetch_meta(uids[:PAGE])}
        return G.run(fn)

    if path == "page":
        def fn():
            off = int(body.get("offset", 0))
            G.select(G.last["folder"])
            return {"items": G.fetch_meta(G.last["uids"][off:off + PAGE])}
        return G.run(fn)

    if path == "senders":
        def fn():
            q = full_query(body.get("q"), body.get("protect"))
            limit = int(body.get("limit", 1000))
            set_progress("Listing emails…")
            uids = G.search(q, G.folder_for(q))[:limit]
            groups = {}
            meta = G.fetch_meta(uids, "FROM", lambda d, t: set_progress(f"Scanning senders… {d:,} / {t:,}", d / t))
            for m in meta:
                k = m["addr"] or "(unknown)"
                g = groups.setdefault(k, {"addr": k, "name": m["name"], "count": 0, "size": 0})
                g["count"] += 1
                g["size"] += m["size"]
            return {"scanned": len(uids), "senders": sorted(groups.values(), key=lambda g: -g["count"])[:300]}
        return G.run(fn)

    if path == "count":
        def fn():
            q = full_query(body.get("q"), body.get("protect"))
            return {"count": len(G.search(q, G.folder_for(q)))}
        return G.run(fn)

    if path == "delete":
        def fn():
            perm = body.get("mode") == "perm"
            if body.get("q"):
                q = full_query(body["q"], body.get("protect"))
                folder = G.folder_for(q)
                uids = G.search(q, folder)
            elif body.get("all"):
                folder, uids = G.last["folder"], list(G.last["uids"])
            else:
                folder, uids = G.last["folder"], [int(u) for u in body.get("uids", [])]
            if not uids:
                return {"deleted": 0, "uids": []}
            imp_mode = body.get("imp_mode", "skip")
            arch = {"archived": 0}
            if imp_mode in ("archive", "archive_all"):
                set_progress("Finding important emails…")
                to_arch = uids if imp_mode == "archive_all" else G.important_subset(uids, folder, load_cfg()["important_q"])
                if to_arch:
                    arch = archive_uids(to_arch, folder)
                G.selected = None
            n, failed = G.delete(uids, folder, perm)
            if folder == G.last["folder"] and not failed:
                gone = set(uids)
                G.last["uids"] = [u for u in G.last["uids"] if u not in gone]
            G.selected = None
            return {"deleted": n, "failed": failed, "uids": uids, "total": G.total(), **arch}
        return G.run(fn)

    if path == "archive":
        def fn():
            if body.get("all"):
                folder, uids = G.last["folder"], list(G.last["uids"])
            else:
                folder, uids = G.last["folder"], [int(u) for u in body.get("uids", [])]
            if not uids:
                return {"archived": 0}
            r = archive_uids(uids, folder)
            G.selected = None
            return r
        return G.run(fn)

    if path == "config_get":
        c = load_cfg()
        return {"oa_url": c["oa_url"], "oa_key_set": bool(c["oa_key"]), "oa_verify_tls": c["oa_verify_tls"],
                "important_q": c["important_q"], "oa_max_mb": c["oa_max_mb"], "oa_origin": c["oa_origin"], "archive_dir": str(ARCHIVE_DIR)}

    if path == "config_save":
        c = load_cfg()
        c["oa_url"] = (body.get("oa_url") or "").strip()
        if body.get("oa_key"):
            c["oa_key"] = body["oa_key"].strip()
        if body.get("clear_key"):
            c["oa_key"] = ""
        c["oa_verify_tls"] = bool(body.get("oa_verify_tls", True))
        c["oa_origin"] = (body.get("oa_origin") or "").strip().rstrip("/")
        c["important_q"] = (body.get("important_q") or "").strip() or DEFAULT_CFG["important_q"]
        try:
            c["oa_max_mb"] = max(5, int(body.get("oa_max_mb") or 90))
        except ValueError:
            c["oa_max_mb"] = 90
        save_cfg(c)
        return {"ok": True}

    if path == "oa_test":
        c = load_cfg()
        if body.get("oa_url"):
            c["oa_url"] = body["oa_url"]
        if body.get("oa_key"):
            c["oa_key"] = body["oa_key"]
        if "oa_verify_tls" in body:
            c["oa_verify_tls"] = bool(body["oa_verify_tls"])
        if "oa_origin" in body:
            c["oa_origin"] = body["oa_origin"]
        srcs = oa_request(c, "GET", "/ingestion-sources", timeout=15)
        n = len(srcs) if isinstance(srcs, list) else len(srcs.get("data", srcs.get("items", [])) or [])
        origin = oa_find_origin(c)
        return {"ok": True, "sources": n, "origin": origin}

    raise GmailError("Unknown endpoint")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, payload, ctype="application/json"):
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _authorised(self):
        return secrets.compare_digest(self.headers.get("X-Token", ""), TOKEN)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            return self._send(200, PAGE_HTML.replace("__TOKEN__", TOKEN).encode(), "text/html; charset=utf-8")
        if self.path == "/api/progress":
            if not self._authorised():
                return self._send(403, {"error": "Forbidden"})
            return self._send(200, PROGRESS)
        self._send(404, {"error": "Not found"})

    def do_POST(self):
        if not self.path.startswith("/api/") or not self._authorised():
            return self._send(403, {"error": "Forbidden"})
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
            result = api(self.path[5:], body)
            self._send(200, result)
        except GmailError as e:
            self._send(400, {"error": str(e)})
        except imaplib.IMAP4.error as e:
            self._send(400, {"error": f"Gmail said: {e}"})
        except Exception as e:
            self._send(500, {"error": f"{type(e).__name__}: {e}"})
        finally:
            set_progress("")


PAGE_HTML = r'''<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Inbox Sweep</title>
<style>
:root{--bg:#0f1115;--panel:#171a21;--panel2:#1e222b;--line:#2a2f3a;--text:#e6e8ec;--muted:#8b92a1;--accent:#4f8cff;--accent2:#3a6fd6;--danger:#ef4d5a;--danger2:#c93a46;--ok:#3ccf8e}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
header{display:flex;align-items:center;gap:12px;padding:14px 22px;border-bottom:1px solid var(--line);background:var(--panel);position:sticky;top:0;z-index:5}
header h1{font-size:17px;margin:0;font-weight:650}header h1 span{color:var(--accent)}header .acct{margin-left:auto;color:var(--muted);font-size:13px}
main{max-width:1180px;margin:0 auto;padding:22px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:18px;margin-bottom:16px}.card h2{margin:0 0 8px;font-size:15px}
.muted{color:var(--muted)}.small{font-size:12px}
input[type=text],input[type=password],input[type=email],select{background:var(--panel2);color:var(--text);border:1px solid var(--line);border-radius:7px;padding:9px 11px;font:inherit;outline:none}
input:focus,select:focus{border-color:var(--accent)}
button{font:inherit;border:1px solid var(--line);background:var(--panel2);color:var(--text);padding:8px 13px;border-radius:7px;cursor:pointer;white-space:nowrap}
button:hover:not(:disabled){border-color:#3b4252;background:#252a35}button:disabled{opacity:.45;cursor:not-allowed}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}button.primary:hover:not(:disabled){background:var(--accent2)}
button.danger{background:var(--danger);border-color:var(--danger);color:#fff}button.danger:hover:not(:disabled){background:var(--danger2)}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.grow{flex:1;min-width:220px}
.login{max-width:440px;margin:40px auto}.login input{width:100%;margin-bottom:10px}
.chips{display:flex;flex-direction:column;gap:7px;margin-top:12px}
.fgroup{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.flabel{width:96px;font-size:12px;color:var(--muted)}.flabel i{font-style:normal;opacity:.7}
.chip.on{background:var(--accent);border-color:var(--accent);color:#fff}
.linkbtn{background:none;border:none;color:var(--accent);padding:0 6px;font-size:12px}.linkbtn:hover:not(:disabled){background:none;text-decoration:underline}
.chip{font-size:12px;padding:4px 10px;border-radius:99px;background:var(--panel2);border:1px solid var(--line);color:var(--muted);cursor:pointer}.chip:hover{color:var(--text);border-color:var(--accent)}
.opts{display:flex;gap:18px;flex-wrap:wrap;margin-top:12px;color:var(--muted);font-size:13px}.opts label{display:flex;align-items:center;gap:6px;cursor:pointer}.perm{color:var(--danger);font-weight:600}
.tabs{display:flex;gap:4px;margin-bottom:-1px}.tab{border-radius:8px 8px 0 0;border-bottom:none;background:transparent;color:var(--muted)}.tab.active{background:var(--panel);color:var(--text);border-color:var(--line)}
.toolbar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;padding-bottom:12px;border-bottom:1px solid var(--line);margin-bottom:4px}.count{font-weight:600}
.banner{background:#1d2a44;border:1px solid #2d4370;padding:9px 12px;border-radius:7px;margin:10px 0;font-size:13px}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:500;font-size:12px;text-transform:uppercase;letter-spacing:.4px}
tr.msg{cursor:pointer}tr.msg:hover td{background:#1b1f27}tr.msg.sel td{background:#1a2333}td.cb,th.cb{width:30px}
td.from{width:26%}.name{font-weight:550}.addr{color:var(--muted);font-size:12px;word-break:break-all}
td.date,td.size{white-space:nowrap;color:var(--muted);font-size:12px;width:1%}td.num{text-align:right;font-variant-numeric:tabular-nums;width:1%;white-space:nowrap}
.empty{padding:40px;text-align:center;color:var(--muted)}
.progress{height:6px;background:var(--panel2);border-radius:99px;overflow:hidden;margin-top:8px}.progress>div{height:100%;width:0;background:var(--accent);transition:width .2s}
#status{position:fixed;left:50%;bottom:18px;transform:translateX(-50%);min-width:360px;max-width:90vw;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 16px;box-shadow:0 10px 30px rgba(0,0,0,.45);display:none;z-index:20}
#status.show{display:block}#status.err{border-color:var(--danger)}#status.ok{border-color:var(--ok)}
.modal-bg{position:fixed;inset:0;background:rgba(0,0,0,.6);display:none;align-items:center;justify-content:center;z-index:30}.modal-bg.show{display:flex}
.modal{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:22px;width:min(480px,92vw)}.modal h3{margin:0 0 10px}
.modal .actions{display:flex;justify-content:flex-end;gap:10px;margin-top:18px}.modal input{width:100%;margin-top:10px}
code{background:var(--panel2);padding:1px 5px;border-radius:4px;font-size:12px}ol{padding-left:20px}ol li{margin:4px 0}a{color:var(--accent)}.hidden{display:none!important}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--muted);margin-right:7px;vertical-align:middle}.dot.on{background:var(--ok)}
#oaModal input[type=text],#oaModal input[type=password]{width:100%;margin-top:4px}
.modal input[type=checkbox]{width:auto;margin:0}
.impOpts{display:grid;grid-template-columns:1fr 1fr;gap:6px 14px;margin:6px 0 8px;font-size:13px}.impOpts label{display:flex;gap:6px;align-items:center;cursor:pointer}
</style></head><body>
<header><h1>Inbox <span>Sweep</span></h1><div class="acct" id="acct"></div><button id="btnOA" class="hidden"><span class="dot" id="oaDot"></span>Open Archiver</button><button id="btnOut" class="hidden">Log out</button></header>
<main>
<section class="card login" id="loginCard">
  <h2>Log in to Gmail</h2>
  <p class="muted small">Use a Google <b>App Password</b>, not your normal password. To make one:</p>
  <ol class="muted small">
    <li>Turn on <a href="https://myaccount.google.com/signinoptions/twosv" target="_blank" rel="noopener">2-Step Verification</a> if it isn't on already.</li>
    <li>Go to <a href="https://myaccount.google.com/apppasswords" target="_blank" rel="noopener">myaccount.google.com/apppasswords</a>, type a name like "Inbox Sweep" and click Create.</li>
    <li>Copy the 16-letter code into the box below.</li>
  </ol>
  <input type="email" id="email" placeholder="you@gmail.com" autocomplete="username">
  <input type="password" id="pw" placeholder="App Password (xxxx xxxx xxxx xxxx)" autocomplete="current-password">
  <button class="primary" id="btnLogin" style="width:100%">Log in</button>
  <p class="muted small" style="margin-top:10px">The code stays in this program's memory on your computer and is only sent to Gmail. You can revoke it any time on the same Google page.</p>
</section>

<section id="app" class="hidden">
  <div class="card">
    <div class="row"><input type="text" id="q" class="grow" placeholder="Gmail search, e.g.  from:newsletter@example.com older_than:1y"><button class="primary" id="btnSearch">Search</button></div>
    <div class="chips" id="chips"></div>
    <div class="opts">
      <label>Important emails:
        <select id="impMode">
          <option value="skip" selected>Skip them (never delete)</option>
          <option value="archive">Archive to Open Archiver, then delete</option>
          <option value="archive_all">Archive everything to Open Archiver, then delete</option>
          <option value="delete">Delete them too</option>
        </select></label>
      <label><input type="radio" name="mode" value="trash" checked> Move to Trash <span class="small">(recoverable for 30 days)</span></label>
      <label class="perm"><input type="radio" name="mode" value="perm"> Permanently delete <span class="small">(can't be undone)</span></label>
    </div>
  </div>
  <div class="tabs"><button class="tab active" data-tab="messages">Messages</button><button class="tab" data-tab="senders">Top senders</button></div>
  <div class="card" id="tab-messages" style="border-top-left-radius:0">
    <div class="toolbar">
      <span class="count" id="selCount">0 selected</span><span class="muted small" id="info"></span><span style="flex:1"></span>
      <button id="btnAll" disabled>Select all matching</button><button id="btnClear" disabled>Clear</button><button id="btnArch" disabled title="Copy to Open Archiver without deleting">Archive selected</button><button class="danger" id="btnDel" disabled>Delete selected</button>
    </div>
    <div class="banner hidden" id="banner"></div>
    <table><thead><tr><th class="cb"><input type="checkbox" id="cbPage"></th><th>From</th><th>Subject</th><th>Date</th><th>Size</th></tr></thead><tbody id="rows"></tbody></table>
    <div class="empty" id="empty">Run a search, or click one of the chips above for a common clean-up.</div>
    <div class="row" style="justify-content:center;margin-top:12px"><button id="btnMore" class="hidden">Load more</button></div>
  </div>
  <div class="card hidden" id="tab-senders" style="border-top-left-radius:0">
    <div class="toolbar">
      <span class="muted">Scan the</span>
      <select id="scanSize"><option value="500">latest 500</option><option value="1000" selected>latest 1,000</option><option value="2500">latest 2,500</option><option value="5000">latest 5,000</option><option value="10000">latest 10,000</option></select>
      <span class="muted">emails matching the search box (or all mail, if it's empty).</span><span style="flex:1"></span><button class="primary" id="btnScan">Scan</button>
    </div>
    <table><thead><tr><th>Sender</th><th class="num">Emails</th><th class="num">Size</th><th style="width:1%"></th></tr></thead><tbody id="senderRows"></tbody></table>
    <div class="empty" id="emptyS">Find out who's filling your inbox, then clear a sender out in one click.</div>
  </div>
</section>
</main>
<div id="status"><div id="statusText"></div><div class="progress hidden" id="statusProg"><div></div></div></div>
<div class="modal-bg" id="oaModal"><div class="modal" style="width:min(560px,94vw)">
  <h3>Open Archiver</h3>
  <p class="muted small" style="margin-top:0">Important emails are copied into your Open Archiver as an EML import, and they're only deleted from Gmail once Open Archiver confirms the import finished.</p>
  <label class="small muted">Open Archiver address</label>
  <input type="text" id="oaUrl" placeholder="http://192.168.1.50:3000">
  <label class="small muted" style="display:block;margin-top:10px">API key <span id="oaKeyState"></span></label>
  <input type="password" id="oaKey" placeholder="From Open Archiver > Settings > API Keys" autocomplete="off">
  <label class="small muted" style="display:block;margin-top:10px">Open Archiver APP_URL <span style="opacity:.7">(optional, worked out automatically. Must match <code>APP_URL</code> in Open Archiver's .env.)</span></label>
  <input type="text" id="oaOrigin" placeholder="e.g. http://localhost:3000 or https://archive.example.com">
  <label class="small" style="display:flex;gap:6px;align-items:center;margin-top:8px;color:var(--muted)"><input type="checkbox" id="oaTls" checked> Verify HTTPS certificate (untick for a self-signed cert)</label>
  <label class="small muted" style="display:block;margin-top:10px">Max upload size (MB). Archives are split into parts under this. Keep it below Open Archiver's <code>BODY_SIZE_LIMIT</code> (only 512K if that's not set in its .env).</label>
  <input type="text" id="oaMax" placeholder="90" inputmode="numeric" style="width:120px!important">
  <label class="small muted" style="display:block;margin-top:12px">What counts as important</label>
  <div class="impOpts" id="impOpts">
    <label><input type="checkbox" data-q="is:starred"> Starred</label>
    <label><input type="checkbox" data-q="is:important"> Marked important by Gmail</label>
    <label><input type="checkbox" data-q="category:updates"> Updates tab <span class="muted">(orders, receipts, bills)</span></label>
    <label><input type="checkbox" data-q="category:promotions"> Promotions tab</label>
  </div>
  <input type="text" id="impQ" placeholder="Anything else (Gmail search), e.g. from:(ato.gov.au OR mybank.com.au)">
  <p class="muted small" style="margin-bottom:0">Important = <code id="impPreview"></code></p>
  <p class="muted small" id="oaDir"></p>
  <div class="actions" style="justify-content:space-between">
    <button id="oaTest">Test connection</button>
    <div class="row"><button id="oaCancel">Cancel</button><button class="primary" id="oaSave">Save</button></div>
  </div>
</div></div>
<div class="modal-bg" id="modal"><div class="modal"><h3 id="mTitle"></h3><div id="mBody" class="muted"></div><input type="text" id="mInput" class="hidden" autocomplete="off"><div class="actions"><button id="mCancel">Cancel</button><button class="danger" id="mOk">Delete</button></div></div></div>
<script>
(() => {
'use strict';
const TOKEN = '__TOKEN__';
// Filter groups. 'any' = OR within the group, 'one' = pick one (clicking another swaps), 'all' = AND.
const FILTERS = [
  {id:'cat', label:'Category', kind:'any', items:[['Promotions','category:promotions'],['Social','category:social'],['Updates','category:updates'],['Forums','category:forums']]},
  {id:'age', label:'Older than', kind:'one', items:[['6 months','older_than:6m'],['1 year','older_than:1y'],['2 years','older_than:2y'],['3 years','older_than:3y'],['5 years','older_than:5y']]},
  {id:'size', label:'Bigger than', kind:'one', items:[['5 MB','larger:5M'],['10 MB','larger:10M'],['25 MB','larger:25M']]},
  {id:'read', label:'Status', kind:'one', items:[['Unread','is:unread'],['Read','is:read']]},
  {id:'has', label:'Has', kind:'all', items:[['Attachment','has:attachment'],['"Unsubscribe"','unsubscribe'],['No label','has:nouserlabels']]},
  {id:'loc', label:'Folder', kind:'one', items:[['Trash','in:trash'],['Spam','in:spam']]},
];
const active = new Set();   // "groupId|query"
function filterQuery(){
  const parts=[];
  for(const g of FILTERS){
    const on=g.items.filter(([,q])=>active.has(g.id+'|'+q)).map(([,q])=>q);
    if(!on.length) continue;
    parts.push(g.kind==='any' && on.length>1 ? '{'+on.join(' ')+'}' : on.join(' '));
  }
  return parts.join(' ');
}
const buildQuery = (base=$('q').value) => [base.trim(), filterQuery()].filter(Boolean).join(' ');
const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmtNum = n => Number(n).toLocaleString();
const fmtSize = b => b>=1e9?(b/1e9).toFixed(1)+' GB':b>=1e6?(b/1e6).toFixed(1)+' MB':b>=1e3?Math.round(b/1e3)+' KB':(b||0)+' B';
function fmtDate(ms){ if(!ms) return ''; const d=new Date(ms),n=new Date();
  if(d.toDateString()===n.toDateString()) return d.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});
  if(d.getFullYear()===n.getFullYear()) return d.toLocaleDateString([],{day:'numeric',month:'short'});
  return d.toLocaleDateString([],{day:'numeric',month:'short',year:'numeric'}); }

let stTimer, pollTimer;
function status(text, kind='', frac=null, sticky=false){
  $('status').className='show '+kind; $('statusText').textContent=text;
  const p=$('statusProg'); if(frac==null) p.classList.add('hidden'); else { p.classList.remove('hidden'); p.firstElementChild.style.width=Math.min(100,frac*100)+'%'; }
  clearTimeout(stTimer); if(!sticky) stTimer=setTimeout(()=>$('status').className='', kind==='err'?9000:3500);
}
async function call(path, body={}){
  pollTimer = setInterval(async()=>{ try{ const r=await fetch('/api/progress',{headers:{'X-Token':TOKEN}}); const p=await r.json(); if(p.text) status(p.text,'',p.frac,true);}catch{} }, 500);
  try{
    const r = await fetch('/api/'+path,{method:'POST',headers:{'Content-Type':'application/json','X-Token':TOKEN},body:JSON.stringify(body)});
    const j = await r.json().catch(()=>({error:'Bad response from Inbox Sweep'}));
    if(!r.ok) throw new Error(j.error||('Error '+r.status));
    return j;
  } catch(e){ if(e instanceof TypeError) throw new Error('Inbox Sweep isn\'t running. Start it again in your terminal.'); throw e; }
  finally{ clearInterval(pollTimer); }
}
function confirmModal({title, body, okText, requireText}){
  return new Promise(res=>{
    $('mTitle').textContent=title; $('mBody').innerHTML=body; $('mOk').textContent=okText;
    const inp=$('mInput'); inp.value=''; inp.classList.toggle('hidden',!requireText); inp.placeholder=requireText?`Type ${requireText} to confirm`:'';
    $('mOk').disabled=!!requireText; inp.oninput=()=>{$('mOk').disabled=inp.value.trim().toUpperCase()!==requireText;};
    $('modal').classList.add('show'); if(requireText) setTimeout(()=>inp.focus(),50);
    const done=v=>{$('modal').classList.remove('show');res(v);}; $('mOk').onclick=()=>done(true); $('mCancel').onclick=()=>done(false);
  });
}

const S = { items: [], total: 0, selected: new Set(), all: false, q: '' };
const impMode = () => $('impMode').value;
const protect = () => impMode()==='skip';
let OA = {configured:false};
const mode = () => document.querySelector('input[name=mode]:checked').value;

// ---- login ----
$('btnLogin').onclick = async () => {
  const email=$('email').value.trim(), password=$('pw').value;
  if(!email||!password) return status('Enter your email and App Password','err');
  $('btnLogin').disabled=true; status('Logging in…','',null,true);
  try{ const r=await call('login',{email,password}); $('pw').value=''; setAcct(r);
    $('loginCard').classList.add('hidden'); $('app').classList.remove('hidden'); $('btnOut').classList.remove('hidden'); $('btnOA').classList.remove('hidden'); loadCfg(); status('Logged in','ok'); $('q').focus();
  } catch(e){ status(e.message,'err'); } finally{ $('btnLogin').disabled=false; }
};
$('pw').addEventListener('keydown',e=>{ if(e.key==='Enter') $('btnLogin').click(); });
function setAcct(r){ $('acct').textContent = r.email ? `${r.email}${r.total!=null?' · '+fmtNum(r.total)+' emails':''}` : (r.total!=null ? $('acct').textContent.replace(/·.*$/,'· '+fmtNum(r.total)+' emails') : ''); }
$('btnOut').onclick = async () => { try{ await call('logout'); }catch{} location.reload(); };

// ---- search ----
async function search(){
  S.q=buildQuery(); S.selected.clear(); S.all=false; $('btnSearch').disabled=true;
  try{ const r=await call('search',{q:S.q,protect:protect()}); S.items=r.items; S.total=r.total; render();
    status(r.total?`${fmtNum(r.total)} emails match`:'No emails match that search','ok');
  } catch(e){ status(e.message,'err'); } finally{ $('btnSearch').disabled=false; }
}
async function more(){
  $('btnMore').disabled=true;
  try{ const r=await call('page',{offset:S.items.length}); S.items.push(...r.items); if(S.all) r.items.forEach(m=>S.selected.add(m.uid)); render(); }
  catch(e){ status(e.message,'err'); } finally{ $('btnMore').disabled=false; }
}
function render(){
  $('rows').innerHTML = S.items.map(m=>{ const sel=S.all||S.selected.has(m.uid);
    return `<tr class="msg${sel?' sel':''}" data-uid="${m.uid}"><td class="cb"><input type="checkbox" ${sel?'checked':''}></td>
    <td class="from"><div class="name">${esc(m.name)}</div><div class="addr">${esc(m.addr)}</div></td>
    <td>${esc(m.subject)}</td><td class="date">${fmtDate(m.date)}</td><td class="size">${fmtSize(m.size)}</td></tr>`; }).join('');
  $('empty').classList.toggle('hidden', S.items.length>0);
  $('btnMore').classList.toggle('hidden', S.items.length>=S.total);
  $('info').textContent = S.total ? `Showing ${fmtNum(S.items.length)} of ${fmtNum(S.total)} matching` : '';
  updSel();
}
function updSel(){
  const n = S.all ? S.total : S.selected.size;
  $('selCount').textContent = `${fmtNum(n)} selected`;
  $('btnDel').disabled=!n; $('btnClear').disabled=!n; $('btnArch').disabled=!n; $('btnAll').disabled=!S.total||S.all;
  const pageAll=S.items.length>0&&(S.all||S.items.every(m=>S.selected.has(m.uid)));
  $('cbPage').checked=pageAll; $('cbPage').indeterminate=!pageAll&&S.items.some(m=>S.selected.has(m.uid));
  $('banner').classList.toggle('hidden',!S.all);
  if(S.all) $('banner').innerHTML=`All <b>${fmtNum(S.total)}</b> emails matching <code>${esc(S.q||'(all mail)')}</code> are selected, including ones not loaded below.`;
}
$('rows').addEventListener('click', e=>{
  const tr=e.target.closest('tr.msg'); if(!tr) return; const uid=Number(tr.dataset.uid);
  if(S.all){ S.all=false; S.items.forEach(m=>S.selected.add(m.uid)); }
  S.selected.has(uid)?S.selected.delete(uid):S.selected.add(uid); render();
});
$('cbPage').onchange = e => { S.all=false; S.items.forEach(m=> e.target.checked?S.selected.add(m.uid):S.selected.delete(m.uid)); render(); };
$('btnAll').onclick = () => { S.all=true; render(); };
$('btnClear').onclick = () => { S.all=false; S.selected.clear(); render(); };
$('btnSearch').onclick = search; $('btnMore').onclick = more;
$('q').addEventListener('keydown', e=>{ if(e.key==='Enter') search(); });

// ---- delete ----
const doneMsg = (n, a) => `${a?`Archived ${fmtNum(a)} to Open Archiver · `:''}${mode()==='perm'?'Permanently deleted':'Moved to Trash:'} ${fmtNum(n)} emails`;
function archiveNote(){
  const m=impMode();
  if(m==='archive') return `<b>Important emails</b> (<code>${esc(OA.important_q)}</code>) are copied to Open Archiver first. Nothing is deleted unless that import succeeds.<br><br>`;
  if(m==='archive_all') return `<b>Every one</b> of these is copied to Open Archiver first. Nothing is deleted unless that import succeeds.<br><br>`;
  return '';
}
function archOk(){
  if(['archive','archive_all'].includes(impMode()) && !OA.configured){ openOA(); status('Set up Open Archiver first','err'); return false; }
  return true;
}
async function confirmDelete(n, desc){
  if(!archOk()) return false;
  const perm = mode()==='perm';
  return confirmModal({
    title: perm?`Permanently delete ${fmtNum(n)} emails?`:`Move ${fmtNum(n)} emails to Trash?`,
    body: (desc?`This covers ${desc}.<br><br>`:'') + archiveNote() + (perm?'<b style="color:var(--danger)">They\'ll be gone for good.</b> They skip the Trash and can\'t be recovered.':'You can restore them from Gmail\'s Trash for 30 days.'),
    okText: perm?'Delete forever':'Move to Trash', requireText: (perm||n>=5000)?'DELETE':null });
}
$('btnDel').onclick = async () => {
  const n = S.all?S.total:S.selected.size;
  if(!await confirmDelete(n, S.all?`everything matching <code>${esc(S.q||'(all mail)')}</code>`:null)) return;
  $('btnDel').disabled=true;
  try{
    const r = await call('delete', S.all?{all:true,mode:mode(),imp_mode:impMode()}:{uids:[...S.selected],mode:mode(),imp_mode:impMode()});
    setAcct({total:r.total});
    if(r.failed){ await search(); return status(`${doneMsg(r.deleted,r.archived)}. Gmail refused ${fmtNum(r.failed)} after several retries. They're still listed, so try Delete again in a minute.`,'err'); }
    const gone=new Set(r.uids); S.items=S.items.filter(m=>!gone.has(m.uid)); S.total=Math.max(0,S.total-r.uids.length);
    S.selected.clear(); S.all=false; render();
    status(doneMsg(r.deleted,r.archived),'ok');
  } catch(e){ status(e.message,'err'); updSel(); }
};
$('btnArch').onclick = async () => {
  if(!OA.configured){ openOA(); return status('Set up Open Archiver first','err'); }
  const n = S.all?S.total:S.selected.size;
  if(!await confirmModal({title:`Archive ${fmtNum(n)} emails to Open Archiver?`, body:'They\'re copied into Open Archiver and stay in Gmail.', okText:'Archive'})) return;
  $('btnArch').disabled=true;
  try{ const r=await call('archive', S.all?{all:true}:{uids:[...S.selected]}); status(`Archived ${fmtNum(r.archived)} emails to Open Archiver`,'ok'); }
  catch(e){ status(e.message,'err'); } finally{ updSel(); }
};

// ---- Open Archiver settings ----
async function loadCfg(){
  try{ const c=await call('config_get'); OA={...c, configured: !!(c.oa_url && c.oa_key_set)};
    $('oaDot').classList.toggle('on', OA.configured);
    $('impMode').querySelectorAll('option[value^=archive]').forEach(o=>o.textContent=o.textContent.replace(' (set up first)','')+(OA.configured?'':' (set up first)'));
  }catch{}
}
function openOA(){
  $('oaUrl').value=OA.oa_url||''; $('oaKey').value=''; $('oaTls').checked=OA.oa_verify_tls!==false; setImp(OA.important_q||'is:starred OR is:important'); $('oaMax').value=OA.oa_max_mb||90; $('oaOrigin').value=OA.oa_origin||'';
  $('oaKeyState').textContent=OA.oa_key_set?'(saved; leave blank to keep it)':'';
  $('oaDir').innerHTML=OA.archive_dir?`A copy of every archive zip is also kept at <code>${esc(OA.archive_dir)}</code>.`:'';
  $('oaModal').classList.add('show');
}
const IMP_PRESETS = [...document.querySelectorAll('#impOpts input')].map(i=>i.dataset.q);
function setImp(q){
  const parts=[]; let depth=0, cur='';
  const toks=q.split(/(\s+OR\s+|[()])/i);
  for(const t of toks){ if(t==='(') depth++; if(t===')') depth--;
    if(depth===0 && /^\s+OR\s+$/i.test(t)){ parts.push(cur.trim()); cur=''; } else cur+=t; }
  parts.push(cur.trim());
  parts.splice(0, parts.length, ...parts.filter(Boolean));
  document.querySelectorAll('#impOpts input').forEach(i=>i.checked=parts.includes(i.dataset.q));
  $('impQ').value = parts.filter(x=>!IMP_PRESETS.includes(x)).join(' OR ');
  updImp();
}
function getImp(){
  const parts=[...document.querySelectorAll('#impOpts input:checked')].map(i=>i.dataset.q);
  const extra=$('impQ').value.trim(); if(extra) parts.push(extra.includes(' ')&&!/^\(.*\)$/.test(extra)?`(${extra})`:extra);
  return parts.join(' OR ') || 'is:starred';
}
function updImp(){ $('impPreview').textContent=getImp(); }
$('impOpts').onchange=updImp; $('impQ').oninput=updImp;
const oaForm = () => ({oa_url:$('oaUrl').value.trim(), oa_key:$('oaKey').value.trim(), oa_verify_tls:$('oaTls').checked, important_q:getImp(), oa_max_mb:$('oaMax').value.trim(), oa_origin:$('oaOrigin').value.trim()});
$('btnOA').onclick = openOA;
$('oaCancel').onclick = () => $('oaModal').classList.remove('show');
$('oaTest').onclick = async () => {
  $('oaTest').disabled=true;
  try{ const r=await call('oa_test', oaForm()); status(`Connected to Open Archiver. Uploads OK (Origin ${r.origin}), ${r.sources} ingestion sources`,'ok'); if(!$('oaOrigin').value) $('oaOrigin').value=r.origin; }
  catch(e){ status(e.message,'err'); } finally{ $('oaTest').disabled=false; }
};
$('oaSave').onclick = async () => {
  try{ await call('config_save', oaForm()); $('oaModal').classList.remove('show'); await loadCfg(); status('Saved','ok'); updPreview(); }
  catch(e){ status(e.message,'err'); }
};
$('impMode').onchange = () => { if(['archive','archive_all'].includes(impMode()) && !OA.configured) openOA(); };

// ---- senders ----
let senders=[];
$('btnScan').onclick = async () => {
  $('btnScan').disabled=true;
  try{ const r=await call('senders',{q:buildQuery(),protect:protect(),limit:Number($('scanSize').value)}); senders=r.senders; renderSenders();
    status(`Scanned ${fmtNum(r.scanned)} emails`,'ok'); }
  catch(e){ status(e.message,'err'); } finally{ $('btnScan').disabled=false; }
};
function renderSenders(){
  $('senderRows').innerHTML = senders.map((s,i)=>`<tr><td><div class="name">${esc(s.name)}</div><div class="addr">${esc(s.addr)}</div></td>
    <td class="num">${fmtNum(s.count)}</td><td class="num muted">${fmtSize(s.size)}</td>
    <td><div class="row" style="flex-wrap:nowrap"><button data-a="view" data-i="${i}">View</button><button class="danger" data-a="del" data-i="${i}">Delete all</button></div></td></tr>`).join('');
  $('emptyS').classList.toggle('hidden', senders.length>0);
}
$('senderRows').addEventListener('click', async e=>{
  const b=e.target.closest('button'); if(!b) return; const s=senders[Number(b.dataset.i)]; if(!s) return;
  if(b.dataset.a==='view'){ $('q').value=`from:${s.addr}`; updPreview(); tab('messages'); return search(); }
  const q=buildQuery(`from:${s.addr}`);
  b.disabled=true;
  try{
    const {count}=await call('count',{q,protect:protect()});
    if(!count) return status('Nothing to delete','ok');
    if(!await confirmDelete(count, filterQuery() ? `every email matching <code>${esc(q)}</code>` : `every email from <code>${esc(s.addr)}</code>`)) return;
    const r=await call('delete',{q,protect:protect(),mode:mode(),imp_mode:impMode()});
    setAcct({total:r.total});
    if(r.failed) return status(`${doneMsg(r.deleted,r.archived)} from ${s.addr}. Gmail refused ${fmtNum(r.failed)}, so click Delete all again in a minute.`,'err');
    senders=senders.filter(x=>x!==s); renderSenders();
    status(`${doneMsg(r.deleted,r.archived)} from ${s.addr}`,'ok');
  } catch(err){ status(err.message,'err'); } finally{ b.disabled=false; }
});

// ---- tabs / chips ----
function tab(t){ document.querySelectorAll('.tab').forEach(x=>x.classList.toggle('active',x.dataset.tab===t));
  $('tab-messages').classList.toggle('hidden',t!=='messages'); $('tab-senders').classList.toggle('hidden',t!=='senders'); }
document.querySelectorAll('.tab').forEach(t=>t.onclick=()=>tab(t.dataset.tab));
$('chips').innerHTML = FILTERS.map(g=>`<div class="fgroup"><span class="flabel">${esc(g.label)}${g.kind==='any'?' <i>(any)</i>':''}</span>`+
  g.items.map(([l,q])=>`<span class="chip" data-g="${g.id}" data-q="${esc(q)}">${esc(l)}</span>`).join('')+'</div>').join('')+
  `<div class="fgroup"><span class="flabel">Searching</span><code id="qPreview"></code><button class="linkbtn" id="btnClearF">Clear filters</button></div>`;
let autoTimer;
function updPreview(){
  document.querySelectorAll('#chips .chip').forEach(c=>c.classList.toggle('on', active.has(c.dataset.g+'|'+c.dataset.q)));
  $('qPreview').textContent = buildQuery() || '(all mail)';
  $('btnClearF').classList.toggle('hidden', !active.size);
}
function autoSearch(){ clearTimeout(autoTimer); autoTimer=setTimeout(()=>{ if(!$('tab-senders').classList.contains('hidden')) return; search(); }, 600); }
$('chips').onclick = e=>{
  if(e.target.id==='btnClearF'){ active.clear(); updPreview(); return autoSearch(); }
  const c=e.target.closest('.chip'); if(!c) return;
  const g=FILTERS.find(x=>x.id===c.dataset.g), key=g.id+'|'+c.dataset.q;
  if(active.has(key)) active.delete(key);
  else { if(g.kind==='one') g.items.forEach(([,q])=>active.delete(g.id+'|'+q)); active.add(key); }
  updPreview(); autoSearch();
};
$('q').addEventListener('input', updPreview);
updPreview();
document.querySelectorAll('input[name=mode]').forEach(r=>r.onchange=()=>{ $('btnDel').textContent=mode()==='perm'?'Delete forever':'Delete selected'; });
$('email').focus();
})();
</script></body></html>'''


def main():
    port = PORT
    for _ in range(10):
        try:
            server = ThreadingHTTPServer((HOST, port), Handler)
            break
        except OSError:
            port += 1
    else:
        sys.exit("Couldn't find a free port.")
    url = f"http://localhost:{port}"
    print(f"\n  Inbox Sweep is running at {url}\n  Press Ctrl+C to stop.\n")
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")
        G.logout()


if __name__ == "__main__":
    main()
