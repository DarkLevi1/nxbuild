#!/bin/bash
# A 3DS game for iPhone: its code, already turned into C on the PC (3dsrecomp's recompiler), compiled here with
# 3dsrecomp's runtime into an app with a game list (recomp + Frameworks/<name>.dylib per game). The game's data
# (romfs.bin) never comes here: the PC puts it into the app. The public log only names the stages.
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
    python3 "$HERE/client.py" put "$LOG" build.log || true
    tail -c 60000 "$LOG" > "$W/tail.log"; python3 "$HERE/client.py" log "$W/tail.log"
    exit 1
}

python3 -m pip install --quiet cryptography >/dev/null 2>&1 || fail "python packages"
stage "machine up"
python3 "$HERE/client.py" job "$W/job.json" || fail "job"
arg() { python3 -c "import json,sys; a=dict(x.split('=',1) for x in json.load(open('$W/job.json'))['args']); print(a.get(sys.argv[1], sys.argv[2]))" "$1" "$2"; }
REV=$(arg rev main)
GAMES=$(arg games "")
APPNAME=$(arg app "3DS")
BID=$(arg bundle "com.yata.ctr")
# app: a 3DS app of its own; yata: the runtime and the games' code as libraries Yata loads (patch_ctr_host.py)
MODE=$(arg mode app)
if [ "$MODE" = yata ]; then SDL_KIND="-DSDL_SHARED=ON -DSDL_STATIC=OFF"; else SDL_KIND="-DSDL_SHARED=OFF -DSDL_STATIC=ON"; fi

stage "tools"
{
    command -v ninja || brew install ninja
    command -v cmake || brew install cmake
    xcrun -f metal || xcodebuild -downloadComponent MetalToolchain
    sysctl -n hw.ncpu hw.memsize
    df -h /System/Volumes/Data
} >> "$LOG" 2>&1 || fail "tools"

stage "source"
{
    R="$W/3dsrecomp"
    git init -q "$R" && git -C "$R" remote add origin https://github.com/nathsou/3dsrecomp.git &&
    git -C "$R" fetch -q --depth 1 origin "$REV" && git -C "$R" checkout -q FETCH_HEAD &&
    git -C "$R" log -1 --format='3dsrecomp %h %cs' &&
    # SDL3 for iOS (static; a shared one for Yata's libraries, which use Yata's own SDL3 on the phone)
    TAG=$(curl -fsSL -H "Authorization: Bearer $GH_TOKEN" https://api.github.com/repos/libsdl-org/SDL/releases/latest |
          python3 -c "import json,sys; print(json.load(sys.stdin)['tag_name'])") &&
    echo "SDL $TAG" &&
    curl -fsSL "https://github.com/libsdl-org/SDL/archive/refs/tags/$TAG.tar.gz" -o "$W/sdl.tgz" &&
    mkdir -p "$W/sdl" && tar xzf "$W/sdl.tgz" -C "$W/sdl" --strip-components 1 &&
    cmake -S "$W/sdl" -B "$W/sdl-build" -G Ninja -DCMAKE_SYSTEM_NAME=iOS -DCMAKE_OSX_SYSROOT=iphoneos \
        -DCMAKE_OSX_ARCHITECTURES=arm64 -DCMAKE_OSX_DEPLOYMENT_TARGET=16.0 -DCMAKE_BUILD_TYPE=Release \
        $SDL_KIND -DSDL_TEST_LIBRARY=OFF -DSDL_EXAMPLES=OFF \
        -DCMAKE_INSTALL_PREFIX="$R/external/sdl-ios/iphoneos" > /dev/null &&
    cmake --build "$W/sdl-build" > /dev/null && cmake --install "$W/sdl-build" > /dev/null &&
    if [ "$MODE" = yata ]; then python3 "$HERE/patch_ctr_host.py" "$R"; fi
} >> "$LOG" 2>&1 || fail "source"
say "$(git -C "$W/3dsrecomp" log -1 --format='3dsrecomp %h (%cs)')"

stage "game files"
python3 "$HERE/client.py" get "$W/in" || fail "game files"
(
    cd "$W/3dsrecomp" &&
    for t in "$W/in"/*.tar.gz; do tar xzf "$t"; done &&
    ls data generated && du -sh generated/*
) >> "$LOG" 2>&1 || fail "unpack"

stage "build"
R="$W/3dsrecomp"
(
    cd "$R" &&
    cmake -S . -B build-ios -G Ninja -DCMAKE_SYSTEM_NAME=iOS -DCMAKE_OSX_SYSROOT=iphoneos \
        -DCMAKE_OSX_ARCHITECTURES=arm64 -DCMAKE_OSX_DEPLOYMENT_TARGET=16.0 -DCMAKE_BUILD_TYPE=Release \
        "-DGAMES=$GAMES" $( [ "$MODE" = yata ] && echo -DYATA_HOST=ON ) &&
    ninja -C build-ios
) >> "$LOG" 2>&1 &
BPID=$!
while kill -0 $BPID 2>/dev/null; do
    sleep 300
    kill -0 $BPID 2>/dev/null && say "building: $(grep -v '^\s*$' "$LOG" | tail -1 | cut -c1-200)"
done
wait $BPID || fail "build"

if [ "$MODE" = yata ]; then
    stage "libraries"
    (
        cd "$R/build-ios" &&
        # SDL is Yata's own SDL3.framework on the phone
        OLD=$(otool -L libctrhost.dylib | awk '/SDL3/ {print $1; exit}') && echo "SDL was $OLD" &&
        install_name_tool -change "$OLD" @rpath/SDL3.framework/SDL3 libctrhost.dylib &&
        mkdir -p "$W/libs" && cp libctrhost.dylib "$W/libs/" &&
        for g in $(echo "$GAMES" | tr ';' ' '); do cp "$g.dylib" "$W/libs/" || exit 1; done &&
        for m in "$W/libs"/*.dylib; do codesign -f -s - "$m" && otool -L "$m" | head -8 || exit 1; done &&
        (cd "$W/libs" && tar czf "$W/ctr.tar.gz" *.dylib) && ls -la "$W/libs" "$W/ctr.tar.gz"
    ) >> "$LOG" 2>&1 || fail "libraries"
    stage "sending back"
    python3 "$HERE/client.py" put "$W/ctr.tar.gz" ctr.tar.gz || fail "send libraries"
    python3 "$HERE/client.py" log "$LOG"
    stage "done"
    exit 0
fi

stage "app"
(
    cd "$R" &&
    BUILD=build-ios BUNDLE_ID="$BID" sh tools/bundle_ios.sh --games $(echo "$GAMES" | tr ';' ' ') --name "$APPNAME" &&
    A="build-ios/$APPNAME.app" &&
    P=/usr/libexec/PlistBuddy &&
    # the app's files can be read from the PC (logs, missing_functions.txt) and from the Files app
    $P -c "Add :UIFileSharingEnabled bool true" "$A/Info.plist" &&
    $P -c "Add :LSSupportsOpeningDocumentsInPlace bool true" "$A/Info.plist" &&
    cat > "$W/app.entitlements" <<'PLIST' &&
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>com.apple.developer.kernel.increased-memory-limit</key>
	<true/>
	<key>get-task-allow</key>
	<true/>
</dict>
</plist>
PLIST
    for m in "$A"/Frameworks/*.dylib; do codesign -f -s - "$m"; done &&
    codesign -f -s - --entitlements "$W/app.entitlements" "$A" &&
    mkdir -p "$W/ipa/Payload" && cp -R "$A" "$W/ipa/Payload/" &&
    (cd "$W/ipa" && zip -qry "$W/app.ipa" Payload) &&
    ls -la "$W/app.ipa" "$A" "$A/Frameworks" "$A/games"/*
) >> "$LOG" 2>&1 || fail "app"

stage "sending back"
python3 "$HERE/client.py" put "$W/app.ipa" app.ipa || fail "send app"
python3 "$HERE/client.py" log "$LOG"
stage "done"
