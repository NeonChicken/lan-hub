# -*- coding: utf-8 -*-
"""Make a LAN Hub zip, WITHOUT your scan data, paths or permissions.

  python app/tools/package.py             -> builds/LAN-Hub-<version>.zip          public release (GitHub)
  python app/tools/package.py --friends   -> builds/LAN-Hub-<version>-friends.zip  + your curation_local.py,
                                             artwork cache and descriptions of curated games (for your LAN party)

Zip layout:
  LAN Hub/LAN Hub.exe      launcher (tray icon)
  LAN Hub/README.txt, LICENSE
  LAN Hub/app/...          page, tools, launcher source, bundled Python (+ artwork cache in --friends)

Public builds never contain: curation_local.py, config.json, data/, thumbs/. The updater relies on that name
pattern (LAN-Hub-<version>.zip) and on the .sha256 file written next to it.
"""
import hashlib, json, os, sys, zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import config as C, curation  # noqa: E402

APP, HUB = C.ROOT, C.HUB
BUILDS = os.path.join(HUB, "builds")
TOP = "LAN Hub"
HUB_FILES = ["LAN Hub.exe", "README.txt", "LICENSE"]
APP_FILES = ["index.html", "Start LAN Hub (fallback).bat"]
APP_DIRS = ["assets", "tools", "launcher", "runtime"]
PRIVATE = {"curation_local.py", "make_steam_collection.py"}   # only in --friends builds
SKIP = ("__pycache__", ".pyc", ".tmp", ".bak", ".lanhub-old")


def main():
    friends = "--friends" in sys.argv
    if friends and not curation.HAS_LOCAL:
        print("Note: no app/tools/curation_local.py, so the friends zip only has the public catalog.")
    for need in ("LAN Hub.exe", "LICENSE"):
        if not os.path.exists(os.path.join(HUB, need)):
            sys.exit(f"{need} is missing" + (": run app\\launcher\\build.bat first" if need.endswith(".exe") else ""))
    if not os.path.exists(os.path.join(APP, "runtime", "python", "python.exe")):
        sys.exit("app\\runtime\\python is missing (portable Python). See README.")
    # Only info about CURATED games is shared (public Steam/Wikipedia text about games listed in curation).
    # Never shared: Steam-search lookups made from your own folder names, or anything about uncurated games you own.
    appids = {str(a) for a, v in curation.STEAM.items() if v} | {str(e["steam"]) for e in curation.LOCAL if e.get("steam")}
    thumbs = {"s" + a for a in appids} | {e["id"] for e in curation.LOCAL}
    wikis = {e["wiki"] for e in curation.LOCAL if e.get("wiki")}

    os.makedirs(BUILDS, exist_ok=True)
    out = os.path.join(BUILDS, f"LAN-Hub-{C.VERSION}{'-friends' if friends else ''}.zip")
    n = 0
    with zipfile.ZipFile(out + ".tmp", "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        def add(src, arc):
            nonlocal n
            z.write(src, f"{TOP}/{arc}")
            n += 1

        for f in HUB_FILES:
            add(os.path.join(HUB, f), f)
        for f in APP_FILES:
            add(os.path.join(APP, f), "app/" + f)
        for d in APP_DIRS + (["thumbs"] if friends else []):
            for root, dirs, files in os.walk(os.path.join(APP, d)):
                top = os.path.normcase(root) == os.path.normcase(os.path.join(APP, d))
                dirs[:] = [x for x in dirs if x != "__pycache__" and not (d == "thumbs" and top and x not in thumbs)]
                for f in files:
                    if f.endswith(SKIP) or (d == "tools" and f in PRIVATE and not friends):
                        continue
                    p = os.path.join(root, f)
                    add(p, os.path.relpath(p, HUB).replace("\\", "/"))
        if friends:
            def cache(name):
                p = os.path.join(C.DATA, name)
                return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else {}
            store = {k: v for k, v in cache("store_cache.json").items() if k in appids}   # drops "search:<folder>" keys
            wiki = {k: v for k, v in cache("wiki_cache.json").items() if k in wikis}
            z.writestr(f"{TOP}/app/data/store_cache.json", json.dumps(store, ensure_ascii=False))
            z.writestr(f"{TOP}/app/data/wiki_cache.json", json.dumps(wiki, ensure_ascii=False))
        z.writestr(f"{TOP}/app/config.json", json.dumps(C.DEFAULTS, indent=2))  # fresh: no consent, no folders
    os.replace(out + ".tmp", out)
    sha = hashlib.sha256(open(out, "rb").read()).hexdigest()
    with open(out + ".sha256", "w", encoding="ascii", newline="\n") as f:
        f.write(f"{sha}  {os.path.basename(out)}\n")
    print(f"Wrote {out}: {n} files, {os.path.getsize(out) / 1e6:.0f} MB")
    print(f"SHA-256: {sha}   (share this so friends can check they got the same file)")


if __name__ == "__main__":
    main()
