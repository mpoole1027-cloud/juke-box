"""launchd entry point for party-lights (com.partyjukebox.lights).

Run with party-lights' own Python from its directory. party-lights wraps the
serial driver so a missing or re-plugged DMX adapter is retried every couple of
seconds, and the watchdog alerts while it's gone. Older party-lights checkouts
without that wrapper exit when the port is missing, so for those this falls
back to the null driver to keep the web UI and jukebox polling up; plugging the
adapter in then needs a restart:

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
try:
    import partylights.dmx.reconnect  # noqa: F401,E402
    retries = True
except ImportError:
    retries = False
if (not retries and settings.get('dmx.driver', 'null') == 'serial' and port
        and not os.path.exists(port)):
    print(f"{time.strftime('%F %T')} DMX adapter not found at {port}; "
          "running without hardware.", flush=True)
    argv += ['--driver', 'null']
sys.exit(cli.main(argv))
