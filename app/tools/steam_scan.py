# -*- coding: utf-8 -*-
"""Scan local Steam data (Windows / Linux / macOS):
 - Steam install + every library folder (libraryfolders.vdf) -> installed games (appmanifests)
 - appcache/librarycache -> games in your library (also not installed) + cached artwork
 - appcache/appinfo.vdf (binary v28/v29) -> names, categories (LAN / split-screen / ...), tags
 - your collections (for the "LAN" collection row)
"""
import json, os, re, struct, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import config as C  # noqa: E402

CAT = {1: "Multi-player", 2: "Single-player", 9: "Co-op", 20: "MMO", 24: "Shared/Split Screen",
       27: "Cross-Platform Multiplayer", 36: "Online PvP", 37: "Shared/Split Screen PvP",
       38: "Online Co-op", 39: "Shared/Split Screen Co-op", 44: "Remote Play Together",
       47: "LAN PvP", 48: "LAN Co-op", 49: "PvP", 28: "Full controller support", 18: "Partial Controller Support",
       31: "VR Support", 8: "Valve Anti-Cheat", 22: "Steam Achievements", 29: "Steam Trading Cards",
       30: "Steam Workshop", 35: "In-App Purchases", 23: "Steam Cloud"}
STEAM64 = 76561197960265728


# ------------------------------------------------------------------ locate Steam
def find_steam(cfg):
    if cfg.get("steamPath") not in (None, "", "auto"):
        return cfg["steamPath"]
    cands = []
    if C.IS_WIN:
        try:
            import winreg
            for hive, key, val in ((winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
                                   (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
                                   (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath")):
                try:
                    with winreg.OpenKey(hive, key) as k:
                        cands.append(os.path.normpath(winreg.QueryValueEx(k, val)[0]))
                except OSError:
                    pass
        except ImportError:
            pass
        cands += [os.path.join(d, *n.split("/")) for d in C.drives()
                  for n in ("Program Files (x86)/Steam", "Program Files/Steam", "Steam", "Games/Steam")]
    elif C.IS_MAC:
        cands.append(os.path.expanduser("~/Library/Application Support/Steam"))
    else:
        cands += [os.path.expanduser(p) for p in ("~/.steam/steam", "~/.local/share/Steam",
                                                   "~/.var/app/com.valvesoftware.Steam/.local/share/Steam")]
    for c in cands:  # a real Steam install has steamapps AND config (a bare library folder only has steamapps)
        if os.path.isdir(os.path.join(c, "steamapps")) and os.path.isdir(os.path.join(c, "config")):
            return os.path.realpath(c)
    return None


def find_user(steam, cfg):
    if str(cfg.get("steamUser", "auto")) not in ("", "auto"):
        return str(cfg["steamUser"])
    try:  # loginusers.vdf: the account flagged MostRecent
        txt = open(os.path.join(steam, "config", "loginusers.vdf"), encoding="utf-8", errors="replace").read()
        for sid, body in re.findall(r'"(\d{17})"\s*\{([^}]*)\}', txt):
            if re.search(r'"MostRecent"\s*"1"', body):
                return str(int(sid) - STEAM64)
    except OSError:
        pass
    ud = os.path.join(steam, "userdata")
    best = None
    for d in (os.listdir(ud) if os.path.isdir(ud) else []):
        p = os.path.join(ud, d, "config", "localconfig.vdf")
        if d.isdigit() and os.path.exists(p) and (best is None or os.path.getmtime(p) > best[0]):
            best = (os.path.getmtime(p), d)
    return best and best[1]


DEFAULT_LIBRARY_NAMES = ["SteamLibrary", "Steam", "Program Files (x86)/Steam", "Program Files/Steam",
                         "Games/Steam", "Games/SteamLibrary", "Steam Games", "SteamGames"]


def library_suggestions(cfg):
    """Steam libraries to show on the permission screen. Before consent: only folders that EXIST at the usual
    places (nothing is read). After a scan: every library Steam knows about (from that scan)."""
    found = []
    steam = find_steam(cfg)
    if steam:
        found.append(steam)
    if C.IS_WIN:
        for d in C.drives():
            for n in DEFAULT_LIBRARY_NAMES:
                p = os.path.join(d, *n.split("/"))
                if os.path.isdir(os.path.join(p, "steamapps")):
                    found.append(p)
    if C.consented(cfg):
        try:
            with open(os.path.join(C.DATA, "steam_scan.json"), encoding="utf-8") as f:
                found += json.load(f).get("allLibraries") or []
        except Exception:
            pass
    off = {os.path.normcase(os.path.normpath(p)) for p in cfg.get("steamOff") or []}
    out, seen = [], set()
    for p in found:
        k = os.path.normcase(os.path.normpath(p))
        if k not in seen:
            seen.add(k)
            out.append({"path": os.path.normpath(p), "off": k in off})
    return out


def default_libraries():
    """Usual Steam library locations on every drive that really contain steamapps\\appmanifest_*.acf files."""
    out = []
    if not C.IS_WIN:
        return out
    for d in C.drives():
        for n in DEFAULT_LIBRARY_NAMES:
            p = os.path.join(d, *n.split("/"))
            sa = os.path.join(p, "steamapps")
            try:
                if os.path.isdir(sa) and any(f.startswith("appmanifest_") for f in os.listdir(sa)):
                    out.append(p)
            except OSError:
                pass
    return out


# ------------------------------------------------------------------ appinfo.vdf
def _cstr(buf, pos):
    end = buf.index(b"\x00", pos)
    return buf[pos:end].decode("utf-8", "replace"), end + 1


def _kv(buf, pos, strings):
    obj = {}
    while True:
        t = buf[pos]; pos += 1
        if t == 0x08:
            return obj, pos
        if strings is not None:
            (ki,) = struct.unpack_from("<i", buf, pos); pos += 4
            key = strings[ki]
        else:
            key, pos = _cstr(buf, pos)
        if t == 0x00:
            val, pos = _kv(buf, pos, strings)
        elif t == 0x01:
            val, pos = _cstr(buf, pos)
        elif t == 0x02:
            (val,) = struct.unpack_from("<i", buf, pos); pos += 4
        elif t == 0x03:
            (val,) = struct.unpack_from("<f", buf, pos); pos += 4
        elif t == 0x07:
            (val,) = struct.unpack_from("<Q", buf, pos); pos += 8
        elif t == 0x0A:
            (val,) = struct.unpack_from("<q", buf, pos); pos += 8
        else:
            raise ValueError(f"unknown kv type {t}")
        obj[key] = val


def parse_appinfo(path, wanted=None):
    """Parse appinfo.vdf. If `wanted` is a set of appids, only those entries are decoded (much faster)."""
    buf = open(path, "rb").read()
    magic, _ = struct.unpack_from("<II", buf, 0)
    pos, strings = 8, None
    if magic == 0x07564429:
        (st_off,) = struct.unpack_from("<q", buf, pos); pos += 8
        (count,) = struct.unpack_from("<I", buf, st_off)
        strings, p = [], st_off + 4
        for _ in range(count):
            s, p = _cstr(buf, p); strings.append(s)
    elif magic != 0x07564428:
        raise ValueError(f"unsupported appinfo.vdf version {magic:#x}")
    apps = {}
    while True:
        (appid,) = struct.unpack_from("<I", buf, pos); pos += 4
        if appid == 0:
            break
        (size,) = struct.unpack_from("<I", buf, pos); pos += 4
        end = pos + size
        if wanted is None or appid in wanted:
            try:
                kv, _ = _kv(buf, pos + 60, strings)
                apps[appid] = kv.get("appinfo", kv)
            except Exception:
                pass
        pos = end
    return apps


def read_acf(path):
    txt = open(path, encoding="utf-8", errors="replace").read()
    get = lambda k: (re.search(r'"%s"\s+"([^"]*)"' % k, txt) or [None, None])[1]
    return {"appid": int(get("appid")), "name": get("name"), "installdir": get("installdir"),
            "size": int(get("SizeOnDisk") or 0)}


def collections(steam, user):
    out = {}
    p = os.path.join(steam, "userdata", str(user), "config", "cloudstorage", "cloud-storage-namespace-1.json")
    try:
        for k, v in json.load(open(p, encoding="utf-8")):
            if k.startswith("user-collections.") and v.get("value"):
                c = json.loads(v["value"])
                out[c.get("name", "")] = c.get("added", [])
    except Exception:
        pass
    return out


# ------------------------------------------------------------------ main scan
def scan(cfg=None, log=print):
    cfg = cfg or C.load()
    steam = find_steam(cfg) if C.allow(cfg, "steam") else None
    if not C.allow(cfg, "steam"):
        log("Steam: skipped (not allowed in settings)")
        return {"steam": None, "user": None, "libraries": [], "collections": {}, "apps": {}}
    if not steam:
        log("Steam not found (set steamPath in config.json if it's installed somewhere unusual).")
        return {"steam": None, "user": None, "libraries": [], "collections": {}, "apps": {}}
    user = find_user(steam, cfg)
    vdf = os.path.join(steam, "steamapps", "libraryfolders.vdf")
    libs = [steam]
    try:
        libs += [l.replace("\\\\", "\\") for l in re.findall(r'"path"\s+"([^"]+)"', open(vdf, encoding="utf-8").read())]
    except OSError:
        pass
    libs += default_libraries()   # libraries Steam forgot about (reinstalled Steam, drive moved, second PC's disk)
    uniq = {}
    for l in libs:
        uniq.setdefault(os.path.normcase(os.path.normpath(l)), os.path.normpath(l))
    all_libs = [l for l in uniq.values() if os.path.isdir(os.path.join(l, "steamapps"))]
    off = {os.path.normcase(os.path.normpath(p)) for p in cfg.get("steamOff") or []}
    libs = [l for l in all_libs if os.path.normcase(l) not in off]   # unticked libraries: not listed, not startable
    installed = {}
    for lib in libs:
        sa = os.path.join(lib, "steamapps")
        try:
            names = os.listdir(sa)
        except OSError:
            continue
        for f in names:
            if f.startswith("appmanifest_") and f.endswith(".acf"):
                try:
                    a = read_acf(os.path.join(sa, f))
                    a["path"] = os.path.join(sa, "common", a["installdir"] or "")
                    installed[a["appid"]] = a
                except Exception:
                    pass
    cache = os.path.join(steam, "appcache", "librarycache")
    library = {int(d) for d in os.listdir(cache) if d.isdigit()} if os.path.isdir(cache) else set()
    try:
        info = parse_appinfo(os.path.join(steam, "appcache", "appinfo.vdf"), library | set(installed))
    except Exception as e:
        log("! could not read appinfo.vdf:", e); info = {}

    apps = {}
    for appid in sorted(library | set(installed)):
        a = info.get(appid) or {}
        c = a.get("common", {}) if isinstance(a, dict) else {}
        typ = str(c.get("type", "")).lower()
        if typ not in ("game", "mod", "") or (typ == "" and appid not in installed):
            continue
        cats = sorted(int(k.split("_")[1]) for k in (c.get("category") or {}) if k.startswith("category_"))
        imgs = {}
        d = os.path.join(cache, str(appid))
        if os.path.isdir(d):
            for root, _, files in os.walk(d):
                for fn in files:
                    base = os.path.splitext(fn)[0]
                    if base in ("header", "library_600x900", "library_hero", "logo"):
                        imgs.setdefault(base, os.path.join(root, fn))
        ext = a.get("extended", {}) if isinstance(a, dict) else {}
        apps[appid] = {
            "appid": appid, "name": c.get("name") or installed.get(appid, {}).get("name"), "type": typ,
            "categories": cats, "catnames": [CAT.get(x, str(x)) for x in cats],
            "tags": [int(v) for v in (c.get("store_tags") or {}).values()],
            "developer": ext.get("developer"), "review": c.get("review_percentage"),
            "metacritic": c.get("metacritic_score"), "installed": appid in installed,
            "install": installed.get(appid), "images": imgs,
        }
    res = {"steam": steam, "user": user, "libraries": libs, "allLibraries": all_libs, "collections": collections(steam, user) if user else {},
           "apps": apps}
    os.makedirs(C.DATA, exist_ok=True)
    json.dump(res, open(os.path.join(C.DATA, "steam_scan.json"), "w", encoding="utf-8"), indent=1)
    log(f"Steam: {len(apps)} library games, {sum(a['installed'] for a in apps.values())} installed, "
        f"{len(libs)} libraries, user {user}")
    return res


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    scan()
