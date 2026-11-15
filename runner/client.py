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
import urllib.request

import nxcrypt

URL = os.environ["NX_URL"].rstrip("/")
JOB = os.environ["NX_JOB"]
TOKEN = os.environ["NX_TOKEN"]
KEY = bytes.fromhex(os.environ["NX_KEY"])


def _req(method, path, body=None, tries=8):
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(URL + path, data=body, method=method, headers={
                "Authorization": "Bearer " + TOKEN, "User-Agent": "builder",
                "Content-Type": "application/octet-stream"})
            with urllib.request.urlopen(req, timeout=180) as r:
                return r.read()
        except Exception as e:  # tunnel hiccups: wait and try again
            last = e
            if attempt >= 2:
                print(f"retry {attempt + 1}: {type(e).__name__}", file=sys.stderr)
            time.sleep(min(60, 3 * (attempt + 1)))
    raise SystemExit(f"request failed after {tries} tries ({type(last).__name__})")


def say(text):
    try:
        _req("POST", f"/say?job={JOB}", nxcrypt.seal(KEY, "say", 0, text.encode()), tries=3)
    except SystemExit:
        pass


def job():
    return json.loads(nxcrypt.open_(KEY, "job", 0, _req("GET", f"/job?job={JOB}")))


PARALLEL = int(os.environ.get("NX_PARALLEL", "8"))


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
        data = nxcrypt.open_(KEY, f"f{i}", p, _req("GET", f"/file?job={JOB}&i={i}&p={p}"))
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
        parts = (f["size"] + nxcrypt.PIECE - 1) // nxcrypt.PIECE
        with cf.ThreadPoolExecutor(PARALLEL) as ex:
            for fut in cf.as_completed([ex.submit(piece, i, p, path) for p in range(parts)]):
                fut.result()
        open(path + ".ok", "w").close()
    finished[0] = True
    say(f"download complete: {total / 1e9:.2f} GB in {(time.time() - t0) / 60:.1f} min")


def put(path, name):
    size = os.path.getsize(path)
    parts = max(1, (size + nxcrypt.PIECE - 1) // nxcrypt.PIECE)
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(8 << 20), b""):
            h.update(chunk)
    stream = f"u:{name}"
    t0 = time.time()

    def piece(p):
        with open(path, "rb") as fh:
            fh.seek(p * nxcrypt.PIECE)
            data = fh.read(nxcrypt.PIECE)
        _req("PUT", f"/up?job={JOB}&name={urllib.request.quote(name)}&p={p}", nxcrypt.seal(KEY, stream, p, data))

    for attempt in range(3):
        with cf.ThreadPoolExecutor(PARALLEL) as ex:
            list(ex.map(piece, range(parts)))
        try:
            _req("POST", f"/done?job={JOB}&name={urllib.request.quote(name)}&size={size}&sha={h.hexdigest()}", b"", tries=4)
            say(f"sent {name}: {size / 1e6:.1f} MB in {(time.time() - t0) / 60:.1f} min")
            return
        except SystemExit:
            say(f"{name}: send failed, trying again")
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
