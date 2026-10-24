"""Emit a self-contained build.sh for a resolved build plan."""
import base64
import hashlib
import json
import os
import subprocess
import time

from . import tools, __version__
from .recipe import normalize_key

HERE = os.path.dirname(os.path.abspath(__file__))
LANGUAGE_KEYS = {"PERL", "LUA", "R", "NUMPY", "PYTHON", "RUBY", "NODEJS"}


def q(s):
    """single-quote for bash"""
    s = "" if s is None else str(s)
    return "'" + s.replace("'", "'\\''") + "'"


def arr(items):
    return "(" + " ".join(q(i) for i in items) + ")"


def _recipe_rel(rendered):
    """Path of a recipe directory inside the embedded payload."""
    rel = getattr(rendered, "payload_rel", None)
    if not rel or rel.startswith(".."):
        rel = "extra/" + os.path.basename(rendered.recipe_dir.rstrip("/"))
    return rel


def _dir_digest(path):
    h = hashlib.sha256()
    for root, dirs, files in sorted(os.walk(path)):
        dirs.sort()
        for f in sorted(files):
            p = os.path.join(root, f)
            h.update(os.path.relpath(p, path).encode())
            try:
                with open(p, "rb") as fh:
                    h.update(hashlib.sha256(fh.read()).digest())
            except OSError:
                pass
    return h.hexdigest()


def _strip_tokens(spec):
    return spec.replace(" @@EF:EXACT@@", "").replace("@@EF:EXACT@@", "").strip()


class Generator:
    def __init__(self, resolver, roots, order, options):
        self.r = resolver
        self.roots = roots
        self.order = order
        self.opt = options
        self.num = {u.key: i + 1 for i, u in enumerate(order)}
        self.input_hash = {}
        self.recipe_dirs = {}
        self.needed_tools = set()

    # ------------------------------------------------------------------ units
    def build_install_set(self, u):
        return self.r.install_set(u.build_units)

    def host_install_set(self, u):
        return self.r.install_set(u.host_units)

    def unit_tools(self, u):
        """tools of the unit + run tools of everything installed in BUILD_PREFIX"""
        tl = list(u.tools)
        pipl = list(u.pip_build)
        py_specs = []
        for b in self.build_install_set(u):
            for t in b.run_tools:
                if t not in tl:
                    tl.append(t)
            for p in b.run_pip:
                if p not in pipl:
                    pipl.append(p)
        out = []
        for name, spec in tl:
            if name == "python":
                if spec and not spec.startswith("~"):
                    py_specs.append(spec)
                continue
            out.append((name, spec or ""))
        has_patches = any(s.get("patches") for s in u.out["sources"])
        if has_patches and not any(n == "patch" for n, _ in out):
            out.append(("patch", ""))
        if any(s["kind"] == "git" for s in u.out["sources"]) and not any(n == "git" for n, _ in out):
            out.append(("git", ""))
        # duplicate tool names with different specs: keep the most specific
        merged = {}
        order = []
        for n, s in out:
            if n not in merged:
                merged[n] = s
                order.append(n)
            elif s and not merged[n]:
                merged[n] = s
            elif s and merged[n] and s != merged[n]:
                merged[n] = merged[n] + "," + s
        return [(n, merged[n]) for n in order], pipl, py_specs

    def run_specs(self, u):
        reqs = u.out["requirements"]
        specs = [_strip_tokens(s) for s in reqs["run"]]
        for hu in u.host_units:
            rex = hu.out["requirements"]["run_exports"]
            for s in rex["weak"] + rex["strong"]:
                specs.append(_strip_tokens(s))
        for bu in u.build_units:
            for s in bu.out["requirements"]["run_exports"]["strong"]:
                specs.append(_strip_tokens(s))
        seen, out = set(), []
        for s in specs:
            if s and s not in seen and not s.startswith("__"):
                seen.add(s)
                out.append(s)
        return out

    def compute_hashes(self):
        for u in self.order:
            h = hashlib.sha256()
            h.update(__version__.encode())
            blob = dict(u.out)
            h.update(json.dumps(blob, sort_keys=True, default=str).encode())
            rd = u.recipe.recipe_dir
            if rd not in self.recipe_dirs:
                self.recipe_dirs[rd] = _dir_digest(rd)
            h.update(self.recipe_dirs[rd].encode())
            for d in ([u.staging] if u.staging else []) + self.build_install_set(u) + \
                    self.host_install_set(u):
                h.update(self.input_hash[d.key].encode())
            self.input_hash[u.key] = h.hexdigest()[:24]

    def emit_unit(self, u):
        n = self.num[u.key]
        o = u.out
        L = []
        rel = _recipe_rel(u.recipe)
        outs = [x for x in u.recipe.outputs]
        which = ""
        if len(outs) > 1:
            which = ", output %d of %d" % (outs.index(o) + 1, len(outs))
        L.append("# " + "-" * 75)
        L.append("# [%d] %s  (%s%s)" % (n, u.label, rel, which))
        for note in u.notes:
            L.append("#     note: %s" % note)
        L.append("# " + "-" * 75)
        L.append("ef_u%d() {" % n)
        target = "native" if u.prov.kind == "native" else u.platform
        tl, pipl, py_specs = self.unit_tools(u)
        for name, _ in tl:
            self.needed_tools.add(name)
        build_pkgs = [x.ident for x in self.build_install_set(u)]
        host_pkgs = [x.ident for x in self.host_install_set(u)]
        run_pkgs = [x.ident for x in self.r.run_closure(u)]
        py_ver = u.python_version
        if py_ver:
            py_ver = ".".join(str(py_ver).split(".")[:2])
        pref = str(self.r.vn.get("python", "")).split(" ")[0].replace("*", "").rstrip(".")
        pref = ".".join(pref.split(".")[:2]) if pref else ""
        if u.python_version and u.prov.kind == "target":
            # a cross-python build wants the build python to match the host one
            pref = py_ver or pref
        build = o["build"]
        files = build.get("files") or {"include": [], "exclude": []}
        variant_env = []
        for k, v in sorted(o["variant_used"].items()):
            nk = normalize_key(k)
            if nk.upper() in LANGUAGE_KEYS or isinstance(v, bool) or v is None:
                continue
            variant_env.append("%s=%s" % (nk, v))
        script = build.get("script") or {"type": "none"}
        stype = script.get("type")
        if stype == "file":
            kind, sfile = "file", script["path"]
        elif stype == "commands":
            kind, sfile = "inline", ""
        else:
            kind, sfile = "none", ""
        tests = o.get("tests") or []
        about = o.get("about") or {}
        lic = about.get("license") if isinstance(about.get("license"), str) else ""
        lic_files = [x for x in (about.get("license_file") or []) if x]
        run_exports = o["requirements"]["run_exports"]
        rex = {k: [_strip_tokens(x) for x in v] for k, v in run_exports.items() if v}
        test_pkgs = []
        for x in self.r.install_set(u.test_run_units) + self.r.run_closure(u):
            if x.ident not in test_pkgs and x.key != u.key:
                test_pkgs.append(x.ident)
        test_build_pkgs = [x.ident for x in self.r.install_set(u.test_build_units)]
        test_tools = []
        for name, spec in u.test_tools:
            if name != "python" and (name, spec or "") not in test_tools:
                test_tools.append((name, spec or ""))
        for b in self.r.install_set(u.test_build_units):
            for name, spec in b.run_tools:
                if name != "python" and (name, spec or "") not in test_tools:
                    test_tools.append((name, spec or ""))
        for name, _ in test_tools:
            self.needed_tools.add(name)
        test_pip_build = list(u.test_pip_build)
        for b in self.r.install_set(u.test_build_units):
            for p in b.run_pip:
                if p not in test_pip_build:
                    test_pip_build.append(p)
        put = L.append
        put("  U_IDENT=%s; U_KIND=%s" % (q(u.ident), o["kind"]))
        put("  U_NAME=%s; U_VERSION=%s; U_BUILDNUM=%s; U_BUILDSTR=%s; U_HASH=%s" % (
            q(o["name"]), q(o["version"]), q(build.get("number") or 0), q(o["build_string"]),
            q(o["hash"])))
        put("  U_RECIPE=%s; U_TARGET=%s; U_NOARCH=%s" % (q(rel), q(target), q(build.get("noarch") or "")))
        put("  U_STAGING=%s" % q(u.staging.ident if u.staging else ""))
        put("  U_BUILD_PKGS=%s" % arr(build_pkgs))
        put("  U_HOST_PKGS=%s" % arr(host_pkgs))
        put("  U_RUN_PKGS=%s" % arr(run_pkgs))
        put("  U_TEST_PKGS=%s" % arr(test_pkgs))
        put("  U_TEST_BUILD_PKGS=%s" % arr(test_build_pkgs))
        put("  U_TEST_TOOLS=%s" % arr(["%s|%s" % (a, b) for a, b in test_tools]))
        put("  U_TEST_PIP_BUILD=%s" % arr(["%s|%s" % (a, b or "") for a, b in test_pip_build]))
        put("  U_TEST_PIP_RUN=%s" % arr(["%s|%s" % (a, b or "") for a, b in u.test_pip_run]))
        put("  U_RUN_PIP=%s" % arr(["%s|%s" % (a, b or "") for a, b in u.run_pip]
                                   if u.prov.kind == "target" else []))
        put("  U_TEST_ONLY=%d" % (1 if u.test_only else 0))
        put("  U_RUN_SPECS=%s" % arr(self.run_specs(u)))
        put("  U_CONSTRAINS=%s" % arr([_strip_tokens(x) for x in o["requirements"]["run_constraints"]]))
        put("  U_TOOLS=%s" % arr(["%s|%s" % (a, b) for a, b in tl]))
        put("  U_PIP_BUILD=%s" % arr(["%s|%s" % (a, b or "") for a, b in pipl]))
        put("  U_PIP_HOST=%s" % arr(["%s|%s" % (a, b or "") for a, b in u.pip_host]))
        put("  U_PY_VER=%s; U_PY_SPEC=%s; U_PY_PREF=%s" % (q(py_ver or ""), q(",".join(py_specs)), q(pref)))
        put("  U_FILES_INCLUDE=%s" % arr(files.get("include") or []))
        put("  U_FILES_EXCLUDE=%s" % arr(files.get("exclude") or []))
        put("  U_ALWAYS_INCLUDE=%s" % arr(build.get("always_include_files") or []))
        put("  U_MERGE_ENVS=%d" % (1 if build.get("merge_build_and_host_envs") else 0))
        put("  U_VARIANT=%s" % arr(variant_env))
        put("  U_SCRIPT=%s; U_SCRIPT_FILE=%s; U_INTERP=%s; U_CWD=%s" % (
            kind, q(sfile), q(script.get("interpreter") or ""), q(script.get("cwd") or "")))
        put("  U_SCRIPT_ENV=%s" % arr(["%s=%s" % (k, v) for k, v in (script.get("env") or {}).items()]))
        put("  U_LICENSE=%s" % q(lic or ""))
        put("  U_LICENSES=%s" % arr(lic_files))
        put("  U_TESTS_JSON=%s" % q(json.dumps(tests, separators=(",", ":"))))
        put("  U_ABOUT_JSON=%s" % q(json.dumps(about, default=str, separators=(",", ":"))))
        put("  U_RUN_EXPORTS_JSON=%s" % q(json.dumps(rex, separators=(",", ":"))))
        put("  U_NOTES=%s" % arr(u.notes))
        put("  U_INPUT_HASH=%s" % q(self.input_hash[u.key]))
        put("}")
        # sources
        put("ef_u%d_sources() {" % n)
        if not o["sources"]:
            put("  :")
        for s in o["sources"]:
            td = s.get("target_directory") or ""
            if s["kind"] == "url":
                put("  ef_src_url %s %s %s %s %s" % (q(td), q(s.get("file_name") or ""),
                                                   q(s.get("sha256") or ""), q(s.get("md5") or ""),
                                                   " ".join(q(x) for x in s["urls"])))
            elif s["kind"] == "git":
                rk, rv = s["rev"] if s["rev"] else ("", "")
                put("  ef_src_git %s %s %s %s %s %s %s %s" % (
                    q(td), q(s["url"]), q(rk), q(rv), q(s.get("depth") if s.get("depth") is not None else ""),
                    q("false" if s.get("submodules") is False else "true"),
                    q("true" if s.get("lfs") else "false"), q(s.get("expected_commit") or "")))
            else:
                put("  ef_src_path %s %s %s %s %s" % (q(td), q(s["path"]), q(s.get("file_name") or ""),
                                                    q(s.get("sha256") or ""), q(s.get("md5") or "")))
            for p in s.get("patches") or []:
                put("  ef_patch %s" % q(p))
        put("}")
        if kind == "inline":
            body = "\n".join(script.get("content") or [])
            delim = "EF_SCRIPT_EOF_%d" % n
            while delim in body:
                delim += "_"
            put("ef_u%d_script() {" % n)
            put("  cat <<'%s'" % delim)
            L.append(body)
            put(delim)
            put("}")
        return "\n".join(L)

    # ----------------------------------------------------------------- tools
    def emit_tools(self):
        names = set(self.needed_tools)
        # specials that bootstrap via other definitions
        names |= {"patch", "git", "automake", "pkg-config", "autoconf", "m4"}
        changed = True
        while changed:
            changed = False
            for name in list(names):
                t = tools.TOOLS.get(name)
                for s in (t["src"] if t else []):
                    for dep in tools.SOURCES[s].get("needs", []):
                        if dep not in names:
                            names.add(dep)
                            changed = True
        srcs = {}
        L = ["# " + "=" * 75, "# native tool registry (conda-forge build requirement -> provider)",
             "# " + "=" * 75]
        for name in sorted(names):
            t = tools.TOOLS.get(name)
            if not t:
                continue
            fn = "ef_tooldef_" + "".join(c if c.isalnum() else "_" for c in name)
            parts = []
            if t["exe"]:
                parts.append("T_EXE=%s" % arr(t["exe"]))
            parts.append("T_VER=%s" % q(t["ver"] or ""))
            if t["src"]:
                parts.append("T_SRC=%s" % arr(t["src"]))
                for s in t["src"]:
                    srcs[s] = tools.SOURCES[s]
            if t["pip"]:
                parts.append("T_PIP=%s" % q(t["pip"]))
            if t["data"]:
                parts.append("T_DATA=%s" % arr(t["data"]))
            if t["need"]:
                parts.append("T_NEED=%s" % arr(t["need"]))
            if t.get("pc"):
                parts.append("T_PC=%s" % arr(t["pc"]))
            if t.get("header"):
                parts.append("T_HEADER=%s" % q(t["header"]))
            if t["special"]:
                sp = t["special"].replace(":", "_").replace("-", "_")
                parts.append("T_SPECIAL=%s" % q(sp))
            if t["shim"]:
                parts.append("T_SHIM=%s" % q(t["shim"]))
            if t["optional"]:
                parts.append("T_OPTIONAL=1")
            for k, var in (("apt", "T_APT"), ("dnf", "T_DNF"), ("brew", "T_BREW"), ("pacman", "T_PACMAN")):
                if t["pkgs"].get(k):
                    parts.append("%s=%s" % (var, q(t["pkgs"][k])))
            L.append("%s() { %s; }" % (fn, "; ".join(parts)))
        # sources needed by deps of bootstraps (m4 for autoconf, ...)
        for name in sorted(names):
            t = tools.TOOLS.get(name)
            if t:
                for s in t["src"]:
                    srcs[s] = tools.SOURCES[s]
        for s in ("cups-2.4.11", "dejavu-fonts-2.37"):
            srcs[s] = tools.SOURCES[s]
        for s in tools.SOURCES:
            if s.startswith("python-"):
                srcs[s] = tools.SOURCES[s]
        L.append("")
        for sid in sorted(srcs):
            s = srcs[sid]
            fn = "ef_srcdef_" + "".join(c if c.isalnum() else "_" for c in sid)
            parts = ["S_URL=%s" % arr(tools.urls_of(s)), "S_SHA256=%s" % q(s["sha256"])]
            if s.get("needs"):
                parts.append("S_NEEDS=%s" % arr(s["needs"]))
            if s.get("configure"):
                parts.append("S_CONFIGURE=%s" % q(s["configure"]))
            if s.get("env"):
                parts.append("S_ENV=%s" % q(s["env"]))
            if s.get("build"):
                parts.append("S_BUILD=%s" % q(s["build"]))
            L.append("%s() { %s; }" % (fn, "; ".join(parts)))
        py_srcs = sorted([s for s in tools.SOURCES if s.startswith("python-")],
                         key=lambda x: [int(p) for p in x.split("-")[1].split(".")])
        L.append("EF_PYTHON_SRCS=%s" % arr(py_srcs))
        L.append("EF_CUPS_SRC='cups-2.4.11'; EF_DEJAVU_SRC='dejavu-fonts-2.37'")
        L.append("EF_NODE_VERSION=%s" % q(tools.NODE_VERSION))
        L.append("ef_node_sha() { case \"$1\" in %s esac; }" % " ".join(
            "%s) echo %s ;;" % (k, v) for k, v in sorted(tools.NODE_SHA256.items())))
        L.append("EF_PIP_WHEEL_URL=%s; EF_PIP_WHEEL_SHA256=%s" % (
            q(tools.PIP_WHEEL["url"]), q(tools.PIP_WHEEL["sha256"])))
        return "\n".join(L)

    # ---------------------------------------------------------------- payload
    def payload(self):
        """The recipe directories, as a readable archive of plain-text files.

        Every file becomes one block:

            #@ef-file <tag> <encoding> <mode> <path>
            <content>
            #@ef-end <tag>

        <encoding> is 'text' (UTF-8, newline added before the end line when
        the file lacks one: 'text-nonl'), 'base64' for binary files, or
        'symlink' (content = link target).  <tag> is unique per file and
        never occurs in its content, so no escaping is needed.  The block
        list follows the script's final 'exit', so bash never parses it.
        """
        dirs = {}
        for u in self.order:
            dirs.setdefault(u.recipe.recipe_dir, _recipe_rel(u.recipe))
        blocks = []
        h = hashlib.sha256()
        for rd, rel_dir in sorted(dirs.items(), key=lambda kv: kv[1]):
            for root, subdirs, files in os.walk(rd):
                subdirs[:] = sorted(d for d in subdirs if d not in (".git", "__pycache__"))
                for f in sorted(files):
                    full = os.path.join(root, f)
                    rel = os.path.join(rel_dir, os.path.relpath(full, rd))
                    blocks.append(_payload_block(full, rel, h))
        return "\n".join(blocks), h.hexdigest()

    # ------------------------------------------------------------------ write
    def render(self):
        self.compute_hashes()
        units_src = [self.emit_unit(u) for u in self.order]
        tools_src = self.emit_tools()
        payload, psha = self.payload()
        engine = _read(os.path.join(HERE, "runtime", "engine.sh"))
        helper = _read(os.path.join(HERE, "runtime", "helper.py"))
        ms = _read(os.path.join(HERE, "matchspec.py"))
        ms = ms.split('"""', 2)[2] if ms.startswith('"""') else ms
        helper = helper.replace("#@@MATCHSPEC@@", ms.strip())
        if "EF_HELPER_EOF" in helper:
            raise RuntimeError("helper contains heredoc delimiter")
        rev = self.opt.get("recipes_rev") or "unknown"
        # identifies the unpacked recipes and runtime helper in a work root:
        # it must change with any of them
        script_id = hashlib.sha256((psha + __version__ + engine + helper + "".join(
            self.input_hash[u.key] for u in self.order)).encode()).hexdigest()[:12]
        H = []
        H.append("#!/usr/bin/env bash")
        H.append("# " + "=" * 75)
        H.append("# Standalone build script for emscripten-forge recipe(s): %s" %
                 ", ".join(u.out["name"] for u in self.roots))
        H.append("#")
        H.append("# Generated by emforge-export %s on %s" % (__version__,
                                                           time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())))
        H.append("# from emscripten-forge/recipes %s" % rev)
        H.append("# target platform: %s   build platform: %s" % (self.r.target_platform,
                                                                 self.r.build_platform))
        H.append("#")
        H.append("# Every package below is built from its recipe; recipe sources are")
        H.append("# downloaded from the URLs in the recipes.  The emscripten package installs")
        H.append("# the prebuilt emsdk toolchain.  Native tools come from the system, PyPI or")
        H.append("# pinned upstream source releases (see the tool registry).  No conda, mamba,")
        H.append("# pixi or rattler-build is used, and no conda channel is contacted.")
        H.append("#")
        H.append("# Build order:")
        for u in self.order:
            kind = "native" if u.prov.kind == "native" else "wasm"
            H.append("#   %3d. %-45s [%s]" % (self.num[u.key], u.label, kind))
        picks = [(p, why) for p, why in self.r.history_picks
                 if any(u.prov is p for u in self.order)]
        if picks:
            H.append("#")
            H.append("# Older recipe revisions (from the git history of emscripten-forge/recipes),")
            H.append("# used where the current recipe does not satisfy a pin or requirement:")
            for p, why in picks:
                H.append("#   - %s %s from %s (%s): %s" % (p.name, p.version, p.origin[0][:10],
                                                      p.origin[1], why or ""))
        rel_w = [w for w in self.r.warnings if any(x.label in w for x in self.order)]
        if rel_w:
            H.append("#")
            H.append("# Export warnings:")
            for w in rel_w:
                H.append("#   - %s" % w)
        H.append("#")
        H.append("# Usage: bash %s --help" % self.opt.get("output_name", "build.sh"))
        H.append("# " + "=" * 75)
        H.append("set -Ee -o pipefail")
        H.append("")
        H.append("EF_TARGETS=%s" % arr([u.out["name"] for u in self.roots]))
        H.append("EF_RECIPES_REV=%s" % q(rev))
        H.append("EF_SCRIPT_ID=%s" % q(script_id))
        H.append("EF_EXPORT_BUILD_PLATFORM=%s" % q(self.r.build_platform))
        H.append("EF_EXPORTER_VERSION=%s" % q(__version__))
        H.append("")
        out = "\n".join(H) + "\n" + engine + "\n"
        out += "\nef_write_helper() {\n  cat <<'EF_HELPER_EOF'\n" + helper + "\nEF_HELPER_EOF\n}\n\n"
        out += tools_src + "\n\n"
        out += "# " + "=" * 75 + "\n# packages, in build order\n# " + "=" * 75 + "\n"
        out += "\n\n".join(units_src) + "\n\n"
        out += "EF_ORDER=(%s)\n" % " ".join(str(self.num[u.key]) for u in self.order)
        # dependency closure per package name (for partial builds); the
        # first unit of a name that is not test-only wins
        units_by_name = []
        names_done = set()
        for u in sorted(self.order, key=lambda x: x.test_only):
            if u.out["kind"] == "package" and u.out["name"] not in names_done:
                names_done.add(u.out["name"])
                units_by_name.append(u)
        L = ["ef_deps_of() {", "  case \"$1\" in"]
        for u in units_by_name:
            L.append("    %s) echo %s ;;" % (q(u.out["name"]), q(" ".join(
                str(self.num[k]) for k in self.closure_keys([u])))))
        L.append("  esac")
        L.append("}")
        L.append("# packages needed to run the script tests of a package")
        L.append("ef_testdeps_of() {")
        L.append("  case \"$1\" in")
        for u in units_by_name:
            # tests run for packages of the plan, not for what tests need
            tdeps = [] if u.test_only else u.test_build_units + u.test_run_units
            if tdeps:
                L.append("    %s) echo %s ;;" % (q(u.out["name"]), q(" ".join(
                    str(self.num[k]) for k in self.closure_keys(tdeps)))))
        L.append("  esac")
        L.append("}")
        L.append("ef_unit_of() {")
        L.append("  case \"$1\" in")
        for u in units_by_name:
            L.append("    %s) echo %d ;;" % (q(u.out["name"]), self.num[u.key]))
        L.append("  esac")
        L.append("}")
        L.append("ef_final_of() {")
        L.append("  case \"$1\" in")
        for u in units_by_name:
            L.append("    %s) echo %s ;;" % (q(u.out["name"]), q(" ".join(
                x.ident for x in [u] + self.r.run_closure(u)))))
        L.append("  esac")
        L.append("}")
        out += "\n".join(L) + "\n"
        out += "\nef_main \"$@\"\nexit $?\n\n"
        out += ("# " + "=" * 75 + "\n"
                "# Recipe files (build scripts, patches, test files) of every package above,\n"
                "# verbatim.  They are unpacked to <root>/recipes/ before the first build;\n"
                "# '--extract DIR' writes them out without building.  Each file sits between\n"
                "# a '#@ef-file <tag> <encoding> <mode> <path>' line and '#@ef-end <tag>'.\n"
                "# Bash never reads past the 'exit' above; the files are inside a\n"
                "# here-document only so that 'bash -n' / shellcheck can parse the script.\n"
                "# " + "=" * 75 + "\n")
        # a delimiter that no line of the payload equals
        lines = set(payload.split("\n"))
        delim = "EF_RECIPE_FILES_END_" + psha[:12]
        while delim in lines:
            delim += "_"
        out += ": <<'%s'\n__EF_PAYLOAD_BEGIN__\n%s\n%s\n" % (delim, payload, delim)
        return out

    def closure_keys(self, units):
        """Unit keys needed to build <units> (and their run closure), in
        build order."""
        seen = []

        def visit(x):
            if x.key in seen:
                return
            for d in ([x.staging] if x.staging else []) + x.build_units + x.host_units:
                visit(d)
                for r in self.r.run_closure(d):
                    visit(r)
            seen.append(x.key)
            for r in self.r.run_closure(x):
                visit(r)
        for u in units:
            visit(u)
        order_keys = [v.key for v in self.order]
        return sorted(seen, key=lambda k: order_keys.index(k))


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _payload_block(full, rel, digest):
    if "\n" in rel or "\r" in rel:
        raise RuntimeError("file name with a newline: %r" % rel)
    if os.path.islink(full):
        enc, mode, body = "symlink", "0777", os.readlink(full)
        data = body.encode()
    else:
        with open(full, "rb") as fh:
            data = fh.read()
        mode = "0755" if os.stat(full).st_mode & 0o111 else "0644"
        body = None
        if b"\x00" not in data:
            try:
                body = data.decode("utf-8")
                enc = "text"
            except UnicodeDecodeError:
                body = None
        if body is None:
            enc = "base64"
            b64 = base64.b64encode(data).decode()
            body = "\n".join(b64[i:i + 76] for i in range(0, len(b64), 76))
        elif body and not body.endswith("\n"):
            enc = "text-nonl"
    digest.update(rel.encode() + b"\0" + mode.encode() + b"\0" + data + b"\0")
    tag = hashlib.sha256(rel.encode() + data).hexdigest()[:16]
    while tag in body:
        tag = hashlib.sha256(tag.encode()).hexdigest()[:16]
    if body and not body.endswith("\n"):
        body += "\n"
    return "#@ef-file %s %s %s %s\n%s#@ef-end %s" % (tag, enc, mode, rel, body, tag)


def recipes_rev(root):
    try:
        out = subprocess.check_output(["git", "-C", root, "rev-parse", "HEAD"],
                                      stderr=subprocess.DEVNULL).decode().strip()
        dirty = subprocess.call(["git", "-C", root, "diff", "--quiet"],
                                stderr=subprocess.DEVNULL) != 0
        return out[:12] + ("-dirty" if dirty else "")
    except Exception:  # noqa
        return "unknown"
