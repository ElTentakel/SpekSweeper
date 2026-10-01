#!/bin/sh
# Kopiert die App aufs Telefon und legt einen Starter an.
set -e
HOST=${1:?usage: deploy.sh user@host}
DIR=.local/share/speksweeper
cd "$(dirname "$0")"
tar cf - speksweeper-launch speksweeper/*.py tools/*.py io.github.speksweeper.SpekSweeper.desktop | ssh "$HOST" "mkdir -p $DIR .local/share/applications && cd $DIR && tar xf - && sed 's|@DIR@|'\$HOME/$DIR'|' io.github.speksweeper.SpekSweeper.desktop > ~/.local/share/applications/io.github.speksweeper.SpekSweeper.desktop"
echo "installiert nach $HOST:~/$DIR"
