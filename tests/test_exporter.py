#!/usr/bin/env python3
"""Tests for emforge-export.  Run:  python3 -m unittest discover -s tests -v

The end-to-end tests build synthetic recipe trees with the generated build.sh
(offline, no compiler needed) and need bash, git and python3 with venv.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from ef_export import matchspec  # noqa: E402
from ef_export.recipe import render_recipe  # noqa: E402
from ef_export.generate import _payload_block  # noqa: E402

EXPORT = [sys.executable, os.path.join(ROOT, "ef-export")]


def load_helper_namespace():
    """The runtime helper as the generated script embeds it."""
    with open(os.path.join(ROOT, "ef_export", "runtime", "helper.py")) as f:
        src = f.read()
    with open(os.path.join(ROOT, "ef_export", "matchspec.py")) as f:
        ms = f.read()
    ns = {"__name__": "ef_helper"}
    exec(compile(src.replace("#@@MATCHSPEC@@", ms.split('"""', 2)[2]), "helper", "exec"), ns)
    return ns


class MatchSpecTest(unittest.TestCase):
    def test_split(self):
        self.assertEqual(matchspec.split_spec("cmake >=3.20"), ("cmake", ">=3.20", None))
        self.assertEqual(matchspec.split_spec("python 3.13.* *_cp313"), ("python", "3.13.*", "*_cp313"))
        self.assertEqual(matchspec.split_spec("openssl <4.0.0a0"), ("openssl", "<4.0.0a0", None))
        self.assertEqual(matchspec.split_spec("make"), ("make", None, None))
        self.assertEqual(matchspec.split_spec("qt6-main[build=jspi*]"), ("qt6-main", None, "jspi*"))
        self.assertEqual(matchspec.split_spec("automake >= 1.16"), ("automake", ">=1.16", None))

    def test_versions(self):
        vm = matchspec.version_matches
        self.assertTrue(vm("3.13.1", "3.13.*"))
        self.assertFalse(vm("3.14.3", "3.13.*"))
        self.assertTrue(vm("3.6.2", "<4.0.0a0"))
        self.assertFalse(vm("4.0.3", "<4.0.0a0"))
        self.assertTrue(vm("21.0.12.1", "21.*"))
        self.assertTrue(vm("1.16.5", "<1.17"))
        self.assertTrue(vm("2.5", ">=2,<3|>=4"))
        self.assertTrue(vm("4.1", ">=2,<3|>=4"))
        self.assertFalse(vm("3.5", ">=2,<3|>=4"))
        self.assertTrue(vm("3.5.3", "3.5.3"))
        self.assertTrue(vm("1.0", "!=1.1"))
        self.assertTrue(vm("4.0.9", "=4.0.9"))
        self.assertTrue(vm("2.0rc1", "<2.0"))
        self.assertTrue(vm("1.2.post1", ">1.2"))

    def test_pip(self):
        self.assertEqual(matchspec.conda_to_pip("cmake", "4.3"), "cmake==4.3.*")
        self.assertEqual(matchspec.conda_to_pip("pip", "25.*"), "pip==25.*")
        self.assertEqual(matchspec.conda_to_pip("crossenv", ">=1.2"), "crossenv>=1.2")
        self.assertEqual(matchspec.conda_to_pip("x", ">=1,<2"), "x>=1,<2")


class GlobTest(unittest.TestCase):
    def setUp(self):
        self.h = load_helper_namespace()

    def test_files_glob(self):
        fg = self.h["files_glob"]
        self.assertTrue(fg("include/").match("include/a/b.h"))
        self.assertTrue(fg("lib/libz.a").match("lib/libz.a"))
        self.assertTrue(fg("share/x11-wasm").match("share/x11-wasm/dom.js"))
        self.assertFalse(fg("lib/libz.a").match("lib/libz.a.b"))
        self.assertTrue(fg("**/*.pyc").match("lib/python3.13/site-packages/a/__pycache__/b.pyc"))
        self.assertTrue(fg("**/__pycache__/**").match("x/__pycache__/y.pyc"))
        self.assertTrue(fg("*.h").match("include/deep/x.h"))   # '*' crosses '/' (globset)

    def test_contents_bin_wasm(self):
        d = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(d, "info"))
            with open(os.path.join(d, "info", "files"), "w") as f:
                f.write("bin/xmlwf\nbin/xmlwf.wasm\nlib/libexpat.a\ninclude/expat.h\n")
            spec = os.path.join(d, "spec.json")
            json.dump({"bin": ["xmlwf.wasm", "xmlwf"], "lib": ["libexpat.a"],
                       "include": ["expat.h"]}, open(spec, "w"))
            self.h["cmd_test_contents"]([d, spec, "emscripten-wasm32", ""])
            json.dump({"lib": ["libnope.a"]}, open(spec, "w"))
            with self.assertRaises(SystemExit):
                self.h["cmd_test_contents"]([d, spec, "emscripten-wasm32", ""])
        finally:
            shutil.rmtree(d)


class PayloadTest(unittest.TestCase):
    def test_roundtrip(self):
        """Every kind of file comes back byte for byte."""
        import hashlib
        src = tempfile.mkdtemp()
        out = tempfile.mkdtemp()
        try:
            files = {
                "build.sh": b"#!/bin/bash\necho hi\n",
                "nonl.txt": b"no trailing newline",
                "empty": b"",
                "crlf.patch": b"--- a/x\r\n+++ b/x\r\n",
                "bin.dat": bytes(range(256)) * 3,
                "latin1.txt": b"caf\xe9\n",
                "tricky.txt": b"#@ef-end deadbeef\n#@ef-file x\n",
                "sub/dir/deep.yaml": b"a: 1\n",
            }
            for rel, data in files.items():
                p = os.path.join(src, rel)
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "wb") as f:
                    f.write(data)
            os.chmod(os.path.join(src, "build.sh"), 0o755)
            os.symlink("build.sh", os.path.join(src, "link.sh"))
            h = hashlib.sha256()
            blocks = [_payload_block(os.path.join(src, rel), "r/" + rel, h)
                      for rel in sorted(list(files) + ["link.sh"])]
            script = os.path.join(out, "s.sh")
            with open(script, "w", encoding="utf-8") as f:
                f.write("exit 0\n__EF_PAYLOAD_BEGIN__\n" + "\n".join(blocks) + "\n")
            with open(os.path.join(ROOT, "ef_export", "runtime", "engine.sh")) as f:
                engine = f.read()
            py = engine.split("<<'PYEOF' || ef_die \"cannot unpack", 1)[1].split("\n", 1)[1]
            py = py.split("\nPYEOF\n", 1)[0]
            dest = os.path.join(out, "x")
            os.makedirs(dest)
            subprocess.check_call([sys.executable, "-c", py, script, dest])
            for rel, data in files.items():
                with open(os.path.join(dest, "r", rel), "rb") as f:
                    self.assertEqual(f.read(), data, rel)
            self.assertTrue(os.access(os.path.join(dest, "r", "build.sh"), os.X_OK))
            self.assertEqual(os.readlink(os.path.join(dest, "r", "link.sh")), "build.sh")
        finally:
            shutil.rmtree(src)
            shutil.rmtree(out)


class RenderTest(unittest.TestCase):
    def render(self, text, platform="emscripten-wasm32", variant=None):
        d = tempfile.mkdtemp()
        try:
            with open(os.path.join(d, "recipe.yaml"), "w") as f:
                f.write(textwrap.dedent(text))
            with open(os.path.join(d, "build.sh"), "w") as f:
                f.write("echo\n")
            return render_recipe(d, platform, "linux-64", variant or {})
        finally:
            shutil.rmtree(d)

    def test_conditionals_and_context(self):
        r = self.render("""
            context:
              version: "1.10"
              major: ${{ version.split('.')[0] }}
            package:
              name: foo
              version: ${{ version }}
            source:
              url: https://example.org/foo-${{ version }}.tar.gz
              sha256: "00"
              patches:
                - if: emscripten
                  then: [a.patch]
                  else: [b.patch]
            requirements:
              build:
                - ${{ compiler('c') }}
                - if: linux
                  then: gcc-only
              host:
                - zlib
            """, variant={"c_compiler": "emscripten", "c_compiler_version": "4.0.9"})
        o = r.outputs[0]
        self.assertEqual(o["version"], "1.10")          # never a float
        self.assertEqual(o["sources"][0]["urls"], ["https://example.org/foo-1.10.tar.gz"])
        self.assertEqual(o["sources"][0]["patches"], ["a.patch"])
        self.assertEqual(o["requirements"]["build"], ["emscripten_emscripten-wasm32 =4.0.9"])
        self.assertEqual(o["build"]["script"]["type"], "file")

    def test_staging_and_hash(self):
        r = self.render("""
            context:
              version: 2.0
            recipe:
              name: multi
              version: ${{ version }}
            build:
              number: 3
            outputs:
              - staging:
                  name: multi-build
                build:
                  script: build.sh
              - package:
                  name: libmulti
                inherit: multi-build
                build:
                  string: h_${{ hash }}_x
              - package:
                  name: multi-dev
                requirements:
                  run:
                    - ${{ pin_subpackage('libmulti', exact=True) }}
            """)
        kinds = [(o["kind"], o["name"]) for o in r.outputs]
        self.assertEqual(kinds, [("staging", "multi-build"), ("package", "libmulti"),
                                 ("package", "multi-dev")])
        lib = r.outputs[1]
        self.assertTrue(lib["build_string"].startswith("h_h"))
        self.assertNotIn("EFHASH", lib["build_string"])
        self.assertEqual(lib["build"]["number"], 3)
        self.assertEqual(lib["inherit"]["from"], "multi-build")


def make_repo(base, recipes):
    """A git repo laid out like emscripten-forge/recipes."""
    for rel, files in recipes.items():
        d = os.path.join(base, "recipes", rel)
        os.makedirs(d, exist_ok=True)
        for name, text in files.items():
            p = os.path.join(d, name)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as f:
                f.write(textwrap.dedent(text))
    with open(os.path.join(base, "variant.yaml"), "w") as f:
        f.write("libbase:\n  - '1.0'\n")


def git(base, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@t")
    subprocess.check_call(["git", "-C", base] + list(args), env=env, stdout=subprocess.DEVNULL)


LIB_RECIPE = """
    package:
      name: libbase
      version: "{v}"
    source:
      path: src
    build:
      noarch: generic
      script:
        - mkdir -p $PREFIX/share/libbase
        - cp version.txt $PREFIX/share/libbase/version.txt
    tests:
      - package_contents:
          files:
            - share/libbase/version.txt
"""

APP_RECIPE = """
    package:
      name: app
      version: "0.1"
    source:
      - path: src
        patches:
          - fix.patch
    build:
      noarch: generic
    requirements:
      host:
        - libbase
      run:
        - libbase
    tests:
      - script:
          - test "$(cat $PREFIX/share/app/greeting)" = "hello, patched"
          - test -f $PREFIX/share/libbase/version.txt
"""

APP_BUILD = """
    #!/bin/bash
    set -euo pipefail
    mkdir -p "$PREFIX/share/app"
    cp greeting "$PREFIX/share/app/greeting"
    # built against libbase from the host prefix
    cp "$PREFIX/share/libbase/version.txt" "$PREFIX/share/app/built-against"
"""


SLOW_RECIPE = """
    package:
      name: slow
      version: "1"
    build:
      noarch: generic
      script:
        - echo started > $PREFIX/../../started
        - sleep {secs}
"""


class InterruptTest(unittest.TestCase):
    def test_term_stops_build(self):
        import signal
        import time
        tmp = tempfile.mkdtemp(prefix="ef-int-")
        try:
            repo = os.path.join(tmp, "recipes")
            secs = "317.%d" % os.getpid()      # unique: identifies this test's process
            make_repo(repo, {"recipes_emscripten/slow": {"recipe.yaml": SLOW_RECIPE.format(secs=secs)}})
            env = dict(os.environ, XDG_CACHE_HOME=os.path.join(tmp, "cache"), NO_COLOR="1")
            script = os.path.join(tmp, "b.sh")
            subprocess.check_call(EXPORT + ["-r", repo, "slow", "-o", script, "--no-history"], env=env,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            root = os.path.join(tmp, "root")
            p = subprocess.Popen(["bash", script, "-C", root, "--offline"], env=env,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            started = os.path.join(root, "started")
            for _ in range(600):
                if os.path.exists(started):
                    break
                time.sleep(0.1)
            self.assertTrue(os.path.exists(started), "build did not start")
            for _ in range(100):
                if "sleep " + secs in subprocess.run(["ps", "-eo", "args"], stdout=subprocess.PIPE,
                                                 text=True).stdout:
                    break
                time.sleep(0.1)
            p.send_signal(signal.SIGTERM)
            self.assertEqual(p.wait(timeout=30), 143)
            time.sleep(0.5)
            ps = subprocess.run(["ps", "-eo", "args"], stdout=subprocess.PIPE, text=True).stdout
            self.assertNotIn(os.path.join(root, "bld"), ps)
            self.assertNotIn("sleep " + secs, ps)
            self.assertFalse(os.path.exists(os.path.join(root, ".lock")))
        finally:
            subprocess.run(["pkill", "-f", tmp])
            subprocess.run(["pkill", "-f", "sleep 317.%d" % os.getpid()])
            shutil.rmtree(tmp, ignore_errors=True)


MULTI_RECIPE = """
    context:
      version: "1.0"
    recipe:
      name: multi
      version: ${{ version }}
    build:
      noarch: generic
    outputs:
      - staging:
          name: multi-cache
        build:
          script:
            - mkdir -p $PREFIX/lib $PREFIX/include
            - echo lib > $PREFIX/lib/libmulti.a
            - echo hdr > $PREFIX/include/multi.h
            - echo from-staging > staged.txt
      - package:
          name: libmulti
        inherit: multi-cache
        build:
          files:
            include:
              - lib/**
          script:
            - test -f staged.txt
            # modifies a (hard-linked) file of the staging cache in place
            - echo more >> staged.txt
      - package:
          name: multi-dev
        inherit: multi-cache
        build:
          files:
            include:
              - include/**
          script:
            - test "$(cat staged.txt)" = from-staging
        requirements:
          run:
            - ${{ pin_subpackage('libmulti', exact=True) }}
      # does nothing to the work dir: with --link-staging it must not cause
      # another rebuild (the engine itself must never write through a link)
      - package:
          name: multi-doc
        inherit: multi-cache
        build:
          files:
            include:
              - include/**
          script:
            - test "$(cat staged.txt)" = from-staging
"""

MERGED_RECIPE = """
    package:
      name: merged
      version: "1"
    build:
      noarch: generic
      merge_build_and_host_envs: true
      script:
        - mkdir -p $PREFIX/share/merged
        - echo hi > $PREFIX/share/merged/x
"""


class MultiOutputTest(unittest.TestCase):
    def build(self, *extra):
        tmp = tempfile.mkdtemp(prefix="ef-multi-")
        self.addCleanup(shutil.rmtree, tmp, True)
        repo = os.path.join(tmp, "recipes")
        make_repo(repo, {"recipes_emscripten/multi": {"recipe.yaml": MULTI_RECIPE},
                         "recipes_emscripten/merged": {"recipe.yaml": MERGED_RECIPE}})
        env = dict(os.environ, XDG_CACHE_HOME=os.path.join(tmp, "cache"), NO_COLOR="1")
        script = os.path.join(tmp, "b.sh")
        # libmulti (which edits a staged file in place) is built before multi-dev
        subprocess.check_call(EXPORT + ["-r", repo, "libmulti", "multi-dev", "multi-doc", "merged", "-o", script,
                                        "--no-history"], env=env, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL)
        root = os.path.join(tmp, "root")
        run = subprocess.run(["bash", script, "-C", root, "--offline"] + list(extra), env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.assertEqual(run.returncode, 0, run.stdout[-4000:])

        def files(prefix):
            d = [x for x in os.listdir(os.path.join(root, "pkgs")) if x.startswith(prefix)][0]
            with open(os.path.join(root, "pkgs", d, "info", "files")) as f:
                return f.read().split()
        self.assertEqual(files("libmulti-"), ["lib/libmulti.a"])
        self.assertEqual(files("multi-dev-"), ["include/multi.h"])
        self.assertEqual(files("merged-"), ["share/merged/x"])
        # multi-dev's run dependency (pin_subpackage) is installed with it
        self.assertTrue(os.path.isfile(os.path.join(root, "prefix", "lib", "libmulti.a")))
        return run.stdout

    def test_staging_copy(self):
        """Default: every output gets its own copy of the staging work dir,
        so libmulti's in-place edit never reaches multi-dev."""
        out = self.build()
        self.assertNotIn("changed files of staging cache", out)
        self.assertEqual(out.count("building multi--staging-multi-cache"), 1, out[-3000:])

    def test_staging_links(self):
        """--link-staging: the in-place edit is detected and the cache is
        rebuilt before multi-dev uses it."""
        out = self.build("--link-staging")
        # libmulti's edit, and nothing else, invalidates the cache once
        self.assertEqual(out.count("changed files of staging cache"), 1, out[-3000:])
        self.assertIn("script of libmulti", out)
        self.assertEqual(out.count("building multi--staging-multi-cache"), 2, out[-3000:])


class EndToEndTest(unittest.TestCase):
    """Builds a two-package tree with the generated script, offline."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="ef-e2e-")
        cls.repo = os.path.join(cls.tmp, "recipes")
        os.makedirs(cls.repo)
        # libbase 1.0 (pinned by variant.yaml) only exists in git history
        make_repo(cls.repo, {
            "recipes_emscripten/libbase": {"recipe.yaml": LIB_RECIPE.format(v="1.0"),
                                           "src/version.txt": "1.0\n"},
            "recipes_emscripten/app": {"recipe.yaml": APP_RECIPE, "build.sh": APP_BUILD,
                                       "src/greeting": "hello, world\n",
                                       "fix.patch": "--- a/greeting\n+++ b/greeting\n@@ -1 +1 @@\n"
                                                    "-hello, world\n+hello, patched\n"},
        })
        git(cls.repo, "init", "-q")
        git(cls.repo, "add", "-A")
        git(cls.repo, "commit", "-q", "-m", "libbase 1.0")
        make_repo(cls.repo, {"recipes_emscripten/libbase": {
            "recipe.yaml": LIB_RECIPE.format(v="2.0"), "src/version.txt": "2.0\n"}})
        git(cls.repo, "commit", "-q", "-am", "libbase 2.0")
        cls.cache = os.path.join(cls.tmp, "cache")
        cls.env = dict(os.environ, XDG_CACHE_HOME=cls.cache, NO_COLOR="1")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def export(self, *args):
        return subprocess.run(EXPORT + ["-r", self.repo] + list(args), env=self.env,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def test_plan_uses_history_for_pin(self):
        r = self.export("app", "--list")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("libbase 1.0 [recipe from git", r.stdout)
        r = self.export("app", "--list", "--no-history")
        self.assertIn("libbase 2.0", r.stdout)
        self.assertIn("requires libbase 1.0", r.stdout)

    def test_build_and_rerun(self):
        script = os.path.join(self.tmp, "build-app.sh")
        r = self.export("app", "-o", script)
        self.assertEqual(r.returncode, 0, r.stdout)
        with open(script) as f:
            text = f.read()
        # the whole script, embedded recipe files included, parses
        self.assertEqual(subprocess.run(["bash", "-n", script]).returncode, 0)
        self.assertIn("#@ef-file", text)
        self.assertIn("hello, patched", text)            # the patch is readable
        root = os.path.join(self.tmp, "root")
        run = subprocess.run(["bash", script, "-C", root, "--offline", "--tests"],
                             env=self.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.assertEqual(run.returncode, 0, run.stdout[-4000:])
        prefix = os.path.join(root, "prefix")
        with open(os.path.join(prefix, "share", "app", "greeting")) as f:
            self.assertEqual(f.read(), "hello, patched\n")
        with open(os.path.join(prefix, "share", "app", "built-against")) as f:
            self.assertEqual(f.read(), "1.0\n")
        self.assertIn("script test 0 of app", run.stdout)
        metas = os.listdir(os.path.join(prefix, "conda-meta"))
        self.assertEqual(sorted(m.split("-")[0] for m in metas), ["app", "libbase"])
        # second run: nothing to rebuild
        run2 = subprocess.run(["bash", script, "-C", root, "--offline"], env=self.env,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.assertEqual(run2.returncode, 0, run2.stdout[-3000:])
        self.assertEqual(run2.stdout.count("is up to date"), 2, run2.stdout)
        # the recipes can be extracted without building
        ext = os.path.join(self.tmp, "extracted")
        subprocess.check_call(["bash", script, "--extract", ext], env=self.env,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.assertTrue(os.path.isfile(os.path.join(ext, "recipes", "recipes_emscripten", "app",
                                                    "fix.patch")))

    def test_conda_channel(self):
        script = os.path.join(self.tmp, "build-app2.sh")
        self.assertEqual(self.export("app", "-o", script).returncode, 0)
        root = os.path.join(self.tmp, "root2")
        run = subprocess.run(["bash", script, "-C", root, "--offline", "--conda-pkgs"], env=self.env,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.assertEqual(run.returncode, 0, run.stdout[-3000:])
        with open(os.path.join(root, "channel", "noarch", "repodata.json")) as f:
            rd = json.load(f)
        self.assertEqual(sorted(p["name"] for p in rd["packages"].values()), ["app", "libbase"])


if __name__ == "__main__":
    unittest.main()
