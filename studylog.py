#!/usr/bin/env python3
"""
studylog - pull study scans off an Android phone, catalog them, commit them.

  studylog                               ingest new scans from the phone
  studylog skip SOURCE PROBLEM "REASON"  record a skipped problem
  studylog index                         rebuild INDEX.md from log.tsv

Each problem becomes one PDF under scans/<source>/, one row in log.tsv,
and a line in INDEX.md (generated, grouped by source, with counts).
Originals are deleted from the phone only after the git commit succeeds.
"""
import argparse
import csv
import datetime as dt
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from PIL import Image, ImageOps

# ---- configuration ---------------------------------------------------------
PHONE_DIR = "/sdcard/DCIM/Zmathscan"
REPO = Path(Path.home() / "dev" / "py" / "studylog")
CACHE = Path.home() / ".cache" / "studylog"
MAX_SIDE = 1800        # px on the long side (~160 dpi for a letter page)
JPEG_QUALITY = 70
# ---------------------------------------------------------------------------

INBOX = CACHE / "inbox"
VIEW = CACHE / "current.jpg"
LOG = REPO / "log.tsv"
FIELDS = ["date", "source", "problem", "status", "provenance", "note", "file"]
PROV = {"c": "cold", "n": "nudge", "r": "read"}
STATUS = {"d": "done", "o": "open"}
IMG_EXT = {".jpg", ".jpeg", ".png", ".webp"}


def die(msg):
    sys.exit(f"studylog: {msg}")


def run(cmd):
    return subprocess.run(cmd, check=True, text=True, capture_output=True).stdout


def natkey(s):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]


def slug(s):
    return re.sub(r"[^A-Za-z0-9._-]+", "-", s).strip("-")


def bump(problem):
    """abbott 2.3.4 -> 2.3.5, A1 -> A2: increment the last number."""
    nums = list(re.finditer(r"\d+", problem))
    if not nums:
        return problem
    m = nums[-1]
    return problem[:m.start()] + str(int(m.group()) + 1) + problem[m.end():]


# ---- log and index ---------------------------------------------------------

def read_log():
    if not LOG.exists():
        return []
    with LOG.open(newline="") as f:
        return list(csv.DictReader(f, delimiter="\t"))


def append_log(rows):
    new = not LOG.exists()
    with LOG.open("a", newline="") as f:
        w = csv.DictWriter(f, FIELDS, delimiter="\t")
        if new:
            w.writeheader()
        w.writerows(rows)


def rebuild_index():
    by_src = {}
    for r in read_log():
        by_src.setdefault(r["source"], []).append(r)
    out = ["# Study log", "",
           "Every problem attempted, left open, or skipped. Provenance: "
           "**cold** = no help, **nudge** = took a hint, **read** = read the "
           "solution first. Corrections are in [ERRATA.md](ERRATA.md).", ""]
    for src in sorted(by_src):
        rs = sorted(by_src[src], key=lambda r: natkey(r["problem"]))
        n = lambda k, v: sum(r[k] == v for r in rs)
        out += [f"## {src}", "",
                f"{n('status', 'done')} done · {n('status', 'open')} open · "
                f"{n('status', 'skipped')} skipped — "
                f"{n('provenance', 'cold')} cold, {n('provenance', 'nudge')} nudge, "
                f"{n('provenance', 'read')} read", "",
                "| problem | date | status | provenance | note |",
                "|---|---|---|---|---|"]
        for r in rs:
            p = f"[{r['problem']}]({r['file']})" if r["file"] else r["problem"]
            note = r["note"].replace("|", "\\|")
            out.append(f"| {p} | {r['date']} | {r['status']} | "
                       f"{r['provenance']} | {note} |")
        out.append("")
    (REPO / "INDEX.md").write_text("\n".join(out))


def commit(msg):
    git = ["git", "-C", str(REPO)]
    run(git + ["add", "-A"])
    if not run(git + ["status", "--porcelain"]).strip():
        print("nothing to commit")
        return
    run(git + ["commit", "-q", "-m", msg])
    print(f"committed: {msg}")
    if run(git + ["remote"]).strip():
        try:
            run(git + ["push", "-q"])
            print("pushed")
        except subprocess.CalledProcessError as e:
            print(f"push failed (commit is safe locally): {e.stderr.strip()}")


# ---- phone -----------------------------------------------------------------

def adb_ready():
    if not shutil.which("adb"):
        die("adb not found (sudo apt install adb)")
    try:
        return run(["adb", "get-state"]).strip() == "device"
    except subprocess.CalledProcessError:
        return False


def pull_from_phone():
    INBOX.mkdir(parents=True, exist_ok=True)
    if not adb_ready():
        print("no phone (or USB debugging not authorized); processing inbox only")
        return
    try:
        listing = run(["adb", "shell", f"ls -1 {shlex.quote(PHONE_DIR)}"])
    except subprocess.CalledProcessError:
        die(f"can't list {PHONE_DIR} on the phone; create that folder first")
    for name in filter(None, (l.strip() for l in listing.splitlines())):
        if not (INBOX / name).exists():
            run(["adb", "pull", "-a", f"{PHONE_DIR}/{name}", str(INBOX / name)])
            print(f"pulled {name}")


def delete_from_phone(names):
    if not adb_ready():
        print("phone not connected; originals left on the phone")
        return
    for name in names:
        run(["adb", "shell", f"rm -f -- {shlex.quote(PHONE_DIR + '/' + name)}"])
    print(f"removed {len(names)} file(s) from the phone")


# ---- images ----------------------------------------------------------------

def pages_of(path):
    """(label, image path) for each page of an inbox file; PDFs get rasterized."""
    ext = path.suffix.lower()
    if ext == ".pdf":
        if not shutil.which("pdftoppm"):
            die("pdftoppm not found (sudo apt install poppler-utils)")
        outdir = CACHE / "raster" / path.stem
        outdir.mkdir(parents=True, exist_ok=True)
        run(["pdftoppm", "-r", "200", "-jpeg", str(path), str(outdir / "p")])
        pages = sorted(outdir.glob("p-*.jpg"), key=lambda p: natkey(p.name))
        return [(f"{path.name} p{i}", p) for i, p in enumerate(pages, 1)]
    if ext in IMG_EXT:
        return [(path.name, path)]
    print(f"skipping {path.name}: not an image or PDF")
    return []


def prepare(path):
    im = ImageOps.exif_transpose(Image.open(path))
    im = ImageOps.autocontrast(im.convert("L"), cutoff=1)
    im.thumbnail((MAX_SIDE, MAX_SIDE))
    return im


class Viewer:
    """One feh window that reloads VIEW every second; we overwrite VIEW."""

    def __init__(self):
        self.proc = None
        self.term = terminal_window()   # grab it before feh steals focus

    def show(self, im):
        tmp = VIEW.with_name("current.tmp.jpg")
        im.save(tmp, quality=85)
        os.replace(tmp, VIEW)          # atomic, so feh never reads half a file
        if self.proc is None:
            # stdin must not be the terminal: feh reads keystrokes from a tty
            # stdin and puts it in raw mode, which swallows our input().
            self.proc = subprocess.Popen(
                ["feh", "--reload", "1", "--scale-down", "--auto-zoom",
                 "--title", "studylog", str(VIEW)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)
            refocus(self.term, self.proc.pid)

    def close(self):
        if self.proc:
            self.proc.terminate()
            self.proc.wait()


def xdo(*args, timeout=3):
    try:
        return subprocess.run(["xdotool", *args], capture_output=True,
                              text=True, timeout=timeout).stdout.strip()
    except subprocess.TimeoutExpired:
        return ""


def terminal_window():
    """X window id of the terminal we're running in, if we can tell."""
    if not shutil.which("xdotool"):
        return None
    return os.environ.get("WINDOWID") or xdo("getactivewindow") or None


def refocus(term, feh_pid):
    """Give keyboard focus back to the terminal once feh's window is up.

    dwm ignores _NET_ACTIVE_WINDOW (it only marks the window urgent) and
    re-steals a plain XSetInputFocus in its FocusIn handler, so if
    activation doesn't take we warp the pointer into the terminal, which
    dwm's focus-follows-mouse honors.
    """
    if not term:
        return
    # wait until feh has mapped its window, otherwise it grabs focus after us
    xdo("search", "--sync", "--onlyvisible", "--pid", str(feh_pid), timeout=5)
    time.sleep(0.1)
    xdo("windowactivate", term)
    if xdo("getactivewindow") != str(int(term, 0)):
        geo = dict(l.split("=", 1) for l in
                   xdo("getwindowgeometry", "--shell", term).splitlines()
                   if "=" in l)
        x, y = int(geo.get("WIDTH", 40)) // 2, int(geo.get("HEIGHT", 40)) // 2
        xdo("mousemove", "--window", term, str(x), str(y))


# ---- prompts ---------------------------------------------------------------

def ask(prompt, default=""):
    s = input(f"{prompt} [{default}]: " if default else f"{prompt}: ").strip()
    return s or default


def parse_keys(ans, last):
    """'abbott 2.3.5, 2.3.6' -> [(abbott, 2.3.5), (abbott, 2.3.6)]."""
    if ans in ("x", "+"):
        return ans
    src = last[0] if last else None
    keys = []
    for tok in filter(None, (t.strip() for t in ans.split(","))):
        parts = tok.split(None, 1)
        if len(parts) == 2:
            src, prob = parts
        elif src:
            prob = parts[0]
        else:
            print("  the first entry needs a source, e.g. 'abbott 1.2.3'")
            return None
        keys.append((src, prob))
    return keys or None


def parse_prov(ans):
    code, _, note = ans.partition(":")
    code = code.strip().lower()
    code = PROV.get(code, code)
    return (code, note.strip()) if code in PROV.values() else None


# ---- commands --------------------------------------------------------------

def ingest():
    if not (REPO / ".git").exists():
        die(f"{REPO} is not a git repo (git init it)")
    pull_from_phone()
    files = sorted((p for p in INBOX.iterdir() if p.is_file()),
                   key=lambda p: (p.stat().st_mtime, p.name))
    if not files:
        print("nothing to process")
        return

    rows = read_log()
    last = (rows[-1]["source"], rows[-1]["problem"]) if rows else None
    problems = {}          # (source, problem) -> info; dicts keep insertion order
    current = []           # problems the previous page belonged to
    viewer = Viewer()
    print("Enter accepts the default. '+' = page continues the previous "
          "problem(s), 'x' = discard page.\nProblems: 'abbott 2.3.5', or just "
          "'2.3.6' to keep the source; separate several with commas.\n")
    try:
        for f in files:
            date = dt.date.fromtimestamp(f.stat().st_mtime).isoformat()
            for label, path in pages_of(f):
                im = prepare(path)
                viewer.show(im)
                default = f"{last[0]} {bump(last[1])}" if last else ""
                while True:
                    keys = parse_keys(ask(f"{label} — problem(s)", default), last)
                    if keys == "+" and not current:
                        print("  nothing to continue yet")
                        continue
                    if keys:
                        break
                if keys == "x":
                    continue
                if keys == "+":
                    for k in current:
                        problems[k]["pages"].append(im)
                    continue
                while not (prov := parse_prov(ask("  provenance c/n/r (n:note)", "c"))):
                    print("  c = cold, n = nudge, r = read; e.g. 'n:hint from ch.3 text'")
                while (status := STATUS.get(ask("  status d/o", "d").lower())) is None:
                    print("  d = done, o = open")
                for k in keys:
                    entry = problems.setdefault(k, {"pages": [], "date": date})
                    entry["pages"].append(im)
                    entry["prov"], entry["note"] = prov
                    entry["status"] = status
                current, last = keys, keys[-1]
    finally:
        viewer.close()

    new_rows = []
    for (src, prob), p in problems.items():
        d = REPO / "scans" / slug(src)
        d.mkdir(parents=True, exist_ok=True)
        dest, v = d / f"{slug(prob)}.pdf", 2
        while dest.exists():                     # a redo never overwrites
            dest, v = d / f"{slug(prob)}-v{v}.pdf", v + 1
        pages = p["pages"]
        pages[0].save(dest, save_all=True, append_images=pages[1:],
                      resolution=max(pages[0].size) / 11, quality=JPEG_QUALITY)
        new_rows.append({"date": p["date"], "source": src, "problem": prob,
                         "status": p["status"], "provenance": p["prov"],
                         "note": p["note"],
                         "file": str(dest.relative_to(REPO))})
    if new_rows:
        append_log(new_rows)
        rebuild_index()
        commit(f"log {dt.date.today().isoformat()}: {len(new_rows)} problem(s)")

    delete_from_phone([f.name for f in files])
    for f in files:
        f.unlink()
    shutil.rmtree(CACHE / "raster", ignore_errors=True)


def skip(source, problem, reason):
    append_log([{"date": dt.date.today().isoformat(), "source": source,
                 "problem": problem, "status": "skipped", "provenance": "",
                 "note": reason, "file": ""}])
    rebuild_index()
    commit(f"skip {source} {problem}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("skip", help="record a skipped problem")
    s.add_argument("source")
    s.add_argument("problem")
    s.add_argument("reason")
    sub.add_parser("index", help="rebuild INDEX.md")
    a = ap.parse_args()
    try:
        if a.cmd == "skip":
            skip(a.source, a.problem, a.reason)
        elif a.cmd == "index":
            rebuild_index()
            commit("rebuild index")
        else:
            ingest()
    except KeyboardInterrupt:
        sys.exit("\naborted: nothing written; scans are still in the inbox "
                 "and on the phone")
    except subprocess.CalledProcessError as e:
        die(f"{' '.join(map(str, e.cmd))} failed:\n{e.stderr}")


if __name__ == "__main__":
    main()
