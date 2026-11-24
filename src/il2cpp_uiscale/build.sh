#!/bin/sh
# Build janus_uiscale.dll (needs gcc-mingw-w64-x86-64). Output is COMMITTED in
# the handlers that inject it (handlers/gunfire-reborn/payload/inject/).
set -e
cd "$(dirname "$0")"
x86_64-w64-mingw32-gcc -O2 -Wall -Wextra -Wno-unused-parameter -Wno-cast-function-type \
    -shared -static-libgcc -o janus_uiscale.dll uiscale.c
x86_64-w64-mingw32-strip janus_uiscale.dll
for d in ../../handlers/gunfire-reborn/payload/inject; do mkdir -p "$d"; cp janus_uiscale.dll "$d/"; done
rm janus_uiscale.dll
echo "built janus_uiscale.dll"
