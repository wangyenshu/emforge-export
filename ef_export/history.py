"""Older revisions of recipes, taken from the git history of the recipes repo.

emscripten-forge's channel holds every package version that was ever built,
and its variant file pins many dependencies to versions that are no longer
in the recipe tree (e.g. ``python 3.13.*`` while recipes/python is already
3.14).  A from-source build has no channel, so when the current recipe does
not satisfy a requirement, the newest revision of that recipe directory that
does is taken from git history instead.

Only git is needed (no network if the clone has blobs; a blobless
``--filter=blob:none`` clone fetches the few blobs it needs on demand).
"""
import hashlib
import os
import shutil
import subprocess
import tarfile
import tempfile


def _git(root, *args, binary=False):
    out = subprocess.check_output(["git", "-C", root] + list(args), stderr=subprocess.DEVNULL)
    return out if binary else out.decode("utf-8", "replace")


class RecipeHistory:
    def __init__(self, root, cache_dir=None):
        self.root = root
        self.ok = False
        self.reason = ""
        self._commits = {}
        try:
            top = _git(root, "rev-parse", "--show-toplevel").strip()
            shallow = _git(root, "rev-parse", "--is-shallow-repository").strip()
        except (OSError, subprocess.CalledProcessError):
            self.reason = "%s is not a git checkout" % root
            return
        if os.path.realpath(top) != os.path.realpath(root):
            self.reason = "%s is not the top of its git checkout" % root
            return
        if shallow == "true":
            self.reason = ("%s is a shallow clone; run 'git -C %s fetch --unshallow "
                           "--filter=blob:none' to enable recipe history" % (root, root))
            return
        self.head = _git(root, "rev-parse", "HEAD").strip()
        base = cache_dir or os.path.join(
            os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"),
            "emforge-export", "history")
        self.cache = os.path.join(base, hashlib.sha256(
            os.path.realpath(root).encode()).hexdigest()[:12])
        self.ok = True

    def commits(self, rel_dir):
        """Commits that changed <rel_dir>/recipe.yaml, newest first, starting
        below HEAD (HEAD's version is the current recipe)."""
        if rel_dir not in self._commits:
            out = _git(self.root, "log", "--format=%H %cs", self.head, "--",
                       rel_dir + "/recipe.yaml")
            res = []
            for line in out.splitlines():
                c, _, date = line.partition(" ")
                if c:
                    res.append((c, date))
            self._commits[rel_dir] = res
        return self._commits[rel_dir]

    def recipe_text(self, commit, rel_dir):
        try:
            return _git(self.root, "show", "%s:%s/recipe.yaml" % (commit, rel_dir))
        except subprocess.CalledProcessError:
            return None

    def materialize(self, commit, rel_dir):
        """Check out <rel_dir> as of <commit> into the cache; returns the path."""
        dest = os.path.join(self.cache, commit[:12], rel_dir)
        marker = dest + ".ef-complete"
        if os.path.isfile(marker) and os.path.isdir(dest):
            return dest
        tmp = tempfile.mkdtemp(prefix="ef-hist-", dir=os.path.dirname(self.cache)
                               if os.path.isdir(os.path.dirname(self.cache)) else None)
        try:
            data = _git(self.root, "archive", "--format=tar", commit, rel_dir, binary=True)
            tar_path = os.path.join(tmp, "a.tar")
            with open(tar_path, "wb") as f:
                f.write(data)
            with tarfile.open(tar_path) as tf:
                kw = {"filter": "tar"} if hasattr(tarfile, "data_filter") else {}
                tf.extractall(os.path.join(tmp, "x"), **kw)
            if os.path.isdir(dest):
                shutil.rmtree(dest)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.move(os.path.join(tmp, "x", rel_dir), dest)
            open(marker, "w").close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        return dest
