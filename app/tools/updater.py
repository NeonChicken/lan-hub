# -*- coding: utf-8 -*-
"""LAN Hub updates from GitHub releases (https://github.com/<C.REPO>/releases).

  check(cfg)   ask api.github.com for the latest release (only when consent.updates is on; at most every 6 h)
  stage(cfg)   download its LAN-Hub-<version>.zip, verify the SHA-256 GitHub publishes for it, check every
               path inside, and unpack it to app/data/update/staged/ (nothing outside app/ is touched yet)
  python updater.py --apply
               run by LAN Hub.exe (or the fallback .bat) BEFORE the server starts: moves the staged files
               into place. Files in use (LAN Hub.exe, python.exe) are renamed to *.old first, which Windows
               allows, and cleaned up on the next start. On any error everything is put back.

Never replaced: app/config.json, app/data/, app/thumbs/, app/tools/curation_local.py (your settings & library).
If your current LAN Hub.exe is code-signed, the new one must be signed by the same publisher.
"""
import hashlib, json, os, re, shutil, subprocess, sys, time, urllib.error, urllib.request, zipfile
from urllib.parse import urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import config as C  # noqa: E402

UPD = os.path.join(C.DATA, "update")
STAGED = os.path.join(UPD, "staged")
READY = os.path.join(UPD, "ready.json")
OLDLIST = os.path.join(UPD, "old.json")
STATUS_FILE = os.path.join(C.DATA, "update.json")
UA = {"User-Agent": f"LANHub/{C.VERSION} (+https://github.com/{C.REPO})", "Accept": "application/vnd.github+json"}
CHECK_EVERY = 6 * 3600
MAX_ZIP = 300 * 1024 * 1024
MAX_FILES, MAX_UNPACKED = 5000, 700 * 1024 * 1024
TOP = "LAN Hub/"
REQUIRED = {"LAN Hub.exe", "app/index.html", "app/tools/lanhub.py", "app/tools/config.py"}
ASSET = re.compile(r"LAN-Hub-(\d+\.\d+\.\d+)\.zip")   # the public build (never the -friends zip)
_status = None


class Blocked(Exception):
    pass


# ------------------------------------------------------------------ status (data/update.json)
def _load():
    global _status
    if _status is None:
        try:
            with open(STATUS_FILE, encoding="utf-8") as f:
                _status = json.load(f)
            if not isinstance(_status, dict):
                _status = {}
        except Exception:
            _status = {}
        _status.update(busy=False)   # a download can't survive a restart
    return _status


def set_status(**kw):
    s = _load()
    s.update(kw)
    try:
        os.makedirs(C.DATA, exist_ok=True)
        with open(STATUS_FILE + ".tmp", "w", encoding="utf-8") as f:
            json.dump(s, f)
        os.replace(STATUS_FILE + ".tmp", STATUS_FILE)
    except OSError:
        pass


def status():
    """What the page may show."""
    s = _load()
    latest = s.get("latest")
    avail = bool(latest and newer(latest, C.VERSION) and s.get("asset"))
    return {"current": C.VERSION, "latest": latest, "available": avail, "notes": (s.get("notes") or "")[:3000],
            "page": s.get("page"), "checked": s.get("checked"), "busy": bool(s.get("busy")), "error": s.get("error"),
            "progress": s.get("progress"), "staged": os.path.exists(READY)}


def ver(v):
    m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", (v or "").strip())
    return tuple(int(x) for x in m.groups()) if m else None


def newer(a, b):
    return bool(ver(a) and ver(b) and ver(a) > ver(b))


# ------------------------------------------------------------------ network (GitHub only)
def check_url(url):
    try:
        u = urlsplit(url)
        port = u.port
    except ValueError:
        raise Blocked("blocked malformed URL")
    if u.scheme != "https" or (u.hostname or "").lower() not in C.UPDATE_HOSTS or u.username or port not in (None, 443):
        raise Blocked(f"blocked request to {u.hostname!r} (not on the update allow-list)")


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl)
        r = super().redirect_request(req, fp, code, msg, headers, newurl)
        if r is not None and urlsplit(newurl).hostname != "api.github.com":
            r.remove_header("Accept")
        return r


_OPENER = urllib.request.build_opener(_SafeRedirect)


def _get(url, limit):
    check_url(url)
    with _OPENER.open(urllib.request.Request(url, headers=UA), timeout=30) as r:
        b = r.read(limit + 1)
    if len(b) > limit:
        raise ValueError("response too large")
    return b


def check(cfg, force=False):
    """-> status() after asking GitHub, or None when update checks aren't allowed."""
    if not C.allow(cfg, "updates"):
        return None
    s = _load()
    if not force and time.time() - (s.get("checkedAt") or 0) < CHECK_EVERY:
        return status()
    try:
        j = json.loads(_get(f"https://api.github.com/repos/{C.REPO}/releases/latest", 1024 * 1024))
    except urllib.error.HTTPError as e:
        set_status(checkedAt=time.time(), checked=time.strftime("%Y-%m-%d %H:%M"),
                   error=None if e.code == 404 else f"GitHub said {e.code}")   # 404 = no release published yet
        return status()
    tag = j.get("tag_name") if isinstance(j, dict) else None
    asset, sums = None, None
    for a in (j.get("assets") or []) if isinstance(j, dict) else []:
        name, url = a.get("name") or "", a.get("browser_download_url") or ""
        if not url.startswith(f"https://github.com/{C.REPO}/releases/download/"):
            continue
        m = ASSET.fullmatch(name)
        if m and ver(m.group(1)) == ver(tag):
            d = a.get("digest") or ""
            asset = {"name": name, "url": url, "size": int(a.get("size") or 0),
                     "sha256": d[7:].lower() if d.startswith("sha256:") else None}
        elif name.endswith(".zip.sha256"):
            sums = url
    if asset and not asset["sha256"] and sums:
        try:
            m = re.search(r"\b([0-9a-fA-F]{64})\b", _get(sums, 4096).decode("ascii", "replace"))
            asset["sha256"] = m.group(1).lower() if m else None
        except Exception:
            pass
    page = j.get("html_url") if isinstance(j, dict) else None
    if not (isinstance(page, str) and page.startswith(f"https://github.com/{C.REPO}/releases/")):
        page = f"https://github.com/{C.REPO}/releases"
    set_status(checkedAt=time.time(), checked=time.strftime("%Y-%m-%d %H:%M"), latest=tag if ver(tag) else None,
               asset=asset if asset and asset["sha256"] else None, notes=str(j.get("body") or "")[:3000],
               page=page, error=None)
    return status()


# ------------------------------------------------------------------ staging (inside app/data/update only)
def protected(rel):
    r = rel.lower()
    return r == "app/config.json" or r.startswith(("app/data/", "app/thumbs/")) or r == "app/tools/curation_local.py"


def safe_rel(name):
    """Zip member -> path relative to the LAN Hub folder, or None if it's not something we'd ever write."""
    if not name.startswith(TOP) or name.endswith("/"):
        return None
    rel = name[len(TOP):]
    parts = rel.split("/")
    if (not rel or "\\" in rel or ":" in rel or len(rel) > 240 or any(p in ("", ".", "..") for p in parts)
            or any(p.endswith((" ", ".")) for p in parts)):
        raise Blocked(f"unsafe path in update: {name!r}")
    return rel


def signer(exe):
    """Authenticode signer of an exe (subject), "" if unsigned / not valid, None if it couldn't be checked."""
    if not C.IS_WIN or not os.path.isfile(exe):
        return None
    ps = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    try:
        r = subprocess.run([ps, "-NoProfile", "-NonInteractive", "-Command",
                            "$s=Get-AuthenticodeSignature -LiteralPath $env:LANHUB_EXE;"
                            "if($s.Status -eq 'Valid'){[Console]::Out.Write($s.SignerCertificate.Subject)}"],
                           capture_output=True, text=True, timeout=60, env=dict(os.environ, LANHUB_EXE=exe),
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.stdout.strip()
    except Exception:
        return None


def stage(cfg, log=print):
    if not C.allow(cfg, "updates"):
        raise PermissionError("Updates are off (Folders & permissions)")
    s = _load()
    a = s.get("asset") or {}
    if not (s.get("latest") and newer(s["latest"], C.VERSION) and a.get("url") and a.get("sha256")):
        raise ValueError("No verified update available")
    set_status(busy=True, error=None, progress=0)
    shutil.rmtree(UPD, ignore_errors=True)
    os.makedirs(STAGED, exist_ok=True)
    zpath = os.path.join(UPD, "download.zip")
    log(f"Downloading {a['name']}…")
    check_url(a["url"])
    h, n = hashlib.sha256(), 0
    with _OPENER.open(urllib.request.Request(a["url"], headers=UA), timeout=60) as r, open(zpath, "wb") as f:
        while True:
            b = r.read(1 << 16)
            if not b:
                break
            n += len(b)
            if n > MAX_ZIP:
                raise ValueError("update download too large")
            h.update(b)
            f.write(b)
            if a.get("size"):
                pct = min(99, n * 100 // a["size"])
                if pct != _load().get("progress"):
                    _load()["progress"] = pct
    if h.hexdigest() != a["sha256"]:
        raise Blocked("download doesn't match the checksum GitHub published: not installing it")
    files, total = [], 0
    with zipfile.ZipFile(zpath) as z:
        infos = z.infolist()
        if len(infos) > MAX_FILES:
            raise Blocked("too many files in update")
        for i in infos:
            rel = safe_rel(i.filename)
            if rel is None or protected(rel):
                continue
            total += i.file_size
            if total > MAX_UNPACKED:
                raise Blocked("update unpacks too large")
            dest = os.path.join(STAGED, *rel.split("/"))
            if not C.is_within(dest, STAGED):
                raise Blocked(f"unsafe path in update: {rel!r}")
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with z.open(i) as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)
            files.append(rel)
    missing = REQUIRED - set(files)
    if missing:
        raise Blocked("update is incomplete: " + ", ".join(sorted(missing)))
    mine = signer(os.path.join(C.HUB, "LAN Hub.exe"))
    if mine:  # we're signed: only accept an exe signed by the same publisher
        theirs = signer(os.path.join(STAGED, "LAN Hub.exe"))
        if theirs != mine:
            raise Blocked("the new LAN Hub.exe isn't signed by the same publisher: not installing it")
    with open(READY + ".tmp", "w", encoding="utf-8") as f:
        json.dump({"version": s["latest"], "files": files, "from": C.VERSION}, f)
    os.replace(READY + ".tmp", READY)
    os.remove(zpath)
    set_status(busy=False, progress=100, error=None)
    log(f"Update {s['latest']} verified and ready ({len(files)} files).")
    return True


# ------------------------------------------------------------------ apply (before the server runs)
def _same(a, b):
    try:
        if os.path.getsize(a) != os.path.getsize(b):
            return False
        with open(a, "rb") as x, open(b, "rb") as y:
            while True:
                p, q = x.read(1 << 20), y.read(1 << 20)
                if p != q:
                    return False
                if not p:
                    return True
    except OSError:
        return False


def cleanup_old():
    try:
        with open(OLDLIST, encoding="utf-8") as f:
            olds = json.load(f)
    except Exception:
        return
    left = []
    for p in olds if isinstance(olds, list) else []:
        if isinstance(p, str) and p.endswith(".lanhub-old") and C.is_within(p, C.HUB):
            try:
                os.remove(p)
            except FileNotFoundError:
                pass
            except OSError:
                left.append(p)   # still in use: next time
    if left:
        with open(OLDLIST, "w", encoding="utf-8") as f:
            json.dump(left, f)
    else:
        try:
            os.remove(OLDLIST)
        except OSError:
            pass


def apply(out=print):
    cleanup_old()
    try:
        with open(READY, encoding="utf-8") as f:
            ready = json.load(f)
    except FileNotFoundError:
        return 0
    except Exception as e:
        out(f"UPDATE_FAILED unreadable ready.json: {e}")
        shutil.rmtree(UPD, ignore_errors=True)
        return 1
    done, olds, exe_changed = [], [], False   # done: (dest, backup or None)
    try:
        for rel in ready.get("files") or []:
            if not isinstance(rel, str) or protected(rel) or safe_rel(TOP + rel) != rel:
                raise Blocked(f"unsafe path {rel!r}")
            src = os.path.join(STAGED, *rel.split("/"))
            dst = os.path.join(C.HUB, *rel.split("/"))
            if not (C.is_within(src, STAGED) and C.is_within(dst, C.HUB)) or not os.path.isfile(src):
                raise Blocked(f"unsafe path {rel!r}")
            if os.path.exists(dst) and _same(src, dst):
                continue
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            bak = None
            if os.path.exists(dst):
                bak = dst + f".{int(time.time())}.lanhub-old"
                os.replace(dst, bak)      # works even for a running exe / loaded dll
            os.replace(src, dst)
            done.append((dst, bak))
            if bak:
                olds.append(bak)
            exe_changed |= rel == "LAN Hub.exe"
    except Exception as e:
        for dst, bak in reversed(done):  # put everything back the way it was
            try:
                if bak:
                    os.replace(bak, dst)
                else:
                    os.remove(dst)
            except OSError:
                pass
        shutil.rmtree(UPD, ignore_errors=True)
        set_status(error=f"update could not be installed: {e}")
        out(f"UPDATE_FAILED {e}")
        return 1
    shutil.rmtree(STAGED, ignore_errors=True)
    for p in (READY, os.path.join(UPD, "download.zip")):
        try:
            os.remove(p)
        except OSError:
            pass
    if olds:
        os.makedirs(UPD, exist_ok=True)
        with open(OLDLIST, "w", encoding="utf-8") as f:
            json.dump(olds, f)
        cleanup_old()
    set_status(error=None, progress=None, applied=ready.get("version"))
    out(f"UPDATE_APPLIED {ready.get('version')} ({len(done)} files changed)")
    if exe_changed:
        out("RESTART_LAUNCHER")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if "--apply" in sys.argv:
        sys.exit(apply())
    if "--check" in sys.argv:
        print(json.dumps(check(C.load(), force=True), indent=1))
