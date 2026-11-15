#!/bin/bash
# One build on the runner. The public log only names the stages; everything else (the job, the build's
# own output) goes back to the PC encrypted (client.py).
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
W="$HOME/work"
LOG="$W/build.log"
mkdir -p "$W"
: > "$LOG"
say() { python3 "$HERE/client.py" say "$*"; }
stage() { echo "== $1"; say "stage: $1"; }
fail() {
    echo "== failed: $1"
    say "FAILED: $1"
    # the whole log as a file (never lost to a text problem), and its end as progress lines
    python3 "$HERE/client.py" put "$LOG" build.log || true
    tail -c 60000 "$LOG" > "$W/tail.log"; python3 "$HERE/client.py" log "$W/tail.log"
    # the packer's own step logs, if it got that far
    if [ -d "$W/build/logs" ]; then
        (cd "$W/build" && tar czf "$W/steplogs.tgz" logs) && python3 "$HERE/client.py" put "$W/steplogs.tgz" steplogs.tgz || true
    fi
    exit 1
}

python3 -m pip install --quiet cryptography >/dev/null 2>&1 || fail "python packages"
stage "machine up"
python3 "$HERE/client.py" job "$W/job.json" || fail "job"

stage "making room"
{
    for x in /Applications/Xcode_*.app; do
        case "$x" in *27.2*) ;; *) [ -L "$x" ] || sudo rm -rf "$x" ;; esac
    done
    sudo rm -rf "$HOME/Library/Android" /usr/local/share/dotnet "$HOME/.dotnet" /usr/local/lib/node_modules \
        "$HOME/Library/Developer/CoreSimulator" /Library/Developer/CoreSimulator/Volumes 2>/dev/null
    df -h /System/Volumes/Data
} >> "$LOG" 2>&1
say "$(df -h /System/Volumes/Data | tail -1 | awk '{print "free disk: " $4}')"

stage "tools"
{
    xcodebuild -downloadComponent MetalToolchain
    brew install xcodegen zstd glslang
    python3 -m pip install lz4 "capstone>=5,<6" fonttools nsz zbic
} >> "$LOG" 2>&1 || fail "tools"

stage "source"
{
    git clone --depth 1 https://github.com/seedds/nxnative.git "$W/nxnative" &&
    git -C "$W/nxnative" log -1 --format='nxnative %h %cs' &&
    python3 "$HERE/patch_host.py" "$W/nxnative"
} >> "$LOG" 2>&1 || fail "source"
say "$(git -C "$W/nxnative" log -1 --format='nxnative %h (%cs)')"

stage "game files"
python3 "$HERE/client.py" get "$W/in" || fail "game files"
mkdir -p "$W/games"
# the keys stay out of the game folder (the packer would take every file there as the game's)
mv "$W/in/prod.keys" "$W/prod.keys"
for f in "$W/in"/*; do case "$f" in *.ok) rm -f "$f" ;; esac; done

stage "build"
ARGS=$(python3 -c "import json,shlex; print(' '.join(shlex.quote(a) for a in json.load(open('$W/job.json'))['args']))")
TID=$(python3 -c "import json; print(json.load(open('$W/job.json'))['title_id'])")
BID="org.nxnative.$(echo "$TID" | tr 'A-F' 'a-f')"
(
    cd "$W/nxnative" &&
    eval python3 scripts/build_ipa_from_nsp.py "\"$W/in\"" -k "\"$W/prod.keys\"" --romfs separate --progress lines \
        --work-dir "\"$W/build\"" -o "\"$W/out/game.ipa\"" --bundle-id "$BID" $ARGS
) >> "$LOG" 2>&1 &
BPID=$!
# a heartbeat to the PC while it builds: the newest stage line, every 5 minutes
while kill -0 $BPID 2>/dev/null; do
    sleep 300
    kill -0 $BPID 2>/dev/null && say "building: $(grep -v '^\s*$' "$LOG" | tail -1 | cut -c1-200)"
done
wait $BPID || fail "build"
rm -f "$W/prod.keys"

stage "sending back"
ls -la "$W/out" >> "$LOG" 2>&1
python3 "$HERE/client.py" put "$W/out/game.ipa" game.ipa || fail "send app"
# the packed version's RomFS image: the biggest one (a DLC's are small)
IMG=$(find "$W/build" -iname 'romfs.img' -size +1M -exec ls -s {} + 2>/dev/null | sort -rn | head -1 | awk '{print $2}')
find "$W/build" -iname '*.img' -exec ls -la {} + >> "$LOG" 2>&1
[ -n "$IMG" ] || fail "no romfs image"
python3 "$HERE/client.py" put "$IMG" romfs.img || fail "send game files"
python3 "$HERE/client.py" log "$LOG"
stage "done"
