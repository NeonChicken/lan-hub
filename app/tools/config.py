# -*- coding: utf-8 -*-
"""LAN Hub configuration + the permission model.

Nothing is scanned until the user approved it on the consent screen:
  consent.steam      read Steam's own files (library list, artwork cache)
  roots              the ONLY folders searched for non-Steam games (incl. Xbox / Game Pass folders)
  consent.internet   download store info / artwork, ONLY from ALLOWED_HOSTS
  consent.installed  read Windows' list of installed programs, to find games you installed with an installer
  consent.updates    check GitHub for a newer LAN Hub, ONLY at UPDATE_HOSTS
"""
import json, os, platform, re, string, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)            # ...\LAN Hub\app  (web root, the only place LAN Hub writes to)
HUB = os.path.dirname(ROOT)             # ...\LAN Hub      (contains LAN Hub.exe)
DATA = os.path.join(ROOT, "data")
THUMBS = os.path.join(ROOT, "thumbs")
CONFIG_PATH = os.path.join(ROOT, "config.json")
IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"
CONSENT_VERSION = 2   # v2 added "installed" + "updates": everyone answers the permission screen once more
CONSENT_KEYS = ("steam", "internet", "installed", "updates")
VERSION = "2.1.1"     # must match the GitHub release tag (v2.1.1)
REPO = "NeonChicken/lan-hub"

# The only internet hosts LAN Hub will ever contact (enforced in build.Net, redirects included).
ALLOWED_HOSTS = {
    "store.steampowered.com": "game descriptions (Steam store API)",
    "cdn.cloudflare.steamstatic.com": "game artwork (Steam CDN)",
    "shared.akamai.steamstatic.com": "game artwork (Steam CDN)",
    "cdn.akamai.steamstatic.com": "game artwork (Steam CDN)",
    "en.wikipedia.org": "descriptions of classic non-Steam games",
    "upload.wikimedia.org": "box art of classic non-Steam games",
}
# The only hosts the updater talks to (enforced in updater.py, redirects included).
UPDATE_HOSTS = {
    "api.github.com": "is there a newer release?",
    "github.com": "download the release zip",
    "objects.githubusercontent.com": "download the release zip (GitHub's file server)",
    "release-assets.githubusercontent.com": "download the release zip (GitHub's file server)",
}

DEFAULTS = {
    "port": 7777,
    "openBrowser": True,
    "consent": None,          # {"version", "steam", "internet", "at"} once the user clicked Allow
    "roots": [],              # approved game folders (absolute paths)
    "steamPath": "auto",
    "steamUser": "auto",
    "lanCollectionName": "LAN",
    "excludeDirs": ["_*", "Nexus Mod Manager", "Vortex", "BlueStacks*", "Mojang", "PS2", "rpcs3*", "Wabbajack*",
                    "sfx manager", "TechnicLauncher", "uPlay", "Riot Games", "LAN Hub", "$RECYCLE.BIN", "steamapps"],
    "showUnknownGames": True,
    "scanDepth": 5,
    "located": {},            # game id -> {"launch", "folder", "how"}: games found after an install or picked by hand
    "steamOff": [],           # Steam library folders you unticked (their installed games aren't listed or started)
}
# Usual game folders on any drive (suggested on the consent screen, never read before you approve them)
AUTO_ROOT_NAMES = ["LAN Games", "LAN", "Games", "GOG Games", "GOG Galaxy/Games", "Program Files (x86)/GOG Galaxy/Games",
                   "Epic Games", "Program Files/Epic Games", "XboxGames", "Program Files/EA Games",
                   "Program Files (x86)/Origin Games", "Program Files/Origin Games", "EA Games",
                   "Program Files (x86)/Ubisoft/Ubisoft Game Launcher/games", "Ubisoft Games", "Amazon Games/Library"]


def load():
    cfg = json.loads(json.dumps(DEFAULTS))
    try:
        with open(CONFIG_PATH, encoding="utf-8-sig") as f:  # -sig: Notepad/PowerShell may add a BOM
            user = json.load(f)
        if isinstance(user, dict):
            cfg.update({k: v for k, v in user.items() if k in DEFAULTS})
    except FileNotFoundError:
        save(cfg)
    except Exception as e:  # broken json: keep a copy so the user's edits aren't lost, then use defaults
        print("! config.json is invalid, using defaults:", e)
        try:
            import shutil
            shutil.copyfile(CONFIG_PATH, CONFIG_PATH + ".broken")
        except OSError:
            pass
    # wrong types fall back to the default for that key (a typo must not crash scanning)
    if isinstance(cfg.get("roots"), str):  # a single folder written by hand
        cfg["roots"] = [cfg["roots"]]
    for k, d in DEFAULTS.items():
        v = cfg.get(k)
        if d is None:
            if v is not None and not isinstance(v, dict):
                cfg[k] = None
        elif isinstance(d, bool):
            if not isinstance(v, bool):
                cfg[k] = d
        elif isinstance(d, int):
            if isinstance(v, bool) or not isinstance(v, int):
                cfg[k] = d
        elif not isinstance(v, type(d)):
            cfg[k] = json.loads(json.dumps(d))
    if not 1024 <= cfg["port"] <= 65535:
        cfg["port"] = DEFAULTS["port"]
    cfg["scanDepth"] = max(1, min(8, cfg["scanDepth"]))
    if isinstance(cfg.get("consent"), dict):
        cfg["consent"] = {**cfg["consent"], **{k: cfg["consent"].get(k) is True for k in CONSENT_KEYS}}
    cfg["located"] = {k: v for k, v in cfg["located"].items()
                      if isinstance(k, str) and isinstance(v, dict) and isinstance(v.get("launch"), str)}
    cfg["roots"] = [r for r in cfg["roots"] if isinstance(r, str)]
    cfg["steamOff"] = [r for r in cfg["steamOff"] if isinstance(r, str) and len(r) < 300][:64]
    cfg["excludeDirs"] = [x for x in cfg["excludeDirs"] if isinstance(x, str)]
    return cfg


def save(cfg):
    out = {k: cfg.get(k, v) for k, v in DEFAULTS.items()}
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    os.replace(tmp, CONFIG_PATH)


def consented(cfg):
    c = cfg.get("consent")
    return isinstance(c, dict) and c.get("version") == CONSENT_VERSION


def allow(cfg, key):
    return consented(cfg) and cfg["consent"].get(key) is True  # "no", 1, "false"... never count as yes


def grant(cfg, answers, roots):
    cfg["consent"] = {"version": CONSENT_VERSION, **{k: answers.get(k) is True for k in CONSENT_KEYS},
                      "at": time.strftime("%Y-%m-%d %H:%M")}
    cfg["roots"] = clean_roots(roots)
    save(cfg)


def drives():
    if IS_WIN:
        return [f"{d}:\\" for d in string.ascii_uppercase if os.path.exists(f"{d}:\\")]
    return [os.path.expanduser("~")]


def norm(p):
    return os.path.normcase(os.path.normpath(os.path.abspath(os.path.expanduser(str(p)))))


def is_within(path, root):
    """True if path is root or inside it (resolves .. and symlinks)."""
    try:
        p, r = os.path.realpath(path), os.path.realpath(root)
    except (OSError, ValueError):
        return False
    p, r = os.path.normcase(p), os.path.normcase(r)
    return p == r or p.startswith(r.rstrip("\\/") + os.sep)


def clean_roots(roots):
    """Absolute, existing, de-duplicated folders; never a whole drive, never LAN Hub itself."""
    out, seen = [], set()
    for r in roots or []:
        if not isinstance(r, str) or not r.strip():
            continue
        r = os.path.expanduser(r.strip().strip('"'))
        if not os.path.isabs(r) or (IS_WIN and not re.match(r"^([A-Za-z]:[\\/]|\\\\)", r)):
            continue  # absolute paths only (C:\Games or a \\server\share), never relative to the server's folder
        p = os.path.normpath(r)
        k = os.path.normcase(p)
        if k in seen or not os.path.isdir(p) or os.path.dirname(p) == p or is_within(p, HUB):
            continue
        seen.add(k)
        out.append(p)
    return out


def xbox_roots(drive):
    """Folders the Xbox app installs Game Pass / Microsoft Store games into on this drive.
    Windows records them in <drive>\\.GamingRoot: b"RGBX", a uint32 count, then UTF-16 paths relative to the drive."""
    out = []
    try:
        with open(os.path.join(drive, ".GamingRoot"), "rb") as f:
            b = f.read(4096)
        if b[:4] == b"RGBX":
            for rel in b[8:].decode("utf-16-le", "ignore").split("\x00"):
                rel = rel.strip().lstrip("\\/")
                if rel and ".." not in rel and ":" not in rel:
                    out.append(os.path.join(drive, rel))
    except OSError:
        pass
    return out


def candidate_roots():
    """Folders we'd suggest on the consent screen (nothing is read from them until approved)."""
    found = []
    for d in drives():
        for n in AUTO_ROOT_NAMES:
            p = os.path.join(d, *n.split("/"))
            if os.path.isdir(p):
                found.append(p)
        found += [p for p in xbox_roots(d) if os.path.isdir(p)]
    parent = os.path.dirname(HUB)  # the folder LAN Hub was dropped into, unless that's somewhere personal
    if os.path.dirname(parent) != parent and not is_within(parent, os.path.expanduser("~")):
        found.append(parent)
    return clean_roots(found)


def scan_roots(cfg):
    """The approved folders that still exist, minus ones nested inside another approved folder."""
    if not consented(cfg):
        return []
    out = clean_roots(cfg.get("roots"))
    return [r for r in out if not any(r != o and is_within(r, o) for o in out)]


def host():
    return platform.node()
