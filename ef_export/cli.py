"""Command line interface of emforge-export."""
import argparse
import os
import subprocess
import sys

from . import __version__
from .generate import Generator, recipes_rev
from .history import RecipeHistory
from .recipe import RecipeError, current_build_platform
from .resolve import Resolver
from .variant import load_variant_files

DEFAULT_REPO = "https://github.com/emscripten-forge/recipes.git"


def parse_overrides(items):
    out = {}
    for it in items or []:
        if "=" not in it:
            raise SystemExit("--variant expects KEY=VALUE, got %r" % it)
        k, v = it.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def ensure_repo(path, clone):
    if os.path.isdir(os.path.join(path, "recipes", "recipes_emscripten")):
        return path
    if not clone:
        raise SystemExit(
            "%s is not an emscripten-forge/recipes checkout (missing recipes/recipes_emscripten).\n"
            "Pass --recipes PATH or --clone to fetch %s" % (path, DEFAULT_REPO))
    sys.stderr.write("cloning %s into %s\n" % (DEFAULT_REPO, path))
    # full history (older recipe revisions are used for pinned versions),
    # file contents fetched on demand
    subprocess.check_call(["git", "clone", "--filter=blob:none", DEFAULT_REPO, path])
    return path


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="ef-export",
        description="Export emscripten-forge recipes (and every recipe they depend on) "
                    "to one self-contained build.sh that needs no conda / rattler-build / "
                    "prefix.dev infrastructure.")
    ap.add_argument("packages", nargs="*", help="package (output) names to export")
    ap.add_argument("-r", "--recipes", default="recipes",
                    help="path to an emscripten-forge/recipes checkout (default: ./recipes)")
    ap.add_argument("--clone", action="store_true",
                    help="clone emscripten-forge/recipes into --recipes if it does not exist")
    ap.add_argument("-o", "--output", help="output script (default: build-<pkg>.sh)")
    ap.add_argument("-m", "--variant-config", action="append",
                    help="variant config file(s) (default: <recipes>/variant.yaml)")
    ap.add_argument("--variant", action="append", metavar="KEY=VALUE",
                    help="override a variant value, e.g. --variant python=3.14")
    ap.add_argument("--build-platform", default=None,
                    help="platform the script will run on (default: this machine)")
    ap.add_argument("--target-platform", default="emscripten-wasm32")
    ap.add_argument("--skip", action="append", metavar="NAME",
                    help="do not build this dependency (assume it is provided)")
    ap.add_argument("--strict-versions", action="store_true",
                    help="fail when a recipe's version does not satisfy a requirement")
    ap.add_argument("--no-history", action="store_true",
                    help="only use the checked-out recipes, never older revisions from git "
                         "history (versions that do not satisfy a pin are then only warned about)")
    ap.add_argument("--list", action="store_true", help="print the build plan and exit")
    ap.add_argument("--explain", action="store_true",
                    help="print how every requirement was resolved")
    ap.add_argument("--version", action="version", version="%(prog)s " + __version__)
    a = ap.parse_args(argv)
    if not a.packages:
        ap.error("give at least one package name")

    root = ensure_repo(os.path.abspath(a.recipes), a.clone)
    vfiles = a.variant_config or [os.path.join(root, "variant.yaml")]
    overrides = parse_overrides(a.variant)
    bp = a.build_platform or current_build_platform()
    vt, _, _ = load_variant_files(vfiles, a.target_platform, bp, overrides)
    vn, _, _ = load_variant_files(vfiles, bp, bp, overrides)
    hist = None if a.no_history else RecipeHistory(root)
    if hist is not None and not hist.ok and a.clone and "shallow" in hist.reason:
        # --clone allows network access: fetch the history (commits and
        # trees only; file contents are fetched when needed)
        sys.stderr.write("fetching the history of %s ...\n" % root)
        if subprocess.call(["git", "-C", root, "fetch", "-q", "--unshallow", "--filter=blob:none"]) == 0:
            hist = RecipeHistory(root)
    if hist is not None and not hist.ok:
        sys.stderr.write("note: recipe history unavailable: %s\n" % hist.reason)
    res = Resolver(root, vt, vn, bp, a.target_platform, strict_versions=a.strict_versions,
                   skip=a.skip, history=hist)
    sys.stderr.write("indexing recipes in %s ...\n" % root)
    res.build_index()
    try:
        roots, order = res.resolve(a.packages)
    except RecipeError as e:
        raise SystemExit("error: %s" % e)

    if a.list or a.explain:
        for i, u in enumerate(order, 1):
            kind = "native" if u.prov.kind == "native" else "wasm"
            if u.test_only:
                kind += ", tests only"
            where = getattr(u.recipe, "payload_rel", None) or os.path.relpath(u.recipe.recipe_dir, root)
            print("%3d. %-48s [%s] %s" % (i, u.label, kind, where))
            if a.explain:
                if u.staging:
                    print("       inherits: %s" % u.staging.label)
                for x in u.build_units:
                    print("       build:  %s (recipe)" % x.label)
                for n, s in u.tools:
                    print("       build:  %s %s (native tool)" % (n, s or ""))
                for n, s in u.pip_build:
                    print("       build:  %s %s (pip)" % (n, s or ""))
                for x in u.host_units:
                    print("       host:   %s" % x.label)
                for n, s in u.pip_host:
                    print("       host:   %s %s (PyPI, pure Python)" % (n, s or ""))
                for x in u.run_units:
                    print("       run:    %s" % x.label)
                for x in u.test_build_units:
                    print("       test build: %s" % x.label)
                for n, s in u.test_tools:
                    print("       test build: %s %s (native tool)" % (n, s or ""))
                for x in u.test_run_units:
                    print("       test run:   %s" % x.label)
                for n, s in u.test_pip_run:
                    print("       test run:   %s %s (PyPI)" % (n, s or ""))
                for x in u.unresolved:
                    print("       UNRESOLVED: %s" % x)
                for nt in u.notes:
                    print("       note: %s" % nt)
        for w in res.warnings:
            if any(x.label in w for x in order):
                sys.stderr.write("warning: %s\n" % w)
        return 0

    out = a.output or "build-%s.sh" % "-".join(a.packages)
    gen = Generator(res, roots, order, {"recipes_rev": recipes_rev(root),
                                         "output_name": os.path.basename(out)})
    text = gen.render()
    # write a new file and rename it over the old one: bash reads a running
    # script as it goes, so rewriting it in place would break a build that
    # is using it right now
    tmp = "%s.tmp-%d" % (out, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, 0o755)
    os.replace(tmp, out)
    for w in res.warnings:
        if any(x.label in w for x in order):
            sys.stderr.write("warning: %s\n" % w)
    sys.stderr.write("wrote %s (%d packages, %.1f MB)\n" % (out, len(order), len(text) / 1e6))
    return 0


if __name__ == "__main__":
    sys.exit(main())
