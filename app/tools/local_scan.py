# -*- coding: utf-8 -*-
"""Find non-Steam games anywhere under the scan roots.

How matching works
  1. Walk every scan root (config.json) a few levels deep and index every
     .exe / archive / installer by lower-case file name.
  2. Each curated game in curation.LOCAL has paths as they exist on the curator's PC.
     Those are turned into *recipes*: the exe name + its parent folders, and the
     installer/archive name with version numbers wild-carded
     (setup_swat4_gold_2.0.0.4.exe -> setup_swat4_gold_*.exe).
  3. A recipe matches a found file when the file name is identical and either
     the parent folders line up, or the file name is distinctive enough on its own.
     Generic names (hl.exe, nw.exe, launcher.exe…) also need a folder whose name
     shares a word with the game title.
  4. A matched "game exe" that is really an installer (Inno Setup, NSIS, ...: see exeinfo.py) counts as
     the installer, so the tile says Install instead of starting setup again and again.
  5. Xbox app / Game Pass folders (Content\\appxmanifest.xml of a package installed for this user) start
     through Windows (shell:AppsFolder), the only way those games can be launched.
  6. Top-level folders nobody claimed, that contain a real exe, become
     "unknown" games (shown in their own row) when showUnknownGames is on.
"""
import fnmatch, json, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import config as C, exeinfo  # noqa: E402

INDEX_EXT = {".exe", ".zip", ".rar", ".7z", ".iso", ".msi", ".url", ".bat"}
SKIP_DIRS = {"__pycache__", "node_modules", ".git", "monobleedingedge", "_commonredist", "redist", "redistributables",
             "directx", "vcredist", "dotnet", "support", "__installer", "engine", "steamapps", "cache", "logs",
             "shadercache", "screenshots", "saves", "savegames", "mods", "workshop", "$recycle.bin",
             "system volume information", "windowsapps"}
GENERIC = {"hl.exe", "nw.exe", "launcher.exe", "launcher_x64.exe", "game.exe", "start.exe", "play.exe", "sb.exe",
           "love.exe", "main.exe", "app.exe", "client.exe", "setup.exe", "autorun.exe", "run.exe"}
JUNK_EXE = re.compile(r"unins|setup|install|redist|vc_?redist|dxsetup|crash|report|update|patch|helper|uninst|"
                      r"dotnet|vcrun|ue4prereq|cleanup|config|editor|server|dedicated|benchmark|7z|unitycrashhandler|"
                      r"createdump|texconv|elevate|eos|bugsplat|cefprocess|webhelper|notification|xdelta|wabbajack|cecho", re.I)
STOP = {"the", "of", "a", "and", "edition", "hd", "gog", "classic", "version", "complete", "definitive", "remastered",
        "game", "games", "goty", "mp", "player", "v", "x64", "win64", "win", "windows", "build", "final", "full"}

# where the curator's paths live (curation_local.CURATOR_ROOTS, passed in by build.py); stripped to get "tails"
CURATOR_ROOTS = []
EXE_CACHE = os.path.join(C.DATA, "exe_cache.json")
_kinds, _kinds_dirty = None, False


def installer_kind(path):
    """exeinfo.installer_kind, cached by path + size + date so a rescan doesn't re-read files."""
    global _kinds, _kinds_dirty
    if not path or not path.lower().endswith(".exe"):
        return None
    if _kinds is None:
        try:
            with open(EXE_CACHE, encoding="utf-8") as f:
                _kinds = json.load(f)
            if not isinstance(_kinds, dict):
                _kinds = {}
        except Exception:
            _kinds = {}
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = f"{os.path.normcase(path)}|{st.st_size}|{int(st.st_mtime)}"
    if key not in _kinds:
        _kinds[key] = exeinfo.installer_kind(path)
        _kinds_dirty = True
    return _kinds[key]


def save_kinds():
    global _kinds_dirty
    if _kinds_dirty and _kinds is not None:
        try:
            os.makedirs(C.DATA, exist_ok=True)
            with open(EXE_CACHE + ".tmp", "w", encoding="utf-8") as f:
                json.dump(_kinds, f)
            os.replace(EXE_CACHE + ".tmp", EXE_CACHE)
            _kinds_dirty = False
        except OSError:
            pass


def is_link(entry):
    """Symlinks and Windows junctions/reparse points: never followed (they could point outside an approved folder)."""
    try:
        if entry.is_symlink():
            return True
        return bool(getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0) & 0x400)
    except OSError:
        return True


def words(s):
    return set(re.findall(r"[a-z]+|\d+", (s or "").lower().replace("&", " and "))) - STOP


def norm_parts(p):
    return [x for x in re.split(r"[\\/]+", p) if x]


def tail_of(p):
    for r in CURATOR_ROOTS:
        if p.lower().startswith(r.lower()):
            return norm_parts(p[len(r):])
    return norm_parts(p)[-3:]


def version_glob(name):
    s = re.sub(r"\d+(?:[._]\d+)+", "*", name)          # 2.0.0.4 / 2_0_5
    s = re.sub(r"\(\d+\)", "*", s)                       # (18947)
    s = re.sub(r"v\d+\*?", "v*", s, flags=re.I)          # v1.1
    return re.sub(r"\*+", "*", s).lower()


# ------------------------------------------------------------------ walking
class Index:
    def __init__(self):
        self.files = {}   # lower name -> [(root, fullpath)]
        self.top = {}     # (root, topdir) -> list of exe paths inside
        self.roots = []

    def add_root(self, root, cfg):
        self.roots.append(root)
        excl = [e.lower() for e in cfg.get("excludeDirs", [])]
        maxd = int(cfg.get("scanDepth", 5))

        def excluded(name):
            n = name.lower()
            return n in SKIP_DIRS or any(fnmatch.fnmatch(n, e) for e in excl) or n.endswith("_data")

        me = os.path.normcase(C.HUB)
        stack = [(root, 0, None)]
        while stack:
            d, depth, top = stack.pop()
            try:
                it = os.scandir(d)
            except OSError:
                continue
            with it:
                for e in it:
                    try:
                        if is_link(e):
                            continue
                        if e.is_dir(follow_symlinks=False):
                            if depth < maxd and not excluded(e.name) and os.path.normcase(e.path) != me:
                                stack.append((e.path, depth + 1, top or e.name))
                                if depth == 0:
                                    self.top.setdefault((root, e.name), [])
                        elif e.is_file():
                            ext = os.path.splitext(e.name)[1].lower()
                            if ext in INDEX_EXT:
                                self.files.setdefault(e.name.lower(), []).append((root, e.path))
                                if ext == ".exe" and top:
                                    self.top.setdefault((root, top), []).append(e.path)
                            elif ext == "" and C.IS_WIN is False and depth <= 3 and os.access(e.path, os.X_OK) and top:
                                self.top.setdefault((root, top), []).append(e.path)
                    except OSError:
                        pass

    def glob(self, pattern):
        pat = pattern.lower()
        if not any(ch in pat for ch in "*?["):
            return self.files.get(pat, [])
        return [x for n, lst in self.files.items() if fnmatch.fnmatch(n, pat) for x in lst]


def rel_parts(root, path):
    return norm_parts(os.path.relpath(path, root))


def best_exe(index, tail, title):
    """tail = ['SWAT 4','Content','System','Swat4.exe'] (curator relative path)."""
    name = tail[-1].lower()
    tw = words(title) | words(tail[0] if len(tail) > 1 else "")
    best, best_score = None, -1
    for root, p in index.files.get(name, []):
        parts = rel_parts(root, p)
        score = 0
        for a, b in zip(reversed(tail[:-1]), reversed(parts[:-1])):
            if a.lower() != b.lower():
                break
            score += 2
        if any(words(x) & tw for x in parts[:-1]):
            score += 1
        if name in GENERIC or len(os.path.splitext(name)[0]) <= 3:
            if score < 1:
                continue
        if score > best_score:
            best, best_score = p, score
    return best


def match_curated(index, entry):
    """Returns dict with launch/install/folder (found on this PC) or None."""
    found = {}
    # exact paths first (curator's own PC)
    for k in ("launch", "install", "folder"):
        p = entry.get(k)
        if not p or not os.path.isabs(p):
            continue  # relative = a recipe from the public catalog, matched by name below
        # permission lock: even a known exact path only counts if it's inside an approved folder
        if not p or not any(os.path.normcase(os.path.abspath(p)).startswith(os.path.normcase(r).rstrip("\\/") + os.sep)
                            for r in index.roots) or not any(C.is_within(p, r) for r in index.roots):
            continue
        if os.path.exists(p) and not os.path.normcase(p).endswith(os.path.normcase(os.sep)):
            if k == "folder" and (p.rstrip("\\/").lower() + "\\" in [r.lower() for r in CURATOR_ROOTS]
                                  or any(os.path.normcase(p.rstrip("\\/")) == os.path.normcase(r.rstrip("\\/")) for r in index.roots)):
                continue  # "the whole LAN folder" is not this game's folder
            found[k] = p
    if entry.get("launch") and "launch" not in found:
        p = best_exe(index, tail_of(entry["launch"]), entry["title"])
        if p:
            found["launch"] = p
    if entry.get("install") and "install" not in found:
        name = norm_parts(entry["install"])[-1]
        hits = index.glob(name) or index.glob(version_glob(name))
        if name.lower() in GENERIC or re.match(r"(setup|install|autorun)\.", name.lower()):
            tw = words(entry["title"]) | words(tail_of(entry["install"])[0])
            hits = [h for h in hits if any(words(x) & tw for x in rel_parts(h[0], h[1])[:-1])]
        if hits:
            found["install"] = sorted(hits, key=lambda h: len(h[1]))[0][1]
    if entry.get("folder") and "folder" not in found:
        t = tail_of(entry["folder"])
        want = t[0].lower() if t else None
        for (root, top) in index.top:
            if want and top.lower() == want:
                found["folder"] = os.path.join(root, top); break
    if "folder" not in found:
        base = found.get("launch") or found.get("install")
        if base:
            # top-level folder of the hit inside its root
            for r in index.roots:
                if os.path.normcase(base).startswith(os.path.normcase(r) + os.sep):
                    parts = rel_parts(r, base)
                    found["folder"] = os.path.join(r, parts[0]) if len(parts) > 1 else r
                    break
    found = {k: v for k, v in found.items() if any(C.is_within(v, r) for r in index.roots)}
    if found.get("launch") and installer_kind(found["launch"]):
        # e.g. "Re-Volt.exe" that is really an NSIS installer: it's the installer, not the game
        inst = found.pop("launch")
        found.setdefault("install", inst)
    if not (found.get("launch") or found.get("install")) and not entry.get("url"):
        # a bare folder only counts if it really looks like this game
        f = found.get("folder")
        if not f or not (words(os.path.basename(f)) & words(entry["title"])):
            return None
    return found or None


def clean_title(name):
    s = re.sub(r"[._]+", " ", name)
    s = re.sub(r"\bv?\d+(\.\d+){1,}[a-z]?\b", "", s, flags=re.I)
    s = re.sub(r"\b(win64|x64|build \d+|gog|multi\d+|plus)\b", "", s, flags=re.I)
    s = re.sub(r"\s+", " ", s).strip(" -")
    return s or name


def main_exe(exes, folder_name, skip_installers=False):
    good = [e for e in exes if not JUNK_EXE.search(os.path.basename(e))]
    if skip_installers:
        good = [e for e in good if not installer_kind(e)]
    if not good:
        return None
    fw = words(folder_name)
    fc = re.sub(r"[^a-z0-9]", "", (folder_name or "").lower())

    def score(e):
        try:
            size = os.path.getsize(e)
        except OSError:
            size = 0
        stem = os.path.splitext(os.path.basename(e))[0]
        sc = re.sub(r"[^a-z0-9]", "", stem.lower())
        same = 2 if sc and len(sc) >= 3 and (sc in fc or fc.startswith(sc)) else 0   # "revolt.exe" for "Re-Volt"
        return (len(words(stem) & fw) + same, -e.count(os.sep), size)
    return max(good, key=score)


def xbox_folders(index):
    """Top-level folders that are Xbox app / Game Pass installs: {normcase(folder): exeinfo.xbox_game()}."""
    out = {}
    for (root, top) in index.top:
        folder = os.path.join(root, top)
        if os.path.isfile(os.path.join(folder, "Content", "appxmanifest.xml")):
            x = exeinfo.xbox_game(folder)
            if x:
                out[os.path.normcase(folder)] = dict(x, folder=folder)
    return out


def scan(curated, cfg=None, log=print, curator_roots=None):
    global CURATOR_ROOTS
    CURATOR_ROOTS = list(curator_roots or [])
    cfg = cfg or C.load()
    roots = C.scan_roots(cfg)
    index = Index()
    for r in roots:
        index.add_root(r, cfg)
    log(f"Local: scanned {len(roots)} folders: " + "; ".join(roots))
    xbox = xbox_folders(index)
    matched, claimed = {}, set()

    def top_of(p):
        for r in roots:
            if os.path.normcase(p).startswith(os.path.normcase(r) + os.sep):
                return os.path.normcase(r), rel_parts(r, p)[0].lower(), os.path.join(r, rel_parts(r, p)[0])
        return None

    for e in curated:
        m = match_curated(index, e)
        if m:
            t = top_of(m.get("launch") or m.get("folder") or "")
            x = xbox.get(os.path.normcase(t[2])) if t else None
            if x and m.get("launch"):
                m["aumid"], m["folder"] = x["aumid"], x["folder"]   # Game Pass: Windows has to start it
            matched[e["id"]] = m
            for p in (v for k, v in m.items() if k != "aumid"):
                t = top_of(p)
                if t:
                    claimed.add(t[:2])
    unknown = []
    seen = set()
    for key, x in sorted(xbox.items()):   # Game Pass games are always shown: they're real installs, not guesses
        t = top_of(x["folder"])
        if t and t[:2] not in claimed:
            claimed.add(t[:2])
            seen.add(x["title"].lower())
            unknown.append({"folder": x["folder"], "launch": x["exe"], "aumid": x["aumid"], "title": x["title"],
                            "root": t[0], "platform": "xbox"})
    if cfg.get("showUnknownGames", True):
        for (root, top), exes in sorted(index.top.items()):
            if (os.path.normcase(root), top.lower()) in claimed or re.fullmatch(r"[\d. _-]+", top):
                continue
            t = clean_title(top)
            if t.lower() in seen:
                continue
            exe = main_exe(exes, top, skip_installers=True)
            if exe:
                seen.add(t.lower())
                unknown.append({"folder": os.path.join(root, top), "launch": exe, "title": t, "root": root})
                continue
            # only an installer in there (e.g. a "Re-Volt" folder with Re-Volt.exe = NSIS setup): offer Install
            setup = [e for e in exes if installer_kind(e) and not re.search(r"unins|redist|vc_?redist|dxsetup|dotnet|directx|patch|update|hotfix|dlc", os.path.basename(e), re.I)]
            setup = sorted(setup, key=lambda e: (e.count(os.sep), len(e)))
            if setup:
                seen.add(t.lower())
                unknown.append({"folder": os.path.join(root, top), "install": setup[0], "title": t, "root": root})
    save_kinds()
    log(f"Local: {len(matched)} curated games found, {len(unknown)} other game folders"
        + (f" ({len(xbox)} from the Xbox app)" if xbox else ""))
    return {"roots": roots, "matched": matched, "unknown": unknown}
