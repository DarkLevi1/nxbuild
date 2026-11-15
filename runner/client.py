"""The build machine's side of the tunnel (see nxcrypt.py). Nothing about the job is printed here:
progress goes back to the PC, encrypted.

  client.py job OUT.json          the job description
  client.py get DEST_DIR          every file of the job into DEST_DIR (resumable, 4 at a time)
  client.py put FILE NAME         sends FILE back to the PC as NAME
  client.py say TEXT...           a progress line on the PC
  client.py log FILE              sends a log file's text as progress lines"""
import concurrent.futures as cf
import hashlib
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

import nxcrypt

JOB = os.environ["NX_JOB"]
TOKEN = os.environ["NX_TOKEN"]
KEY = bytes.fromhex(os.environ["NX_KEY"])
GIST = os.environ.get("NX_GIST", "")
GH_TOKEN = os.environ.get("GH_TOKEN", "")
OWNER = os.environ.get("GITHUB_REPOSITORY_OWNER", "")
_url = [os.environ["NX_URL"].rstrip("/")]
_url_lock = threading.Lock()
_last_look = [0.0]

PATIENCE = 3600     # a file piece keeps being asked for this long (the PC can be away for a while)


def _gist_blob():
    """This job's entry in the gist. The API without a login allows 60 looks an hour, which a long outage
    uses up: with the run's own token first, then without, then the raw file (no limit, cached a few minutes)."""
    name = f"{JOB}.txt"
    api = f"https://api.github.com/gists/{GIST}"
    for hdr in ([{"Authorization": "Bearer " + GH_TOKEN}] if GH_TOKEN else []) + [{}]:
        try:
            req = urllib.request.Request(api, headers={"User-Agent": "builder", **hdr})
            with urllib.request.urlopen(req, timeout=30) as r:
                return ((json.load(r).get("files", {}).get(name) or {}).get("content", "")).strip()
        except Exception:
            continue
    if OWNER:
        try:
            req = urllib.request.Request(f"https://gist.githubusercontent.com/{OWNER}/{GIST}/raw/{name}?t={int(time.time())}",
                                         headers={"User-Agent": "builder", "Cache-Control": "no-cache"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode().strip()
        except Exception:
            pass
    return ""


def _refresh_url():
    """The PC restarts a tunnel that dies (a new address each time) and posts the address, encrypted, in a
    gist: look there when the PC can't be reached (at most every 30 s)."""
    if not GIST:
        return
    with _url_lock:
        if time.time() - _last_look[0] < 30:
            return
        _last_look[0] = time.time()
        try:
            blob = _gist_blob()
            if blob:
                url = nxcrypt.open_(KEY, "url", 0, bytes.fromhex(blob)).decode().rstrip("/")
                if url != _url[0]:
                    _url[0] = url
                    print("the PC's address changed: going on with the new one", file=sys.stderr)
        except Exception as e:
            print(f"address check failed: {type(e).__name__}", file=sys.stderr)


def _req(method, path, body=None, tries=8, patience=0):
    """tries attempts, or with patience: as many as fit in that many seconds."""
    last = None
    t0 = time.time()
    attempt = 0
    while attempt < tries or (patience and time.time() - t0 < patience):
        try:
            req = urllib.request.Request(_url[0] + path, data=body, method=method, headers={
                "Authorization": "Bearer " + TOKEN, "User-Agent": "builder",
                "Content-Type": "application/octet-stream"})
            with urllib.request.urlopen(req, timeout=180) as r:
                return r.read()
        except Exception as e:  # tunnel hiccups: wait and try again
            last = e
            if attempt % 10 == 2:
                print(f"retry {attempt + 1}: {type(e).__name__}", file=sys.stderr)
            # every failure looks for a newer address (at most every 30 s): a short-lived call (say) starts
            # with the address the run was given, which is gone after the PC restarted its tunnel
            _refresh_url()
            time.sleep(min(30, 3 * (attempt + 1)))
        attempt += 1
    raise SystemExit(f"request failed after {attempt} tries ({type(last).__name__})")


def say(text):
    try:
        _req("POST", f"/say?job={JOB}", nxcrypt.seal(KEY, "say", 0, text.encode()), tries=3)
    except SystemExit:
        pass


def job():
    return json.loads(nxcrypt.open_(KEY, "job", 0, _req("GET", f"/job?job={JOB}")))


PARALLEL = int(os.environ.get("NX_PARALLEL", "8"))
PUT_PARALLEL = 4      # back to the PC: its drive writes them one at a time anyway


def get(dest):
    j = job()
    os.makedirs(dest, exist_ok=True)
    total = sum(f["size"] for f in j["files"])
    done = [0]
    t0 = time.time()
    finished = [False]

    def report():   # every 2 minutes, whatever the pieces are doing
        while not finished[0]:
            for _ in range(120):
                if finished[0]:
                    return
                time.sleep(1)
            el = max(1, time.time() - t0)
            rate = done[0] / el
            left = (total - done[0]) / rate / 60 if rate > 0 else 0
            say(f"download {done[0] / 1e9:.2f} / {total / 1e9:.2f} GB ({rate / 1e6:.2f} MB/s, ~{left:.0f} min left)")

    threading.Thread(target=report, daemon=True).start()

    def piece(i, p, path):
        data = nxcrypt.open_(KEY, f"f{i}", p, _req("GET", f"/file?job={JOB}&i={i}&p={p}", tries=20, patience=PATIENCE))
        with open(path, "r+b") as fh:
            fh.seek(p * nxcrypt.PIECE)
            fh.write(data)
        done[0] += len(data)
        return len(data)

    for i, f in enumerate(j["files"]):
        path = os.path.join(dest, f["name"])
        if os.path.exists(path) and os.path.getsize(path) == f["size"] and os.path.exists(path + ".ok"):
            done[0] += f["size"]
            continue
        with open(path, "wb") as fh:
            fh.truncate(f["size"])
        todo = list(range((f["size"] + nxcrypt.PIECE - 1) // nxcrypt.PIECE))
        for _ in range(4):          # a piece that gave up is asked for again in the next pass
            failed = []
            with cf.ThreadPoolExecutor(PARALLEL) as ex:
                futs = {ex.submit(piece, i, p, path): p for p in todo}
                for fut in cf.as_completed(futs):
                    try:
                        fut.result()
                    except BaseException:
                        failed.append(futs[fut])
            if not failed:
                break
            say(f"download: {len(failed)} pieces again")
            todo = sorted(failed)
        else:
            raise SystemExit("download failed")
        open(path + ".ok", "w").close()
    finished[0] = True
    say(f"download complete: {total / 1e9:.2f} GB in {(time.time() - t0) / 60:.1f} min")


def _status(method, path, body=None):
    """One request; (HTTP status, body), 0 when it didn't get through."""
    try:
        req = urllib.request.Request(_url[0] + path, data=body, method=method, headers={
            "Authorization": "Bearer " + TOKEN, "User-Agent": "builder", "Content-Type": "application/octet-stream"})
        with urllib.request.urlopen(req, timeout=180) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception:
        _refresh_url()
        return 0, b""


def put(path, name):
    size = os.path.getsize(path)
    parts = max(1, (size + nxcrypt.PIECE - 1) // nxcrypt.PIECE)
    stream = f"u:{name}"
    t0 = time.time()
    qname = urllib.request.quote(name)

    def piece(p):
        with open(path, "rb") as fh:
            fh.seek(p * nxcrypt.PIECE)
            data = fh.read(nxcrypt.PIECE)
        try:
            _req("PUT", f"/up?job={JOB}&name={qname}&p={p}", nxcrypt.seal(KEY, stream, p, data), tries=20,
                 patience=PATIENCE)
        except SystemExit:
            pass        # the PC lists it as missing at /done and it's sent again

    todo = list(range(parts))
    for attempt in range(6):
        with cf.ThreadPoolExecutor(PUT_PARALLEL) as ex:
            list(ex.map(piece, todo))
        for _ in range(60):         # up to 10 minutes for the PC to answer
            st, body = _status("POST", f"/done?job={JOB}&name={qname}&size={size}&sha=-", b"")
            if st == 200:
                say(f"sent {name}: {size / 1e6:.1f} MB in {(time.time() - t0) / 60:.1f} min")
                return
            if st == 409 and body.startswith(b"missing:"):
                todo = [int(x) for x in body[8:].decode().split(",") if x]
                say(f"{name}: sending {len(todo)} missing pieces again")
                break
            time.sleep(10)   # the tunnel or the PC is busy: ask again
    raise SystemExit("upload failed")


def log(path):
    try:
        text = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return
    # big logs: the last part matters most
    if len(text) > 400_000:
        text = "[... start cut ...]\n" + text[-400_000:]
    for off in range(0, len(text), 60_000):
        say(text[off:off + 60_000])


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "job":
        with open(sys.argv[2], "w") as fh:
            json.dump(job(), fh)
    elif cmd == "get":
        get(sys.argv[2])
    elif cmd == "put":
        put(sys.argv[2], sys.argv[3])
    elif cmd == "say":
        say(" ".join(sys.argv[2:]))
    elif cmd == "log":
        log(sys.argv[2])
