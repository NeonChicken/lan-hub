# -*- coding: utf-8 -*-
"""Example of curation_local.py: YOUR games, paths and notes.

Copy this file to curation_local.py (same folder) and edit it. curation_local.py is in .gitignore,
so your library never ends up on GitHub. LAN Hub loads it automatically when it exists.

  * An entry with the same id as one in curation.CLASSICS replaces it.
  * Paths can be absolute (your PC). On a friend's PC LAN Hub still recognises the same files by name,
    as long as they're inside a folder that friend approved.
  * Make a zip for friends that includes this file:  python app/tools/package.py --friends
"""

# Where the paths below start. Stripped to recognise the same layout on a friend's PC.
CURATOR_ROOTS = ["D:\\LAN Games\\"]

# Extra or corrected Steam metadata: appid: (players, couch, lan, genre, note)
STEAM = {
    # 440: ("2–24", 0, 1, "shooter", "Our house rules: no Pyros on 2Fort."),
}

L = "D:\\LAN Games\\"
LOCAL = [
    # A portable game: "launch" is the exe, "folder" is what "Open folder" shows.
    dict(id="mygame", title="My Favourite Game", players="2–8", couch=0, lan=1, genre="shooter", year=2004,
         launch=L + "My Favourite Game\\Game.exe", args="-windowed", folder=L + "My Favourite Game",
         wiki="Some_Wikipedia_Page", note="Host → LAN. Password is in the group chat."),
    # An installer: the tile says Install. After you install it, LAN Hub finds the game by itself
    # (or use "Locate game…" on the tile).
    dict(id="myinstaller", title="Another Game", players="1–4", lan=1, genre="coop", year=2010, steam=12345,
         install=L + "setup_another_game_1.0.exe", aka=["Another Game"], note="Installer from GOG."),
]
