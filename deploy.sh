#!/bin/sh
# Copies the app to another device (e.g. a phone) over ssh and installs it there.
# Usage: ./deploy.sh user@host
set -e
HOST=${1:?usage: deploy.sh user@host}
cd "$(dirname "$0")"
TMP=$(ssh "$HOST" mktemp -d)
tar cf - install.sh speksweeper-launch speksweeper/*.py tools/*.py io.github.speksweeper.SpekSweeper.desktop \
    | ssh "$HOST" "cd $TMP && tar xf - && sh install.sh && rm -rf $TMP"
