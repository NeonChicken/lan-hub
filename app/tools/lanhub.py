# -*- coding: utf-8 -*-
"""LAN Hub server: serves the hub on http://127.0.0.1:<port>, scans this PC (only what the
user allowed on the consent screen), and launches games / installers / folders for the page.

  python tools/lanhub.py              start (opens the browser)
  python tools/lanhub.py --no-browser (used by LAN Hub.exe, which opens the browser itself)
  python tools/lanhub.py --rescan     rebuild data once and exit

Permission locks enforced here:
  * listens on 127.0.0.1 only; API needs Host=127.0.0.1:<port>, same Origin and an X-LANHub header
  * only serves the page, its assets, artwork and the game list (nothing else from disk)
  * only launches/opens paths from its own scan, and only inside approved folders / Steam libraries /
    folders of games found after an install or picked by you (data/places.json, written by the scan)
  * steam:// links and Xbox app IDs (shell:AppsFolder) must match a fixed pattern
  * updates: only from the GitHub releases of C.REPO, checked and staged by updater.py
"""
import json, os, re, secrets, shutil, socket, subprocess, sys, threading, time, traceback, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import config as C  # noqa: E402
import build as B  # noqa: E402
import exeinfo, installed, updater  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
except Exception:
    pass

STEAM_URL = re.compile(r"steam://(rungameid|install|store|nav/games/details|url/GameHub|url/StoreAppPage)/\d{1,10}")  # used with fullmatch
STATE = {"scanning": False, "phase": "idle", "log": [], "built": None, "error": None}
LOCK = threading.Lock()
INSTALLS = {}   # game id -> {"since", "before"}: installers started from the page that are still running
RESTART_CODE = 75   # tells LAN Hub.exe "restart me": it applies a staged update before starting the server again
IPS = []
SERVE_DIRS = ("assets/", "thumbs/")
SERVE_FILES = {"index.html", "data/games.json"}  # games.js is only for opening index.html from disk; never served
SAFE_URL_PATH = re.compile(r"[A-Za-z0-9_\-./]{1,200}")  # what our own page ever requests
SYS = os.environ.get("SystemRoot", r"C:\Windows")
POWERSHELL = os.path.join(SYS, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
EXPLORER = os.path.join(SYS, "explorer.exe")
SERVE_EXT = {".json", ".png", ".jpg", ".jpeg", ".webp", ".ico", ".woff2"}  # + index.html itself; nothing script-capable
MIME = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
        ".json": "application/json; charset=utf-8", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".webp": "image/webp", ".ico": "image/x-icon", ".svg": "image/svg+xml", ".gif": "image/gif",
        ".woff2": "font/woff2"}


def out(line):
    try:
        print(line, flush=True)
    except Exception:  # pythonw: no stdout
        pass


def log(*a):
    line = " ".join(str(x) for x in a)
    out("  " + line)
    with LOCK:
        STATE["log"] = (STATE["log"] + [line])[-40:]


# ------------------------------------------------------------------ scanning
def rescan(online=True):
    with LOCK:
        if STATE["scanning"]:
            STATE["again"] = True  # settings changed mid-scan: run once more afterwards
            return False
        STATE.update(scanning=True, error=None, again=False)

    def run():
        try:
            while True:
                cfg = C.load()
                if not C.consented(cfg):
                    log("Waiting for permission (consent screen) before scanning anything.")
                    break
                STATE["phase"] = "quick scan"
                STATE["built"] = B.build(cfg, online=False, log=log)["built"]
                if online and C.allow(cfg, "internet"):
                    STATE["phase"] = "fetching artwork & descriptions"
                    STATE["built"] = B.build(cfg, online=True, log=log)["built"]
                with LOCK:
                    if not STATE.get("again"):
                        break
                    STATE["again"] = False
        except Exception as e:
            STATE["error"] = f"{e.strerror}: {e.filename}" if isinstance(e, OSError) and e.filename else str(e)
            log("! scan failed:", STATE["error"])
            traceback.print_exc()
        finally:
            with LOCK:
                STATE.update(scanning=False, phase="idle")
    threading.Thread(target=run, daemon=True).start()
    return True


def lan_ips():
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))  # no packet is sent; picks the default-route interface
        ips.append(s.getsockname()[0]); s.close()
    except OSError:
        pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if ip not in ips and not ip.startswith(("127.", "169.254.")):
                ips.append(ip)
    except OSError:
        pass

    def rank(ip):  # home LAN ranges first; 10.x is often a VPN
        if ip.startswith("192.168."):
            return 0
        if re.match(r"172\.(1[6-9]|2\d|3[01])\.", ip):
            return 1
        return 2 if ip.startswith("10.") else 3
    return sorted(ips, key=rank)


# ------------------------------------------------------------------ OS actions
def allowed_places():
    cfg = C.load()
    places = list(C.scan_roots(cfg))
    sc = B.load(os.path.join(C.DATA, "steam_scan.json"), {})
    places += [os.path.join(l, "steamapps", "common") for l in sc.get("libraries") or []]
    extra = B.load(os.path.join(C.DATA, "places.json"), [])
    places += [p for p in (extra if isinstance(extra, list) else []) if isinstance(p, str) and installed.folder_ok(p)]
    return places


def guard(path):
    """Permission lock: only things inside approved folders / Steam libraries."""
    if not path or not os.path.exists(path):
        raise ValueError("Not found on disk, try Rescan")
    if not any(C.is_within(path, p) for p in allowed_places()):
        picked = {os.path.normcase(os.path.realpath(v["launch"])) for v in C.load()["located"].values()}
        if os.path.normcase(os.path.realpath(path)) not in picked:   # an exe you picked yourself with "Locate game…"
            raise PermissionError("That path is outside the folders you allowed")
    return path


def shell_run_tracked(target, cwd=None):
    """ShellExecuteEx that hands back the process (installers that ask for admin rights work too), or None."""
    import ctypes
    from ctypes import wintypes

    class SEI(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("fMask", ctypes.c_ulong), ("hwnd", wintypes.HWND),
                    ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
                    ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE),
                    ("lpIDList", ctypes.c_void_p), ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
                    ("dwHotKey", wintypes.DWORD), ("hIconOrMonitor", wintypes.HANDLE), ("hProcess", wintypes.HANDLE)]
    sei = SEI(cbSize=ctypes.sizeof(SEI), fMask=0x40, lpVerb="open", lpFile=target, lpDirectory=cwd, nShow=1)  # NOCLOSEPROCESS
    se = ctypes.windll.shell32.ShellExecuteExW
    se.argtypes = [ctypes.POINTER(SEI)]
    se.restype = wintypes.BOOL
    if not se(ctypes.byref(sei)):
        raise OSError(f"Windows could not open it (error {ctypes.GetLastError()})")
    return sei.hProcess or None


def wait_process(handle):
    import ctypes
    k = ctypes.windll.kernel32
    k.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    k.CloseHandle.argtypes = [ctypes.c_void_p]
    k.WaitForSingleObject(handle, 0xFFFFFFFF)
    k.CloseHandle(handle)


def start_install(gid, path):
    """Run an installer and, when it closes, find out where it put the game (needs consent.installed)."""
    cfg = C.load()
    before = installed.programs() if C.allow(cfg, "installed") else None
    h = shell_run_tracked(path, os.path.dirname(path)) if C.IS_WIN else (shell_open(path) or None)
    with LOCK:
        INSTALLS[gid] = {"since": time.time(), "before": before}
    if h:
        def watch():
            wait_process(h)
            time.sleep(2)  # some installers start the game / a last helper right at the end
            finish_install(gid)
        threading.Thread(target=watch, daemon=True).start()


def finish_install(gid):
    """Installer closed (or the user clicked "Done installing"): remember what it installed, then rescan."""
    with LOCK:
        info = INSTALLS.pop(gid, None)
    if info and info.get("before") is not None:
        g = next((x for x in B.load(os.path.join(C.DATA, "games.json"), {}).get("games", []) if x.get("id") == gid), None)
        if g:
            found = installed.added_since(info["before"], {"title": g.get("title"), "install": g.get("installPath")})
            if found:
                cfg = C.load()
                cfg["located"][gid] = {"launch": found["launch"], "folder": found["folder"], "how": "installer"}
                C.save(cfg)
                log(f"Found {g.get('title')} after its install: {found['launch']}")
    rescan(online=False)


def pick_exe():
    if not C.IS_WIN:
        return None
    ps = ("Add-Type -AssemblyName System.Windows.Forms;"
          "$o=New-Object System.Windows.Forms.Form -Property @{TopMost=$true;ShowInTaskbar=$false};"
          "$f=New-Object System.Windows.Forms.OpenFileDialog;$f.Title='Pick the game''s .exe for LAN Hub';"
          "$f.Filter='Games (*.exe)|*.exe';$f.DereferenceLinks=$true;$f.InitialDirectory=$env:LANHUB_DIR;"
          "if($f.ShowDialog($o) -eq 'OK'){[Console]::Out.Write($f.FileName)}")
    try:
        r = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-STA", "-Command", ps],
                           capture_output=True, text=True, timeout=600,
                           env=dict(os.environ, LANHUB_DIR=os.environ.get("ProgramFiles(x86)", "C:\\")),
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.stdout.strip() or None
    except Exception:
        return None


def locate(gid):
    """The user points at the installed game's exe themselves. Their pick is the permission for that folder."""
    acts = B.load(os.path.join(C.DATA, "actions.json"), {})
    if not isinstance(gid, str) or gid not in acts:
        raise ValueError("Unknown game, try Rescan")
    p = pick_exe()
    if not p:
        return None
    p = os.path.normpath(p)
    if not (os.path.isabs(p) and p.lower().endswith(".exe") and os.path.isfile(p)):
        raise ValueError("That's not a program (.exe)")
    if C.is_within(p, C.HUB):
        raise PermissionError("That's part of LAN Hub itself")
    if B.local_scan.installer_kind(p):
        raise ValueError("That looks like an installer. Pick the game itself (install it first)")
    folder = os.path.dirname(p)
    cfg = C.load()
    cfg["located"][gid] = {"launch": p, "folder": folder if installed.folder_ok(folder) else None, "how": "manual"}
    C.save(cfg)
    log(f"Located {gid}: {p}")
    rescan(online=False)
    return p


def forget(gid):
    cfg = C.load()
    if isinstance(gid, str) and cfg["located"].pop(gid, None):
        C.save(cfg)
        rescan(online=False)


def shell_open(target, args=None, cwd=None):
    if C.IS_WIN:
        import ctypes
        r = ctypes.windll.shell32.ShellExecuteW(None, "open", target, args or None, cwd, 1)
        if r <= 32:
            raise OSError(f"Windows could not open it (code {r})")
    elif C.IS_MAC:
        subprocess.Popen(["open", target])
    else:
        subprocess.Popen(["xdg-open", target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def reveal(path):
    if C.IS_WIN:
        if os.path.isfile(path) and "," not in path:  # explorer's /select, parser trips over commas
            subprocess.Popen([EXPLORER, "/select,", path])
        else:
            os.startfile(path if os.path.isdir(path) else os.path.dirname(path))
    else:
        shell_open(path if os.path.isdir(path) else os.path.dirname(path))


def do_action(gid, action, url=None):
    if action == "steam":
        if not isinstance(url, str) or not STEAM_URL.fullmatch(url):
            raise ValueError("Blocked steam url")
        shell_open(url)
        return "Steam: " + url
    acts = B.load(os.path.join(C.DATA, "actions.json"), {})
    a = acts.get(gid) if isinstance(gid, str) else None
    if not a:
        raise ValueError("Unknown game, try Rescan")
    if action == "launch" and a.get("aumid"):
        # Xbox app / Game Pass: only Windows can start these (package identity), via shell:AppsFolder
        aumid = a["aumid"]
        if not isinstance(aumid, str) or not exeinfo.AUMID.fullmatch(aumid):
            raise ValueError("Blocked app id")
        guard(a.get("folder"))
        subprocess.Popen([EXPLORER, "shell:AppsFolder\\" + aumid])
        return "Launched (Xbox app) " + aumid
    if action == "launch":
        exe = guard(a.get("launch"))
        if not exe.lower().endswith(".exe") and C.IS_WIN:
            raise PermissionError("Only .exe files can be launched")
        shell_open(exe, a.get("args") or None, os.path.dirname(exe))
        return "Launched " + exe
    if action == "install":
        p = guard(a.get("install"))
        if C.IS_WIN and p.lower().endswith(".iso"):
            subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-Command",
                            "Mount-DiskImage -ImagePath $env:LANHUB_ISO"], timeout=60, env=dict(os.environ, LANHUB_ISO=p),
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            subprocess.Popen([EXPLORER, "shell:MyComputerFolder"])
        elif p.lower().endswith((".exe", ".msi")):
            start_install(gid, p)  # watched: when the installer closes, LAN Hub looks for the installed game
        else:
            shell_open(p, cwd=os.path.dirname(p))  # .zip/.rar/.url open in their default app
        return "Opened " + p
    if action == "folder":
        p = guard(a.get("folder") or a.get("launch") or a.get("install"))
        reveal(p)
        return "Opened folder " + p
    raise ValueError("Unknown action")


def pick_folder():
    if not C.IS_WIN:
        return None
    ps = ("Add-Type -AssemblyName System.Windows.Forms;"
          "$o=New-Object System.Windows.Forms.Form -Property @{TopMost=$true;ShowInTaskbar=$false};"
          "$f=New-Object System.Windows.Forms.FolderBrowserDialog;$f.Description='Pick a folder with games for LAN Hub';"
          "$f.ShowNewFolderButton=$false;if($f.ShowDialog($o) -eq 'OK'){[Console]::Out.Write($f.SelectedPath)}")
    try:
        r = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-STA", "-Command", ps],
                           capture_output=True, text=True, timeout=600,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.stdout.strip() or None
    except Exception:
        return None


def public_config():
    cfg = C.load()
    from steam_scan import find_steam, library_suggestions
    return {"ok": True, "consented": C.consented(cfg), "consent": cfg.get("consent"),
            "roots": C.scan_roots(cfg), "candidates": C.candidate_roots(),
            "steamPath": find_steam(cfg), "steamLibraries": library_suggestions(cfg),
            "hosts": C.ALLOWED_HOSTS, "updateHosts": C.UPDATE_HOSTS,
            "version": C.VERSION, "repo": C.REPO, "windows": C.IS_WIN,
            "showUnknownGames": cfg.get("showUnknownGames", True), "lanCollectionName": cfg.get("lanCollectionName", "LAN")}


def update_check(force=False):
    """Background: ask GitHub (only if allowed) whether there's a newer release."""
    def run():
        try:
            r = updater.check(C.load(), force=force)
            if r and r.get("available"):
                log(f"LAN Hub {r['latest']} is available (you have {C.VERSION}).")
        except Exception as e:
            log("! update check failed:", e)
    threading.Thread(target=run, daemon=True).start()


def update_install():
    """Background: download + verify + stage the update, then ask LAN Hub.exe to restart into it."""
    def run():
        try:
            updater.stage(C.load(), log=log)
            if os.environ.get("LANHUB_LAUNCHER") == "1":
                log("Update ready: restarting LAN Hub…")
                time.sleep(1.5)  # let the page see "restarting"
                os._exit(RESTART_CODE)
            log("Update ready: close this window and start LAN Hub again to finish.")
        except Exception as e:
            updater.set_status(error=str(e), busy=False)
            log("! update failed:", e)
    threading.Thread(target=run, daemon=True).start()


# ------------------------------------------------------------------ HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "LANHub"
    sys_version = ""

    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json; charset=utf-8", cache=False, nonce=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=604800" if cache else "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")  # other sites can't embed our files
        if ctype.startswith("text/html"):
            self.send_header("Content-Security-Policy",
                             f"default-src 'none'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; font-src 'self'; "
                             f"script-src {repr('nonce-' + nonce) if nonce else repr('none')}; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
                             f"form-action 'none'")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def cross_site(self):
        """Browsers tell us who's asking. Anything from another site (incl. other localhost ports) is refused,
        except a plain top-level visit to the page itself (e.g. a bookmark)."""
        site = (self.headers.get("Sec-Fetch-Site") or "").lower()
        if site in ("", "same-origin", "none"):
            return False
        return not (self.headers.get("Sec-Fetch-Mode") == "navigate" and self.headers.get("Sec-Fetch-Dest") == "document")

    def trusted(self):
        port = self.server.server_address[1]
        okhosts = {f"127.0.0.1:{port}"}  # deliberately NOT "localhost": that can resolve to ::1, where we don't listen
        if self.headers.get("Host") not in okhosts:  # blocks DNS-rebinding
            return False
        o = self.headers.get("Origin")
        return o is None or o in {f"http://{h}" for h in okhosts}

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n < 0 or n > 100_000:
            raise ValueError("request too large")
        b = json.loads(self.rfile.read(n) or b"{}") if n else {}
        if not isinstance(b, dict):
            raise ValueError("bad request")
        return b

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = unquote(urlparse(self.path).path)
        if not self.trusted() or self.cross_site():
            return self.send(403, {"ok": False, "error": "forbidden"})
        if path.startswith("/api/"):
            if path == "/api/ping":
                with LOCK:
                    inst = sorted(INSTALLS)
                cfg = C.load()
                return self.send(200, {"ok": True, "built": STATE["built"], "scanning": STATE["scanning"],
                                       "phase": STATE["phase"], "error": STATE["error"], "ips": IPS,
                                       "consented": C.consented(cfg), "log": STATE["log"][-6:],
                                       "allow": {k: C.allow(cfg, k) for k in C.CONSENT_KEYS},
                                       "version": C.VERSION, "installing": inst, "update": updater.status()})
            if path == "/api/config":
                return self.send(200, public_config())
            return self.send(404, {"ok": False, "error": "not found"})
        # Validate the URL as text FIRST: no backslashes, drive letters, UNC paths or ".." ever reach the filesystem.
        rel = "index.html" if path in ("/", "") else path[1:] if path.startswith("/") else ""
        parts = rel.split("/")
        ext = os.path.splitext(rel)[1].lower()
        if (not SAFE_URL_PATH.fullmatch(rel) or any(x in ("", ".", "..") for x in parts)
                or not (rel == "index.html" or (ext in SERVE_EXT and (rel in SERVE_FILES or rel.startswith(SERVE_DIRS))))):
            return self.send(404, "not found", "text/plain")
        full = os.path.join(C.ROOT, *parts)
        if not C.is_within(full, C.ROOT) or not os.path.isfile(full):
            return self.send(404, "not found", "text/plain")
        with open(full, "rb") as f:
            data = f.read()
        if rel == "index.html":
            nonce = secrets.token_urlsafe(18)
            return self.send(200, data.replace(b"__CSP_NONCE__", nonce.encode()), MIME[ext], nonce=nonce)
        self.send(200, data, MIME.get(ext, "application/octet-stream"), cache=rel.startswith(SERVE_DIRS))

    def do_POST(self):
        path = urlparse(self.path).path
        if not self.trusted() or self.cross_site() or self.headers.get("X-LANHub") != "1":
            return self.send(403, {"ok": False, "error": "forbidden"})
        try:
            b = self.body()
            if path == "/api/do":
                msg = do_action(b.get("id"), b.get("action"), b.get("url"))
                log("> " + msg)
                return self.send(200, {"ok": True, "message": msg})
            if path == "/api/consent":
                cfg = C.load()
                roots = b.get("roots") if isinstance(b.get("roots"), list) else []
                if isinstance(b.get("steamOff"), list):
                    cfg["steamOff"] = [p for p in b["steamOff"] if isinstance(p, str) and len(p) < 300][:64]
                C.grant(cfg, b, roots)
                log("Permission granted: " + " ".join(f"{k}={cfg['consent'][k]}" for k in C.CONSENT_KEYS)
                    + f" folders={len(cfg['roots'])}")
                rescan()
                update_check()
                return self.send(200, public_config())
            if path == "/api/config":
                cfg = C.load()
                if not C.consented(cfg):
                    raise PermissionError("Answer the permission screen first")
                if "roots" in b:
                    cfg["roots"] = C.clean_roots(b["roots"] if isinstance(b["roots"], list) else [])
                for k in C.CONSENT_KEYS:
                    if k in b:
                        cfg["consent"][k] = b[k] is True
                if isinstance(b.get("steamOff"), list):
                    cfg["steamOff"] = [p for p in b["steamOff"] if isinstance(p, str) and len(p) < 300][:64]
                if "showUnknownGames" in b:
                    cfg["showUnknownGames"] = bool(b["showUnknownGames"])
                if isinstance(b.get("lanCollectionName"), str):
                    cfg["lanCollectionName"] = b["lanCollectionName"].strip()[:80] or "LAN"
                C.save(cfg)
                log("Settings saved, rescanning…")
                rescan()
                return self.send(200, public_config())
            if path == "/api/rescan":
                return self.send(200, {"ok": True, "started": rescan()})
            if path == "/api/pickfolder":
                return self.send(200, {"ok": True, "path": pick_folder()})
            if path == "/api/locate":
                if not C.consented(C.load()):
                    raise PermissionError("Answer the permission screen first")
                return self.send(200, {"ok": True, "path": locate(b.get("id"))})
            if path == "/api/forget":
                forget(b.get("id"))
                return self.send(200, {"ok": True})
            if path == "/api/installdone":
                gid = b.get("id")
                with LOCK:
                    tracked = isinstance(gid, str) and gid in INSTALLS
                if tracked:
                    threading.Thread(target=finish_install, args=(gid,), daemon=True).start()
                else:
                    rescan(online=False)
                return self.send(200, {"ok": True})
            if path == "/api/update/check":
                if not C.allow(C.load(), "updates"):
                    raise PermissionError("Update checks are off (Folders & permissions)")
                update_check(force=True)
                return self.send(200, {"ok": True})
            if path == "/api/update/install":
                st = updater.status()
                if not st.get("available") or st.get("busy"):
                    raise ValueError("No update to install")
                updater.set_status(busy=True, error=None)
                update_install()
                return self.send(200, {"ok": True})
            return self.send(404, {"ok": False, "error": "not found"})
        except Exception as e:
            log("! " + str(e))
            return self.send(400, {"ok": False, "error": str(e)})


class Server(ThreadingHTTPServer):
    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def is_lanhub(port):
    try:
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        c.request("GET", "/api/ping", headers={"Host": f"127.0.0.1:{port}"})
        return (c.getresponse().getheader("Server") or "").startswith("LANHub")
    except Exception:
        return False


def main():
    global IPS
    cfg = C.load()
    if "--rescan" in sys.argv:
        if not C.consented(cfg):
            out("Open LAN Hub once and answer the permission screen first."); return 2
        B.build(cfg, online="--offline" not in sys.argv, log=log)
        return 0
    base = int(cfg.get("port", 7777))
    srv = None
    for port in range(base, base + 10):
        try:
            srv = Server(("127.0.0.1", port), Handler)
            break
        except OSError:
            if is_lanhub(port):  # it's us (another LAN Hub window): just show that one
                out(f"ALREADY_RUNNING http://127.0.0.1:{port}/")
                if "--no-browser" not in sys.argv:
                    webbrowser.open(f"http://127.0.0.1:{port}/")
                return 3
            out(f"  Port {port} is used by another program, trying {port + 1}…")
    if srv is None:
        out(f"  No free port between {base} and {base + 9}. Change \"port\" in app\\config.json.")
        return 4
    url = f"http://127.0.0.1:{port}/"
    try:  # lets a second LAN Hub.exe find this instance even if we moved to another port
        with open(os.path.join(C.DATA, "port.txt"), "w") as f:
            f.write(str(port))
    except OSError:
        pass
    IPS = lan_ips()
    out(f"READY {url}")
    out(f"  LAN HUB  →  {url}")
    if IPS:
        out(f"  Your LAN IP: {', '.join(IPS)}  (friends connect to this)")
    if C.consented(cfg):
        rescan()
        update_check()   # at most every few hours, and only if you allowed it
    else:
        log("First run: waiting for you to allow scanning in the browser.")
    if cfg.get("openBrowser", True) and "--no-browser" not in sys.argv:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
