#!/bin/sh
# Installs SpekSweeper for the current user and creates a launcher.
# Usage: ./install.sh            (installs to ~/.local/share/speksweeper)
#        ./install.sh --uninstall
set -e
APP_ID=io.github.speksweeper.SpekSweeper
DATA=${XDG_DATA_HOME:-$HOME/.local/share}
DEST=$DATA/speksweeper
DESKTOP=$DATA/applications/$APP_ID.desktop
cd "$(dirname "$0")"

if [ "$1" = "--uninstall" ]; then
    rm -rf "$DEST" "$DESKTOP"
    echo "removed $DEST and $DESKTOP"
    exit 0
fi

mkdir -p "$DEST/speksweeper" "$DEST/tools" "$DATA/applications"
cp speksweeper-launch "$DEST/"
cp speksweeper/*.py "$DEST/speksweeper/"
cp tools/*.py "$DEST/tools/"
sed "s|@DIR@|$DEST|" "$APP_ID.desktop" > "$DESKTOP"
echo "installed to $DEST, launcher: $DESKTOP"
