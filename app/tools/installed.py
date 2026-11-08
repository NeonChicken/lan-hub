# -*- coding: utf-8 -*-
"""Find games you installed with an installer, so their tile turns from "Install" into "Play".

Only used when you allowed it (consent.installed). Reads Windows' list of installed programs
(the same list as Settings > Apps: the Uninstall keys in the registry) and nothing else:
  * after you click Install, LAN Hub compares that list from before and after the installer ran
    and remembers what it added (config.json "located", how="installer")
  * on every scan, installer tiles whose game shows up in that list by name get a Play button
  * "Locate game…" on a tile lets you pick the exe yourself (how="manual")
"""
import os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import config as C, local_scan  # noqa: E402

UNINSTALL = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
NOT_GAME_EXE = re.compile(r"unins|uninst|setup|install|redist|crash|report|update|helper|launcherpatcher|dxsetup|vcredist", re.I)


def programs():
    """{key: {"name", "location", "icon"}} from HKLM (64 + 32-bit view) and HKCU. Empty off Windows."""
    out = {}
    if not C.IS_WIN:
        return out
    import winreg
    views = [(winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY, "HKLM64"), (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY, "HKLM32"),
             (winreg.HKEY_CURRENT_USER, 0, "HKCU")]
    for hive, view, tag in views:
        try:
            root = winreg.OpenKey(hive, UNINSTALL, 0, winreg.KEY_READ | view)
        except OSError:
            continue
        with root:
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(root, i)
                except OSError:
                    break
                i += 1
                try:
                    with winreg.OpenKey(root, sub) as k:
                        def val(n):
                            try:
                                v = winreg.QueryValueEx(k, n)[0]
                                return v.strip() if isinstance(v, str) else None
                            except OSError:
                                return None
                        name = val("DisplayName")
                        if not name or val("SystemComponent") == "1" or val("ParentKeyName"):
                            continue
                        if sub.lower().startswith("steam app ") or "steam://" in (val("UninstallString") or "").lower():
                            continue  # Steam games come from the Steam scan
                        out[f"{tag}\\{sub}"] = {"name": name[:200], "location": val("InstallLocation"), "icon": val("DisplayIcon"),
                                                "uninstall": val("UninstallString")}
                except OSError:
                    pass
    return out


def folder_ok(folder):
    """A remembered game folder must be a real, specific folder: never a drive root, Windows, Program Files
    itself or your user folder (those would let "Open folder" / launching reach far too much)."""
    if not folder or not os.path.isabs(folder) or not os.path.isdir(folder):
        return False
    f = os.path.normcase(os.path.normpath(folder))
    if os.path.dirname(f) == f:
        return False
    broad = [os.environ.get(v) for v in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "ProgramData",
                                          "USERPROFILE", "APPDATA", "LOCALAPPDATA")]
    broad += [os.path.join(os.environ.get("USERPROFILE", "~"), d) for d in ("Desktop", "Documents", "Downloads")]
    if any(b and f == os.path.normcase(os.path.normpath(b)) for b in broad):
        return False
    sysroot = os.environ.get("SystemRoot")
    return not (sysroot and C.is_within(f, sysroot)) and not C.is_within(f, C.HUB)


def _icon_exe(icon):
    if not icon:
        return None
    p = icon.split(",")[0].strip().strip('"')
    return p if p.lower().endswith(".exe") and os.path.isfile(p) else None


SUBFOLDERS = {"bin", "bin32", "bin64", "binaries", "win32", "win64", "x64", "x86", "system", "editor", "edit", "tools", "launcher"}


def install_folder(p):
    """Where a program lives: InstallLocation, else the uninstaller's folder (NSIS/Inno put it in the game's root),
    else the icon's folder (one level up when that's a bin/editor subfolder)."""
    if p.get("location") and folder_ok(p["location"]):
        return os.path.normpath(p["location"])
    u = p.get("uninstall") or ""
    m = re.match(r'\s*"([^"]+\.exe)"|\s*(\S+\.exe)\b', u, re.I)
    up = (m.group(1) or m.group(2)) if m else None
    if up and "msiexec" not in up.lower() and os.path.isfile(up):
        d = os.path.dirname(up)
        if os.path.basename(d).lower() in ("uninstall", "uninst", "_uninst", "__installer"):
            d = os.path.dirname(d)
        if folder_ok(d):
            return os.path.normpath(d)
    icon = _icon_exe(p.get("icon"))
    if icon:
        d = os.path.dirname(icon)
        if os.path.basename(d).lower() in SUBFOLDERS:
            d = os.path.dirname(d)
        if folder_ok(d):
            return os.path.normpath(d)
    return None


def game_exe(folder, title, hints=()):
    """The game's exe inside an install folder: the curated exe name, the program's icon, or the best guess."""
    if not folder_ok(folder):
        return None
    hints = {h.lower() for h in hints if h}
    exes = []
    stack = [(folder, 0)]
    while stack and len(exes) < 400:
        d, depth = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    if local_scan.is_link(e):
                        continue
                    if e.is_dir(follow_symlinks=False) and depth < 3 and e.name.lower() not in local_scan.SKIP_DIRS:
                        stack.append((e.path, depth + 1))
                    elif e.is_file() and e.name.lower().endswith(".exe"):
                        exes.append(e.path)
        except OSError:
            continue
    for e in sorted(exes, key=lambda p: p.count(os.sep)):
        if os.path.basename(e).lower() in hints:
            return e
    return local_scan.main_exe(exes, title, skip_installers=True)


NOISE = {"the", "of", "a", "an", "and", "patch", "update", "hotfix", "version", "gog", "com", "inc", "ltd", "llc",
         "x86", "x64", "bit", "setup", "v"}


def _words(s):
    return set(re.findall(r"[a-z]+|\d+", (s or "").lower().replace("&", " and "))) - NOISE


def name_score(names, program):
    """How sure are we that `program` (a DisplayName) is the game known by `names`? 0 = not at all.
    The game's numbers must be there ("UT99" is not "Unreal Tournament 2004", "Counter-Strike 1.6" is not
    "Counter-Strike: Source"); numbers only the program has are versions ("Re-Volt patch 12.07")."""
    num = lambda ws: {w for w in ws if w.isdigit()}
    p_all = num(_words(program))
    p_real = num(_words(re.sub(r"\d+(?:\.\d+)+|\bv\d+\b", " ", program, flags=re.I)))   # minus versions like 12.07
    plain = [re.sub(r"\(.*?\)", " ", n) for n in names]
    need = set().union(*[num(_words(n)) for n in plain]) if plain else set()          # "1.6" in Counter-Strike 1.6
    known = set().union(*[num(_words(n)) for n in names]) if names else set()         # incl. "(UT99)"
    if not p_real <= known or not need <= p_all:
        return 0
    best = 0
    for n in names:
        nw, pw = _words(n), _words(program) - (p_all - p_real)   # numbers count as words, versions don't
        if not nw or not pw:
            continue
        common = nw & pw
        small, large = min(len(nw), len(pw)), max(len(nw), len(pw))
        if not common or (small == 1 and max(len(w) for w in common) < 4):
            continue
        if len(common) / small >= 0.6 and len(common) / large >= 0.4:
            best = max(best, len(common) / large + (1 if nw == pw else 0))
    return best


def names_for(entry):
    """Title, aka's and the installer's own name ("rvgl_21.0930a-1_setup_win64.exe" -> "rvgl")."""
    t = entry.get("title") or ""
    out = [re.sub(r"\(.*?\)", " ", t), t] + list(entry.get("aka") or [])   # "Grim Dawn (GOG, 4-player)" -> "Grim Dawn"
    inst = entry.get("install")
    if inst:
        stem = os.path.splitext(re.split(r"[\\/]", inst)[-1])[0]
        stem = re.sub(r"[_\-.()]+", " ", stem)
        stem = re.sub(r"(?i)\b(setup|installer|install|win32|win64|win|x64|x86|full|gog|v?\d\w*)\b", " ", stem)
        if stem.strip():
            out.append(stem)
    return [n for n in out if n.strip()]


def hint_exes(entry):
    l = entry.get("launch")
    return [re.split(r"[\\/]", l)[-1]] if l else []


def match(entry, progs):
    """Best installed program for a curated / unknown entry -> {"launch", "folder"} or None."""
    names = names_for(entry)
    scored = sorted(((name_score(names, p["name"]), k) for k, p in progs.items()), reverse=True)
    for score, k in scored[:3]:
        if score <= 0:
            break
        m = _resolve(progs[k], entry)
        if m:
            return m
    return None


def _resolve(p, entry):
    folder = install_folder(p)
    if not folder:
        return None
    icon = _icon_exe(p.get("icon"))
    hints = hint_exes(entry)
    if icon and os.path.basename(os.path.dirname(icon)).lower() not in SUBFOLDERS and not NOT_GAME_EXE.search(os.path.basename(icon)):
        hints.append(os.path.basename(icon))   # the icon is usually the game exe (not when it's the track editor)
    exe = game_exe(folder, entry.get("title") or p["name"], hints)
    if exe and NOT_GAME_EXE.search(os.path.basename(exe)):
        exe = None
    return {"launch": exe, "folder": folder, "program": p["name"]} if exe else None


def added_since(before, entry):
    """After an installer closed: what it added to the installed-programs list, matched to the game."""
    now = programs()
    new = {k: v for k, v in now.items() if k not in before or before[k].get("location") != v.get("location")}
    if not new:
        return None
    m = match(entry, new)
    if m:
        return m
    if len(new) == 1:  # one new program and nothing else changed: that's what this installer installed
        return _resolve(next(iter(new.values())), entry)
    return None
