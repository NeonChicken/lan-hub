# -*- coding: utf-8 -*-
"""Small, read-only file inspections used by the scanner.

  installer_kind(path)  -> "Inno Setup" / "NSIS" / ... if an .exe is really an installer, else None.
                           Reads only the PE headers, the resource section and the first bytes after the
                           program image (where installers keep their payload), never the whole file.
  xbox_game(folder)     -> Xbox app / Game Pass game info from <folder>\\Content\\appxmanifest.xml, with the
                           AppUserModelID Windows needs to start it (shell:AppsFolder\\<aumid>).
"""
import hashlib, os, re, struct

RSRC_MAX = 3 * 1024 * 1024
OVERLAY_PEEK = 64 * 1024
# markers inside the resource section (manifest / version info) or right after the program image
RSRC_MARKERS = [(b"JR.Inno.Setup", "Inno Setup"), (b"Inno Setup", "Inno Setup"), (b"Nullsoft.NSIS", "NSIS"),
                (b"Nullsoft Install System", "NSIS"), (b"InstallShield", "InstallShield"),
                (b"Setup Factory", "Setup Factory"), (b"WiX Toolset", "WiX"), (b"7-Zip SFX", "7-Zip SFX"),
                (b"Advanced Installer", "Advanced Installer"), (b"Clickteam Install", "Clickteam")]
OVERLAY_MARKERS = [(b"Inno Setup Setup Data", "Inno Setup"), (b"zlb\x1a", "Inno Setup"), (b"rDlPtS", "Inno Setup"),
                   (b"NullsoftInst", "NSIS"), (b"InstallShield", "InstallShield")]
SETUP_WORDS = re.compile(r"\b(setup|installer|install|installation|self-extract\w*|sfx)\b", re.I)


def _pe(f, size):
    """-> (sections [(name, raw_ptr, raw_size)], end_of_image, cert_size) or None."""
    f.seek(0)
    mz = f.read(64)
    if len(mz) < 64 or mz[:2] != b"MZ":
        return None
    pe = struct.unpack_from("<I", mz, 0x3C)[0]
    if pe <= 0 or pe > 1 << 20:
        return None
    f.seek(pe)
    h = f.read(24 + 240)
    if h[:4] != b"PE\0\0":
        return None
    nsec, optsize = struct.unpack_from("<H", h, 6)[0], struct.unpack_from("<H", h, 20)[0]
    magic = struct.unpack_from("<H", h, 24)[0]
    cert = 0
    dd = 24 + (96 if magic == 0x10B else 112 if magic == 0x20B else 0)
    if dd > 24 and len(h) >= dd + 5 * 8:
        cert = struct.unpack_from("<I", h, dd + 4 * 8 + 4)[0]   # data directory 4 = Authenticode signature
    f.seek(pe + 24 + optsize)
    secs, end = [], 0
    for _ in range(min(nsec, 96)):
        s = f.read(40)
        if len(s) < 40:
            break
        name = s[:8].rstrip(b"\0").decode("latin-1")
        rsize, rptr = struct.unpack_from("<II", s, 16)
        secs.append((name, rptr, rsize))
        if rptr + rsize <= size:
            end = max(end, rptr + rsize)
    return secs, end, cert


def _file_description(rsrc):
    m = re.search(rb"F\0i\0l\0e\0D\0e\0s\0c\0r\0i\0p\0t\0i\0o\0n\0\0\0(?:\0\0)?((?:[^\0]\0){1,120})", rsrc)
    return m.group(1).decode("utf-16-le", "replace") if m else ""


def installer_kind(path):
    """None for normal programs. Never raises."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            pe = _pe(f, size)
            if not pe:
                return None
            secs, end, cert = pe
            names = {s[0].lower() for s in secs}
            if ".wixburn" in names:
                return "WiX"
            rsrc = b""
            for name, ptr, rs in secs:
                if name.lower() == ".rsrc" and rs:
                    f.seek(ptr)
                    rsrc = f.read(min(rs, RSRC_MAX))
            overlay = max(0, size - end - cert)
            head = b""
            if overlay > 4096:
                f.seek(end)
                head = f.read(OVERLAY_PEEK)
        for m, kind in OVERLAY_MARKERS:
            if m in head:
                return kind
        for m, kind in RSRC_MARKERS:
            if m in rsrc:
                return kind
        desc = _file_description(rsrc)
        # "Re-Volt Setup", "XYZ Installer", "7-Zip self-extracting archive": only with a real payload attached
        if desc and SETUP_WORDS.search(desc) and overlay > 512 * 1024:
            return "installer"
        if head[:6] == b"7z\xbc\xaf\x27\x1c" and "sfx" in desc.lower():
            return "7-Zip SFX"
    except (OSError, struct.error, ValueError):
        pass
    return None


# ------------------------------------------------------------------ Xbox app / Game Pass
_B32 = "0123456789abcdefghjkmnpqrstvwxyz"
AUMID = re.compile(r"[A-Za-z0-9.\-]{3,64}_[0-9a-z]{13}![A-Za-z0-9.\-_]{1,64}")   # used with fullmatch


def publisher_id(publisher):
    """The 13-character hash Windows puts in package family names (…_8wekyb3d8bbwe)."""
    n = int.from_bytes(hashlib.sha256(publisher.encode("utf-16-le")).digest()[:8], "big") << 1
    return "".join(_B32[(n >> (5 * (12 - i))) & 31] for i in range(13))


def _attr(tag, name, xml):
    m = re.search(r"<%s\b[^>]*?\b%s\s*=\s*\"([^\"]*)\"" % (tag, name), xml, re.S)
    return m.group(1).strip() if m else None


def registered(name, pid):
    """Is this package installed for the current user? (Windows' own package list in the registry.)"""
    try:
        import winreg
        k = r"Software\Classes\Local Settings\Software\Microsoft\Windows\CurrentVersion\AppModel\Repository\Packages"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, k) as r:
            i = 0
            while True:
                try:
                    n = winreg.EnumKey(r, i)
                except OSError:
                    return False
                i += 1
                if n.lower().startswith(name.lower() + "_") and n.lower().endswith("_" + pid):
                    return True
    except Exception:
        return False


def xbox_game(folder):
    """-> {"title", "aumid", "exe"} for an installed Xbox app game folder, else None."""
    content = os.path.join(folder, "Content")
    try:
        with open(os.path.join(content, "appxmanifest.xml"), encoding="utf-8", errors="replace") as f:
            xml = f.read(256 * 1024)
    except OSError:
        return None
    name, pub = _attr("Identity", "Name", xml), _attr("Identity", "Publisher", xml)
    app = _attr("Application", "Id", xml)
    if not (name and pub and app):
        return None
    pid = publisher_id(pub)
    aumid = f"{name}_{pid}!{app}"
    if not AUMID.fullmatch(aumid) or not registered(name, pid):
        return None   # copied folder / not installed for this user: Windows couldn't start it anyway
    title = None
    m = re.search(r"<DisplayName>([^<]+)</DisplayName>", xml)
    if m and not m.group(1).startswith("ms-resource:"):
        title = m.group(1).strip()
    exe = None
    try:  # the real game exe (for "Open folder" and artwork lookups), from MicrosoftGame.config
        cfgname = next((n for n in os.listdir(content) if n.lower() == "microsoftgame.config"), None)
        if cfgname:
            with open(os.path.join(content, cfgname), encoding="utf-8", errors="replace") as f:
                gc = f.read(256 * 1024)
            e = _attr("Executable", "Name", gc)
            if e and not re.search(r"[\\/:]|\.\.", e) and os.path.isfile(os.path.join(content, e)):
                exe = os.path.join(content, e)
            if not title:
                t = _attr("ShellVisuals", "DefaultDisplayName", gc)
                title = t if t and not t.startswith("ms-resource:") else None
    except OSError:
        pass
    return {"title": title or os.path.basename(folder), "aumid": aumid, "exe": exe}
