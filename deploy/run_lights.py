"""launchd entry point for party-lights (com.partyjukebox.lights).

Run with party-lights' own Python from its directory. With the serial driver
and the DMX adapter unplugged, party-lights can't open its port and exits, and
launchd restarts it every 10 s for as long as the Mac is on. Fall back to the
null driver instead so the web UI and jukebox polling keep running. After
plugging the adapter in, restart to pick it up:

    launchctl kickstart -k gui/$(id -u)/com.partyjukebox.lights

This is Python rather than a shell script because launchd's /bin/sh may not
read files under ~/Desktop or ~/Documents, while this Python already can.
"""
import os
import sys
import time

sys.path.insert(0, os.getcwd())
from partylights import cli                     # noqa: E402
from partylights.config import Settings         # noqa: E402

settings = Settings.load()
argv = ['run']
port = settings.get('dmx.port')
if settings.get('dmx.driver', 'null') == 'serial' and port and not os.path.exists(port):
    print(f"{time.strftime('%F %T')} DMX adapter not found at {port}; "
          "running without hardware.", flush=True)
    argv += ['--driver', 'null']
sys.exit(cli.main(argv))
