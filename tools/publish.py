#!/usr/bin/env python3
"""Publish this repo to GitHub.

Tries `git push` first (fast path). If github.com:443 is unreachable
(common in mainland China: CONNECT tunnel failed / connection reset),
falls back to uploading every changed file through the GitHub REST
Contents API via `gh api --input -` (stdin, so we never hit the ~32KB
Windows command-line limit with base64 payloads).

Usage:
    python tools/publish.py                  # publish pending changes
    python tools/publish.py -m "message"     # custom commit message
    python tools/publish.py --init           # first push: send all tracked files
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OWNER = "fatekingcsr"
REPO = "fatekingcsr.github.io"
BRANCH = "main"

# files the repo owns; anything else in the tree is ignored by the API path
TRACKED = [
    ".gitattributes",
    ".gitignore",
    ".nojekyll",
    "tools/build-repo.yml",
    "CydiaIcon.png",
    "Packages",
    "Packages.bz2",
    "Packages.gz",
    "Release",
    "README.md",
    "index.html",
    "packages.json",
    "repo-data.js",
    "repo.json",
    "tools/gen_repo.py",
    "tools/publish.py",
]


def find_tool(names):
    """Locate an executable, checking PATH plus the usual Windows spots."""
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    extra = [
        r"C:\Program Files\GitHub CLI\gh.exe",
        r"C:\Users\王先森\.workbuddy\binaries\PortableGit\versions\1.2.0\mingw64\bin\git.exe",
        r"C:\Users\王先森\.workbuddy\binaries\PortableGit\versions\1.2.0\cmd\git.exe",
    ]
    for path in extra:
        if Path(path).exists() and Path(path).name.lower().startswith(names[0][:2].lower()):
            return path
    for path in extra:
        if Path(path).exists():
            base = Path(path).stem.lower()
            if any(base == n.lower() or base.startswith(n.lower()) for n in names):
                return path
    return None


GH = find_tool(["gh.exe", "gh"])
GIT = find_tool(["git.exe", "git"])

PG = Path(r"C:\Users\王先森\.workbuddy\binaries\PortableGit\versions\1.2.0")


def env_for_git():
    env = os.environ.copy()
    if PG.exists():
        parts = [str(PG / "mingw64" / "bin"), str(PG / "cmd"), str(PG / "usr" / "bin")]
        env["PATH"] = os.pathsep.join(parts + [env.get("PATH", "")])
        env["GIT_EXEC_PATH"] = str(PG / "mingw64" / "bin")
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, env=env_for_git(), **kw)


def try_git_push() -> bool:
    """Fast path. Returns True on success."""
    if not GIT:
        print("[publish] git not found, skipping fast path")
        return False
    remote = run([GIT, "remote", "get-url", "origin"])
    if remote.returncode != 0:
        print("[publish] no origin remote, skipping fast path")
        return False

    print("[publish] trying git push ...")
    res = run([GIT, "push", "-u", "origin", BRANCH, "--no-verify"], cwd=str(ROOT),
              timeout=120)
    if res.returncode == 0:
        print("[publish] git push OK")
        return True
    err = (res.stderr or "") + (res.stdout or "")
    print("[publish] git push failed:")
    for line in err.strip().splitlines()[:6]:
        print("   " + line)
    return False


def gh_api(args, data=None):
    """Call gh api. `data` is passed on stdin to avoid argv length limits."""
    cmd = [GH, "api"] + args
    if data is not None:
        cmd += ["--input", "-"]
    return subprocess.run(cmd, input=data, capture_output=True, text=True)


def remote_sha(path: str):
    """Current blob sha of a file on the remote branch, or None."""
    res = gh_api([f"repos/{OWNER}/{REPO}/contents/{path}",
                  "-X", "GET", "-f", f"ref={BRANCH}", "--jq", ".sha"])
    if res.returncode != 0:
        return None
    sha = res.stdout.strip().strip('"')
    return sha if sha and sha != "null" else None


def publish_via_api(message: str, files: list[str]) -> int:
    print(f"[publish] falling back to Contents API ({len(files)} file(s))")
    failures = []

    for rel in files:
        path = ROOT / rel
        if not path.exists():
            print(f"   - skip {rel} (missing locally)")
            continue

        blob = path.read_bytes()
        import base64
        body = {
            "message": message,
            "content": base64.b64encode(blob).decode("ascii"),
            "branch": BRANCH,
            "committer": {"name": "fatekingcsr",
                          "email": "fatekingcsr@users.noreply.github.com"},
            "author": {"name": "fatekingcsr",
                       "email": "fatekingcsr@users.noreply.github.com"},
        }
        existing = remote_sha(rel)
        if existing:
            body["sha"] = existing

        res = gh_api([f"repos/{OWNER}/{REPO}/contents/{rel}", "-X", "PUT"],
                     data=json.dumps(body))
        if res.returncode == 0:
            print(f"   ✓ {rel}")
        else:
            print(f"   ✗ {rel}: {(res.stderr or res.stdout).strip()[:160]}")
            failures.append(rel)

    return len(failures)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-m", "--message", default="chore: publish repo")
    ap.add_argument("--init", action="store_true",
                    help="upload all tracked files regardless of local git state")
    args = ap.parse_args()

    if not GH:
        print("gh not found; cannot publish")
        return 1

    if not args.init and try_git_push():
        return 0

    files = TRACKED if args.init else changed_files()
    if not files:
        print("[publish] nothing to publish")
        return 0

    bad = publish_via_api(args.message, files)
    if bad:
        print(f"\n[publish] {bad} file(s) failed")
        return 1
    print("\n[publish] done. Pages 需 1~3 分钟重新构建。")
    return 0


def changed_files() -> list[str]:
    """Files whose local bytes differ from the remote branch.

    We compare against the remote blob sha rather than local git status,
    because API-made commits leave the local repo unaware of the difference
    and a clean working tree would otherwise look like "nothing to do".
    """
    out = []
    for rel in TRACKED:
        path = ROOT / rel
        if not path.exists():
            continue
        import base64
        import hashlib as _h

        local = path.read_bytes()
        # git blob sha1 = sha1("blob <len>\0" + content)
        header = f"blob {len(local)}\0".encode()
        local_sha = _h.sha1(header + local).hexdigest()
        if local_sha != remote_sha(rel):
            out.append(rel)
    return out


if __name__ == "__main__":
    sys.exit(main())
