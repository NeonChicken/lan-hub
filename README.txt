LAN HUB
=======
An interactive overview for LAN-party games: your Steam games plus 
non-Steam classics, installers and archives, with player counts, 
LAN tips and Play / Install / Open folder buttons.

START
  Double-click "LAN Hub.exe". It sits in the system tray (next to the clock)
  and opens the hub in your browser. Right-click the tray icon to reopen it,
  rescan, copy your LAN IP or quit.

  The first time, it asks what it may look at (Steam library, which folders,
  internet for artwork) before it reads anything.

  Keep the whole folder together: LAN Hub.exe needs the "app" folder next to it.

UNINSTALL
  Quit LAN Hub from the tray icon and delete the folder. It installs nothing
  and changes nothing outside this folder.

"WINDOWS PROTECTED YOUR PC"?
  Click "More info" > "Run anyway" 
  Downloaded files (browser, Discord) get this warning because LAN Hub isn't
  code-signed which is expensive. 

WHAT'S INSIDE
  LAN Hub.exe              small launcher, source: app\launcher\LANHub.cs
  app\tools\*.py           the hub server + game scanner (readable Python)
  app\runtime\python       official portable Python 3.12.8 (signed by the
                           Python Software Foundation)
  app\index.html           the page itself
  app\config.json          your choices from the permission screen
  app\Start LAN Hub (fallback).bat   same as the exe, with a console window

WHAT IT CAN AND CAN'T DO
  * Reads only what you allowed: Steam's library files and the folders you ticked.
  * Downloads artwork and descriptions only from the Steam store, the Steam CDN
    and Wikipedia (or nothing, if you untick internet).
  * Only reachable from this PC (127.0.0.1), not from your network.
  * Only launches games, installers and folders it found in your allowed folders
    (or a game you pointed it to with "Locate game").
  * Only writes inside its own folder (new LAN Hub files only when you click Update).
  * Updates come from github.com/NeonChicken/lan-hub and must match GitHub's checksum.

FOR THE CURATOR
  Player counts, LAN flags and tips:  app\tools\curation.py (public)
  Your own games, paths and notes:    app\tools\curation_local.py (never on GitHub, see the .example.py)
  Rebuild the exe:                    app\launcher\build.bat
  Make a zip for friends:             python app\tools\package.py --friends  (-> builds\)
  Make the public release zip:        python app\tools\package.py            (-> builds\)
