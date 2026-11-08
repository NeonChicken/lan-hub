# -*- coding: utf-8 -*-
"""Build the hub data for THIS PC.

  steam_scan  -> Steam games (installed + library)       \
  local_scan  -> non-Steam games found in scan folders    }-> data/games.js + games.json + actions.json
  curation    -> player counts / LAN flags / tips         /

Network (optional, cached in data/*_cache.json): Steam store descriptions + artwork,
Wikipedia box art for non-Steam classics. Pass online=False for an instant offline build.
"""
import hashlib, json, os, re, shutil, sys, time, urllib.error, urllib.parse, urllib.request
from urllib.parse import urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import config as C, curation, installed, local_scan, steam_scan  # noqa: E402

ROOT, DATA, THUMBS = C.ROOT, C.DATA, C.THUMBS
UA = {"User-Agent": f"LANHub/{C.VERSION} (+https://github.com/{C.REPO})"}
MAX_DOWNLOAD = 15 * 1024 * 1024  # bytes; artwork/JSON are far smaller


class Blocked(Exception):
    pass


def check_url(url):
    """Permission lock: HTTPS to an allow-listed host only."""
    try:
        u = urlsplit(url)
        port = u.port
    except ValueError:
        raise Blocked("blocked malformed URL")
    if u.scheme != "https" or (u.hostname or "").lower() not in C.ALLOWED_HOSTS or u.username or port not in (None, 443):
        raise Blocked(f"blocked request to {u.hostname!r} (not on the allow-list)")


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl)  # a redirect can't escape the allow-list either
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_SafeRedirect)
CDN = "https://cdn.cloudflare.steamstatic.com/steam/apps/{}/{}"
try:
    from PIL import Image  # optional: shrinks artwork to webp
except Exception:
    Image = None


def load(p, default):
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return default


def save(p, obj, indent=None):
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=indent, separators=None if indent else (",", ":"))
    os.replace(tmp, p)


class Net:
    def __init__(self, online, log):
        self.online, self.log = online, log
        self.blocked = 0
        self.store = load(os.path.join(DATA, "store_cache.json"), {})
        self.wiki = load(os.path.join(DATA, "wiki_cache.json"), {})
        self.dirty = 0

    def flush(self):
        save(os.path.join(DATA, "store_cache.json"), self.store)
        save(os.path.join(DATA, "wiki_cache.json"), self.wiki)

    def get(self, url, binary=False):
        if not self.online:
            return None
        try:
            check_url(url)
        except Blocked as e:
            self.blocked += 1
            if self.blocked <= 3:
                self.log("  " + str(e))
            return None
        for _ in range(3):
            try:
                with _OPENER.open(urllib.request.Request(url, headers=UA), timeout=20) as r:
                    b = r.read(MAX_DOWNLOAD + 1)
                    if len(b) > MAX_DOWNLOAD:
                        return None
                    return b if binary else b.decode("utf-8", "replace")
            except Blocked as e:
                self.log("  " + str(e)); return None
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    self.log("  Steam rate limit, pausing 70s…"); time.sleep(70); continue
                return None
            except Exception:
                time.sleep(2)
        return None

    def _mark(self):
        self.dirty += 1
        if self.dirty % 10 == 0:
            self.flush()

    def details(self, appid):
        k = str(appid)
        if k in self.store or not self.online:
            return self.store.get(k)
        txt = self.get(f"https://store.steampowered.com/api/appdetails?appids={appid}&l=english")
        time.sleep(1.6)
        d = None
        try:
            j = json.loads(txt)[k]
            if j.get("success"):
                x = j["data"]
                d = {"name": x.get("name"), "short": x.get("short_description"), "header": x.get("header_image"),
                     "genres": [g["description"] for g in x.get("genres", [])],
                     "release": (x.get("release_date") or {}).get("date"), "dev": x.get("developers"),
                     "free": x.get("is_free"), "meta": (x.get("metacritic") or {}).get("score"),
                     "cats": [c["description"] for c in x.get("categories", [])]}
        except Exception:
            if txt is None:
                return None  # network failure: don't cache, retry next build
        self.store[k] = d
        self._mark()
        return d

    def search(self, term):
        k = "search:" + term.lower()
        if k in self.store or not self.online:
            return self.store.get(k) or []
        txt = self.get("https://store.steampowered.com/api/storesearch/?cc=us&l=english&term=" + urllib.parse.quote(term))
        time.sleep(1.0)
        try:
            self.store[k] = [{"id": i["id"], "name": i["name"]} for i in json.loads(txt).get("items", [])[:5]]
        except Exception:
            return []
        self._mark()
        return self.store[k]

    def wikipedia(self, title):
        if title in self.wiki or not self.online:
            return self.wiki.get(title)
        txt = self.get("https://en.wikipedia.org/api/rest_v1/page/summary/" + urllib.parse.quote(title, safe="%:_()'!,"))
        try:
            j = json.loads(txt)
            d = {"extract": j.get("extract"), "img": (j.get("originalimage") or j.get("thumbnail") or {}).get("source")}
        except Exception:
            return None
        self.wiki[title] = d
        self._mark()
        return d


# ------------------------------------------------------------------ images
MAXW = {"hero": 1600, "logo": 640, "cover": 616, "art": 900}


def optimize(path, kind):
    """Convert to a right-sized webp if Pillow is available (cuts thumbs/ to ~1/4)."""
    if not Image or path.endswith(".webp"):
        return path
    try:
        with Image.open(path) as im:
            im.load()
            w = MAXW.get(kind, 1200)
            if im.width > w:
                im = im.resize((w, round(im.height * w / im.width)), Image.LANCZOS)
            if im.mode not in ("RGB", "RGBA"):
                im = im.convert("RGBA" if "transparency" in im.info or im.mode in ("LA", "P") else "RGB")
            out = os.path.splitext(path)[0] + ".webp"
            im.save(out, "WEBP", quality=80 if kind != "logo" else 88, method=4)
        if os.path.normcase(out) != os.path.normcase(path):
            os.remove(path)
        return out
    except Exception:
        return path


def image_ext(head):
    if head[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    return None


def put_image(net, dest_dir, name, local=None, urls=()):
    if not C.is_within(dest_dir, THUMBS):  # permission lock: only ever write inside app/thumbs
        raise Blocked("refusing to write outside thumbs/: " + dest_dir)
    for ext in (".webp", ".jpg", ".png"):
        p = os.path.join(dest_dir, name + ext)
        if os.path.exists(p) and os.path.getsize(p) > 0:
            return optimize(p, name)
    os.makedirs(dest_dir, exist_ok=True)
    if local and os.path.isfile(local) and os.path.getsize(local) < MAX_DOWNLOAD:
        with open(local, "rb") as f:
            head = f.read(12)
        ext = image_ext(head)
        if ext:  # anything that isn't really a JPEG/PNG/WebP is ignored
            p = os.path.join(dest_dir, name + ext)
            shutil.copyfile(local, p)
            return optimize(p, name)
    if not net.online:
        return None
    for u in urls:
        b = u and net.get(u, binary=True)
        ext = image_ext(b[:12]) if b and len(b) > 2000 else None
        if ext:
            p = os.path.join(dest_dir, name + ext)
            open(p, "wb").write(b)
            return optimize(p, name)
    return None


def rel(p):
    return os.path.relpath(p, ROOT).replace("\\", "/") if p else None


def steam_images(net, gid, appid, scanimgs, header_url=None):
    d, s = os.path.join(THUMBS, gid), scanimgs or {}
    return {"cover": rel(put_image(net, d, "cover", s.get("header"), [header_url, CDN.format(appid, "header.jpg")])),
            "hero": rel(put_image(net, d, "hero", s.get("library_hero"), [CDN.format(appid, "library_hero.jpg")])),
            "logo": rel(put_image(net, d, "logo", s.get("logo"), [CDN.format(appid, "logo.png")]))}


# ------------------------------------------------------------------ helpers
def clean_html(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    for a, b in (("&quot;", '"'), ("&amp;", "&"), ("&#39;", "'"), ("&lt;", "<"), ("&gt;", ">")):
        s = s.replace(a, b)
    return re.sub(r"\s+", " ", s).strip() or None


def nums(p):
    return [int(n) for n in re.findall(r"\d+", p or "")]


def norm(s):
    return set(re.findall(r"[a-z0-9]+", (s or "").lower())) - {"the", "of", "a", "edition", "and", "hd", "gog", "classic"}


def same_game(a, b):
    A, B = norm(a), norm(b)
    return bool(A and B) and len(A & B) / max(1, min(len(A), len(B))) >= 0.5


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40] or "game"


# tag -> genre for games that aren't hand-curated (Steam store tag ids)
TAG_GENRE = [({7178, 7108}, "party"), ({745697}, "social"), ({1743}, "fighting"), ({699}, "racing"), ({701}, "sports"),
             ({1667}, "horror"), ({1663, 1774, 176981}, "shooter"), ({9, 1676, 1770, 1666}, "strategy"),
             ({1662, 1100689}, "survival"), ({3810}, "sandbox"), ({122}, "rpg")]
MP_CATS = {1, 9, 24, 36, 37, 38, 39, 47, 48, 49}


def auto_meta(g):
    c = set(g["categories"])
    lan = bool({47, 48} & c)
    couch = 2 if {24, 37, 39} & c else 0
    tags = set(g.get("tags") or [])
    genre = next((gname for ids, gname in TAG_GENRE if ids & tags), "coop" if {9, 38, 48, 39} & c else "party")
    note = ("Steam lists LAN support." if lan else "Online multiplayer.") + (" Has split-screen / shared-screen play." if couch else "")
    return ("?", couch, int(lan), genre, note)


# ------------------------------------------------------------------ build
def build(cfg=None, online=True, log=print):
    cfg = cfg or C.load()
    t0 = time.time()
    os.makedirs(DATA, exist_ok=True)
    online = online and C.allow(cfg, "internet")
    net = Net(online, log)
    sc = steam_scan.scan(cfg, log)
    apps = {int(k): v for k, v in sc["apps"].items()}
    lanc = set(sc["collections"].get(cfg.get("lanCollectionName") or "LAN", []))
    games, actions = [], {}

    # ---------------- Steam
    todo = [a for a, v in curation.STEAM.items() if v and a in apps]
    for a, g in apps.items():
        if a in curation.STEAM or g["type"] not in ("game", "mod"):
            continue
        c = set(g["categories"])
        if {47, 48} & c or (g["installed"] and MP_CATS & c) or a in lanc:
            todo.append(a)
    log(f"Building {len(todo)} Steam games{' (fetching new store info/art…)' if online else ''}")
    for i, appid in enumerate(todo):
        g = apps[appid]
        curated = appid in curation.STEAM
        players, couch, lan, genre, note = curation.STEAM.get(appid) or auto_meta(g)
        sd = net.details(appid) or {}
        gid = f"s{appid}"
        inst = g.get("install") or {}
        n = nums(players)
        games.append({
            "id": gid, "appid": appid, "title": sd.get("name") or g["name"] or f"App {appid}", "source": "steam",
            "installed": g["installed"], "sizeGB": round(inst["size"] / 1e9, 1) if inst.get("size") else None,
            "path": inst.get("path"), "players": players, "pmin": min(n) if n else 1, "pmax": max(n) if n else 99,
            "guess": not curated, "couch": couch, "lan": bool(lan), "genre": genre, "note": note,
            "desc": clean_html(sd.get("short")), "tags": sd.get("genres") or [],
            "features": [x for x in (sd.get("cats") or g["catnames"]) if not x.isdigit()],
            "dev": ", ".join(sd.get("dev") or []) or g.get("developer"), "release": sd.get("release"),
            "meta": sd.get("meta") or g.get("metacritic"), "review": g.get("review"), "free": sd.get("free"),
            "lanCollection": appid in lanc, "img": steam_images(net, gid, appid, g.get("images"), sd.get("header")),
        })
        if inst.get("path"):
            actions[gid] = {"folder": inst["path"]}
        if online and i and i % 50 == 0:
            log(f"  {i}/{len(todo)}")

    # ---------------- local (curated, found anywhere in scan roots)
    ls = local_scan.scan(curation.LOCAL, cfg, log, curator_roots=curation.CURATOR_ROOTS)
    roots = ls["roots"]
    progs = installed.programs() if C.allow(cfg, "installed") else {}
    places = []   # extra folders games may be started from: found after an install, or picked by you

    def located(gid, entry):
        """Where an installer put this game: remembered (after Install / Locate) or found in Windows' program list."""
        loc = (cfg.get("located") or {}).get(gid)
        if loc and os.path.isfile(loc.get("launch") or "") and loc["launch"].lower().endswith(".exe"):
            folder = next((f for f in (loc.get("folder"), os.path.dirname(loc["launch"])) if installed.folder_ok(f)), None)
            return {"launch": loc["launch"], "folder": folder, "how": loc.get("how") or "manual"}
        if progs and entry.get("install"):
            m = installed.match(entry, progs)
            if m:
                return dict(m, how="windows")
        return None
    vault_roots = [r for r in roots if "lan" in os.path.basename(r).lower()] or [os.path.dirname(C.HUB)]

    def in_vault(p):
        return bool(p) and any(C.is_within(p, r) for r in vault_roots)

    def local_art(gid, appid, title, wiki):
        sd = {}
        if appid:
            sd = net.details(appid) or {}
            if sd and not same_game(sd.get("name"), title):
                hit = next((h for h in net.search(re.sub(r"\(.*?\)", "", title).strip()) if same_game(h["name"], title)), None)
                appid, sd = (hit["id"], net.details(hit["id"]) or {}) if hit else (None, {})
        elif not wiki:
            hit = next((h for h in net.search(title) if same_game(h["name"], title)), None)
            if hit:
                appid, sd = hit["id"], net.details(hit["id"]) or {}
        appid = int(appid) if isinstance(appid, int) or (isinstance(appid, str) and appid.isdigit()) else None
        w = net.wikipedia(wiki) if wiki else None
        imgs = steam_images(net, gid, appid, (apps.get(appid) or {}).get("images"), sd.get("header")) if appid \
            else {"cover": None, "hero": None, "logo": None}
        if not imgs["cover"]:
            art = put_image(net, os.path.join(THUMBS, gid), "art", None, [(w or {}).get("img")])
            imgs["cover"] = rel(art)
        return appid, sd, w, imgs

    for e in curation.LOCAL:
        m = ls["matched"].get(e["id"])
        gid = e["id"]
        loc = None
        if m and not m.get("launch"):
            loc = located(gid, dict(e, install=m.get("install") or e.get("install")))
        elif not m and (cfg.get("located") or {}).get(gid):
            loc = located(gid, {})   # its installer was moved/deleted, but the game is still installed
        if not m and not loc:
            continue
        m = dict(m or {})
        if loc:
            m["launch"], m["folder"] = loc["launch"], loc.get("folder") or m.get("folder")
            places.append(m["folder"])
        if e.get("steam") is not None and not isinstance(e["steam"], int):
            continue
        launch, install, folder = m.get("launch"), m.get("install"), m.get("folder")
        appid, sd, w, imgs = local_art(gid, e.get("steam"), e["title"], e.get("wiki"))
        n = nums(e["players"])
        size = round(os.path.getsize(install) / 1e9, 1) if install and os.path.isfile(install) else None
        games.append({
            "id": gid, "appid": appid, "title": e["title"], "source": "local",
            "status": "installed" if launch else "installer" if install else "store" if e.get("url") else "folder",
            "installed": bool(launch), "players": e["players"], "pmin": min(n) if n else 1, "pmax": max(n) if n else 1,
            "couch": e.get("couch", 0), "lan": bool(e.get("lan")), "genre": e["genre"], "note": e.get("note"),
            "desc": e.get("desc") or clean_html(sd.get("short")) or (w or {}).get("extract"),
            "tags": sd.get("genres") or [], "features": sd.get("cats") or [],
            "dev": ", ".join(sd.get("dev") or []) or None, "release": sd.get("release") or e.get("year"),
            "meta": sd.get("meta"), "path": launch or install or folder, "launchPath": launch, "installPath": install,
            "folderPath": folder, "url": e.get("url"), "sizeGB": size, "vault": in_vault(install or folder or launch),
            "img": imgs, "located": loc and loc["how"], "platform": "xbox" if m.get("aumid") else None,
        })
        actions[gid] = {k: v for k, v in (("launch", launch), ("args", e.get("args", "")), ("install", install),
                                           ("folder", folder), ("aumid", m.get("aumid"))) if v}

    # ---------------- unknown folders that contain a game exe
    for u in ls["unknown"]:
        # stable across restarts and independent of scan order: name + hash of the folder path
        gid = "u-" + slug(u["title"])[:32] + "-" + hashlib.md5(u["folder"].lower().encode("utf-8")).hexdigest()[:6]
        launch, install, folder, loc = u.get("launch"), u.get("install"), u["folder"], None
        if not launch and not u.get("aumid"):
            loc = located(gid, {"title": u["title"], "install": install})
            if loc:
                launch = loc["launch"]
                places.append(loc["folder"])
        appid, sd, w, imgs = local_art(gid, None, u["title"], None)
        xbox = u.get("platform") == "xbox"
        ready = bool(launch or u.get("aumid"))
        games.append({
            "id": gid, "appid": appid, "title": (u["title"] if xbox else sd.get("name") or u["title"]), "source": "local",
            "status": "installed" if ready else "installer", "installed": ready, "players": "?", "pmin": 1, "pmax": 99,
            "guess": True, "unknown": True, "couch": 0, "lan": False, "genre": "other",
            "note": ("Installed with the Xbox app / Game Pass. " if xbox else "") +
                    "Found automatically, no player info yet. Add it to tools/curation_local.py to curate.",
            "desc": clean_html(sd.get("short")), "tags": sd.get("genres") or [], "features": sd.get("cats") or [],
            "dev": ", ".join(sd.get("dev") or []) or None, "release": sd.get("release"), "path": launch or install or folder,
            "launchPath": launch or (folder if xbox else None), "installPath": install, "folderPath": folder,
            "vault": in_vault(folder), "img": imgs, "located": loc and loc["how"], "platform": "xbox" if xbox else None,
        })
        actions[gid] = {k: v for k, v in (("launch", launch), ("install", install), ("folder", folder),
                                           ("aumid", u.get("aumid"))) if v}

    save(os.path.join(DATA, "places.json"), sorted({p for p in places if p}))
    net.flush()
    meta = {"built": time.strftime("%Y-%m-%d %H:%M:%S"), "host": C.host(), "roots": roots,
            "steam": sc["steam"], "lanCollection": cfg.get("lanCollectionName"),
            "complete": online, "games": games}
    save(os.path.join(DATA, "actions.json"), actions)
    save(os.path.join(DATA, "games.json"), meta)
    with open(os.path.join(DATA, "games.js.tmp"), "w", encoding="utf-8") as f:
        f.write("window.LAN_HUB = " + json.dumps(meta, ensure_ascii=False, separators=(",", ":")) + ";\n")
    os.replace(os.path.join(DATA, "games.js.tmp"), os.path.join(DATA, "games.js"))
    log(f"Done: {len(games)} games ({sum(g['installed'] for g in games)} installed) in {time.time() - t0:.0f}s")
    return meta


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    build(online="--offline" not in sys.argv)
