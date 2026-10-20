#!/bin/bash
# Double-click in Finder to set up AudioLab (macOS): installs it on the first run, then adds
# AudioLab to your Applications folder and opens it — later, open it from there.
cd "$(dirname "$0")" && exec ./start.sh "$@"
