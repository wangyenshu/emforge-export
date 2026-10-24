"""Dependency resolution against the recipe tree (no package channels).

Every build/host/run requirement is resolved to one of:
  * a recipe output in recipes/recipes_emscripten  (built for the target)
  * a recipe output in recipes/recipes             (built for the build machine)
  * a native build tool from the tool registry     (system / bootstrapped)
  * a Python package for the build machine         (pip, into BUILD_PREFIX)
Nothing is ever fetched from prefix.dev / conda channels.
"""
import os
import re
import sys

import tempfile

from . import matchspec, tools
from .recipe import RecipeError, render_recipe, normalize_key

VIRTUAL_PREFIX = "__"
# packages that only exist as metadata in conda and carry no files we need
IGNORED_NAMES = {"python_abi", "_libgcc_mutex", "_openmp_mutex"}


def log(msg):
    sys.stderr.write(msg + "\n")


class _Candidate:
    """An older revision of a recipe: its version is known, the recipe
    directory is only checked out when the candidate is chosen."""

    def __init__(self, resolver, base, name, commit, date, version):
        self.r, self.base, self.name = resolver, base, name
        self.commit, self.date, self.version = commit, date, version
        self._prov = False

    def realize(self):
        if self._prov is False:
            self._prov = self.r._render_historic(self.base, self.name, self.commit, self.date)
        return self._prov


def _maybe_pypi(name):
    """Could a requirement no recipe provides be a PyPI distribution?"""
    n = name.lower()
    return not (n.startswith(("r-", "bioconductor-", "lib", "__")) or n.endswith(("-devel", "_impl")))


class Provider:
    """One package output of one rendered recipe."""

    def __init__(self, rendered, output, platform_kind):
        self.rendered = rendered
        self.output = output
        self.kind = platform_kind  # 'target' or 'native'

    @property
    def origin(self):
        """None for the checked-out recipe, (commit, date) for an older one."""
        return getattr(self.rendered, "origin", None)

    @property
    def name(self):
        return self.output["name"]

    @property
    def version(self):
        return self.output["version"]


class Unit:
    def __init__(self, prov, platform):
        self.prov = prov
        self.platform = platform  # target platform of this build
        self.out = prov.output
        self.recipe = prov.rendered
        self.build_units = []     # native recipe outputs -> BUILD_PREFIX
        self.host_units = []      # outputs -> PREFIX
        self.run_units = []       # runtime deps of this package
        self.tools = []           # (name, spec) native tools for this build
        self.pip_build = []       # (name, spec) python pkgs for BUILD_PREFIX
        self.pip_host = []        # (name, spec) pure-python pkgs for PREFIX
        self.run_tools = []       # tools needed when this native pkg is installed
        self.run_pip = []
        self.staging = None       # Unit of the staging cache it inherits
        self.unresolved = []
        self.notes = []
        self.python_version = None
        # requirements of the recipe's script tests (resolved by resolve_tests)
        self.test_build_units = []
        self.test_tools = []
        self.test_pip_build = []
        self.test_run_units = []
        self.test_pip_run = []
        self.test_only = False    # only needed to run another package's tests

    @property
    def key(self):
        o = self.out
        prefix = "staging:" if o["kind"] == "staging" else ""
        return (self.prov.kind, self.recipe.recipe_dir, prefix + o["name"])

    @property
    def ident(self):
        o = self.out
        if o["kind"] == "staging":
            return "%s--staging-%s%s" % (self.recipe.name, o["name"], self._ident_tail())
        return "%s-%s-%s%s" % (o["name"], o["version"], o["build_string"], self._ident_tail())

    def _ident_tail(self):
        # a noarch package of the native tree, resolved for a target
        # environment, is stored apart from its native twin
        if self.prov.kind == "target" and self.platform != getattr(self.recipe, "platform", None):
            return "+" + self.platform.split("-")[0]
        return ""

    @property
    def label(self):
        o = self.out
        org = getattr(self.recipe, "origin", None)
        tail = " [recipe from git %s, %s]" % (org[0][:10], org[1]) if org else ""
        if o["kind"] == "staging":
            return "%s (staging cache of %s)%s" % (o["name"], self.recipe.name, tail)
        return "%s %s%s" % (o["name"], o["version"], tail)


class Resolver:
    def __init__(self, recipes_root, variant_target, variant_native, build_platform,
                 target_platform="emscripten-wasm32", strict_versions=False,
                 skip=None, extra_recipe_dirs=None, history=None):
        self.root = recipes_root
        self.vt = variant_target
        self.vn = variant_native
        self.build_platform = build_platform
        self.target_platform = target_platform
        self.strict_versions = strict_versions
        self.skip = set(normalize_key(s) for s in (skip or []))
        self.target_index = {}
        self.native_index = {}
        self.rendered = {}
        self.units = {}
        self.warnings = []
        self.extra_dirs = extra_recipe_dirs or []
        self.history = history if (history is not None and history.ok) else None
        self.chosen = {}          # (kind, name) -> [Provider] used by the plan
        self._hist_cands = {}     # (kind, recipe rel dir, name) -> [Provider]
        self.history_picks = []   # (Provider, reason)
        self._hist_state = {}
        self._history_reason = history.reason if (history is not None and not history.ok) else ""

    # ------------------------------------------------------------------ index
    def build_index(self):
        tdir = os.path.join(self.root, "recipes", "recipes_emscripten")
        ndir = os.path.join(self.root, "recipes", "recipes")
        for base, kind in [(tdir, "target"), (ndir, "native")] + \
                [(d, "target") for d in self.extra_dirs]:
            if not os.path.isdir(base):
                continue
            for d in sorted(os.listdir(base)):
                rdir = os.path.join(base, d)
                if not os.path.isfile(os.path.join(rdir, "recipe.yaml")):
                    continue
                try:
                    r = self.render(rdir, kind)
                    r.rel_dir = os.path.relpath(rdir, self.root)
                    r.payload_rel = os.path.relpath(rdir, os.path.join(self.root, "recipes"))
                except RecipeError as e:
                    self.warnings.append("recipe %s could not be rendered: %s" % (d, e))
                    continue
                except Exception as e:  # pragma: no cover - defensive
                    self.warnings.append("recipe %s failed to render: %r" % (d, e))
                    continue
                idx = self.target_index if kind == "target" else self.native_index
                for o in r.outputs:
                    if o["kind"] != "package":
                        continue
                    n = normalize_key(o["name"]).lower()
                    if n in idx and idx[n].rendered.recipe_dir != rdir:
                        self.warnings.append(
                            "%s is provided by %s and %s; using the first" %
                            (o["name"], idx[n].rendered.name, r.name))
                        continue
                    idx[n] = Provider(r, o, kind)

    def render(self, rdir, kind):
        key = (rdir, kind)
        if key not in self.rendered:
            if kind == "target":
                self.rendered[key] = render_recipe(rdir, self.target_platform,
                                                   self.build_platform, self.vt)
            else:
                self.rendered[key] = render_recipe(rdir, self.build_platform,
                                                   self.build_platform, self.vn)
        return self.rendered[key]

    # ------------------------------------------------------------- resolution
    def lookup(self, name, kind):
        n = normalize_key(name).lower()
        idx = self.target_index if kind == "target" else self.native_index
        return idx.get(n)

    def variant_pin(self, name, kind):
        v = self.vt if kind == "target" else self.vn
        for k, val in v.items():
            if normalize_key(k) == normalize_key(name):
                return val
        return None

    def unit_for(self, prov, platform):
        u = self.units.get((prov.kind, prov.rendered.recipe_dir, prov.output["name"]))
        if u is None:
            u = Unit(prov, platform)
            self.units[u.key] = u
            self._resolve_unit(u)
        return u

    def unit_in_env(self, prov, env_kind, env_platform):
        """Unit for prov when it is installed into an environment of
        env_kind.  A noarch package of the native tree that ends up in a
        target environment resolves its own dependencies for the target."""
        if prov.kind == env_kind:
            return self.unit_for(prov, env_platform)
        if env_kind == "target" and prov.output["build"].get("noarch"):
            return self.unit_for(Provider(prov.rendered, prov.output, "target"),
                                 self.target_platform)
        return self.unit_for(prov, self.build_platform)

    def staging_unit(self, rendered, name, kind, platform):
        key = (kind, rendered.recipe_dir, "staging:" + name)
        if key in self.units:
            return self.units[key]
        for o in rendered.outputs:
            if o["kind"] == "staging" and o["name"] == name:
                u = Unit(Provider(rendered, o, kind), platform)
                self.units[key] = u
                self._resolve_unit(u)
                return u
        raise RecipeError("staging %s not found in %s" % (name, rendered.name))

    def pin_version(self, name, kind):
        """Version part of a variant pin ('3.13.* *_cp313' -> '3.13.*')."""
        pin = self.variant_pin(name, kind)
        if pin is None or isinstance(pin, bool):
            return None
        pv = str(pin).split(" ")[0].strip()
        return pv or None

    def _tool_spec(self, name, ver, kind):
        """Version spec for a native tool: the recipe's, else a soft ('~')
        variant pin -- honoured when cheap (pip), not enforced otherwise."""
        if ver is not None:
            return ver
        pv = self.pin_version(name, kind)
        return ("~" + pv) if pv else None

    # ------------------------------------------------------- version choice
    def _history_candidates(self, base, name):
        """Historic providers rendered so far for base's recipe and <name>."""
        key = (base.kind, base.rendered.rel_dir, normalize_key(name).lower())
        return self._hist_cands.setdefault(key, [])

    def _iter_history(self, base, name):
        """Older revisions of base's recipe providing <name>, newest first,
        one per version, as _Candidate objects (checked out on demand)."""
        if self.history is None or not getattr(base.rendered, "rel_dir", None):
            return
        key = (base.kind, base.rendered.rel_dir, normalize_key(name).lower())
        cached = self._history_candidates(base, name)
        for p in list(cached):
            yield p
        state = self._hist_state.setdefault(key, {"pos": 0, "seen": {base.version} |
                                                   {p.version for p in cached}})
        commits = self.history.commits(base.rendered.rel_dir)
        while state["pos"] < len(commits):
            commit, date = commits[state["pos"]]
            state["pos"] += 1
            version = self._peek_version(base, name, commit, state["seen"])
            if version is not None:
                c = _Candidate(self, base, name, commit, date, version)
                cached.append(c)
                yield c

    def _peek_version(self, base, name, commit, seen):
        """Version of <name> in base's recipe.yaml at <commit> (rendered on
        its own), or None when unknown or already seen."""
        text = self.history.recipe_text(commit, base.rendered.rel_dir)
        if not text:
            return None
        n = normalize_key(name).lower()
        with tempfile.TemporaryDirectory(prefix="ef-peek-") as tmp:
            with open(os.path.join(tmp, "recipe.yaml"), "w", encoding="utf-8") as f:
                f.write(text)
            try:
                r = render_recipe(tmp, *self._render_args(base.kind))
            except Exception:  # noqa -- old recipes may not render any more
                return None
        outs = [o for o in r.outputs if o["kind"] == "package"
                and normalize_key(o["name"]).lower() == n]
        if not outs or outs[0]["version"] in seen:
            return None
        seen.add(outs[0]["version"])
        return outs[0]["version"]

    def _render_historic(self, base, name, commit, date):
        rel_dir = base.rendered.rel_dir
        rdir = self.history.materialize(commit, rel_dir)
        try:
            r = render_recipe(rdir, *self._render_args(base.kind))
        except Exception:  # noqa
            return None
        r.rel_dir = rel_dir
        r.origin = (commit, date)
        r.payload_rel = "%s@%s" % (os.path.relpath(os.path.join(self.root, rel_dir),
                                                   os.path.join(self.root, "recipes")), commit[:10])
        n = normalize_key(name).lower()
        for o in r.outputs:
            if o["kind"] == "package" and normalize_key(o["name"]).lower() == n:
                return Provider(r, o, base.kind)
        return None

    def _render_args(self, kind):
        if kind == "target":
            return (self.target_platform, self.build_platform, self.vt)
        return (self.build_platform, self.build_platform, self.vn)

    def choose(self, name, kind, explicit, pin, who):
        """Pick the recipe output that provides <name>.

        explicit: version spec written in the recipe (or None)
        pin:      variant pin version (or None) -- a requirement when the
                  recipe gives no version (rattler-build semantics), else a
                  preference that keeps the plan on one version per package.
        Order: outputs the plan already uses, the checked-out recipe, then
        older revisions from git history."""
        base = self.lookup(name, kind)
        if base is None:
            return None
        n = normalize_key(name).lower()
        explicit = None if (explicit is None or "@@EF" in str(explicit)) else str(explicit)
        used = self.chosen.setdefault((kind, n), [])

        def ok(p, with_pin):
            if explicit and not matchspec.version_matches(p.version, explicit):
                return False
            if with_pin and pin and not matchspec.version_matches(p.version, pin):
                return False
            return True

        def take(p, why=None):
            if p not in used:
                used.append(p)
                if p.origin:
                    self.history_picks.append((p, why))
            return p

        rounds = [True, False] if pin else [False]
        for with_pin in rounds:
            if pin and not with_pin and explicit is None:
                break   # the pin is the only constraint and nothing meets it
            for p in used:
                if ok(p, with_pin):
                    return p
            if ok(base, with_pin):
                return take(base)
            if self.history is not None:
                for c in self._iter_history(base, name):
                    if not ok(c, with_pin):
                        continue
                    p = c.realize()
                    if p is None:
                        continue
                    want = " ".join(x for x in (explicit, ("pin " + pin) if pin and with_pin else None) if x)
                    return take(p, "%s needs %s %s; current recipe has %s" % (
                        who.label, name, want, base.version))
            if with_pin and explicit is None:
                # rattler-build would enforce the pin; nothing satisfies it
                pass
        # nothing satisfies the constraints
        p = used[0] if used else base
        want = explicit or pin
        msg = "%s: requires %s %s but recipe %s provides version %s" % (
            who.label, name, want, p.rendered.name, p.version)
        if self.history is None and self._history_reason:
            msg += " (%s)" % self._history_reason
        if self.strict_versions:
            raise RecipeError(msg)
        who.notes.append(msg)
        self.warnings.append(msg)
        return take(p)

    def _resolve_unit(self, u):
        o = u.out
        kind = u.prov.kind
        reqs = o["requirements"]
        if o.get("inherit"):
            u.staging = self.staging_unit(u.recipe, o["inherit"]["from"], kind, u.platform)
        native_platform = kind == "native"
        # a Python package: requirements no recipe provides are taken from
        # PyPI (pure-Python wheels) -- conda-forge noarch packages otherwise
        is_py = any(normalize_key(matchspec.split_spec(x)[0]) == "python"
                    for x in reqs["host"] + reqs["run"])

        # ---- build requirements -> BUILD_PREFIX
        for spec in reqs["build"]:
            name, ver, build = matchspec.split_spec(spec)
            if self._ignored(name):
                continue
            prov = None
            if normalize_key(name) not in self.skip:
                pin = self.pin_version(name, "native") if ver is None else None
                prov = self.choose(name, "native", ver, pin, u)
            if prov is not None:
                if prov.output is o:
                    continue
                bu = self.unit_for(prov, self.build_platform)
                if bu not in u.build_units:
                    u.build_units.append(bu)
                continue
            self._add_tool(u, name, self._tool_spec(name, ver, "native"), build,
                           u.tools, u.pip_build, native_platform)

        # ---- host requirements -> PREFIX
        for spec in reqs["host"]:
            name, ver, build = matchspec.split_spec(spec)
            if self._ignored(name):
                continue
            if normalize_key(name) in self.skip:
                u.notes.append("host dependency %s skipped (--skip)" % name)
                continue
            pin = self.pin_version(name, kind) if ver is None else None
            prov = self.choose(name, kind, ver, pin, u)
            if prov is None and kind == "target":
                # noarch packages from the native tree can be used as well
                nprov = self.lookup(name, "native")
                if nprov is not None and nprov.output["build"].get("noarch"):
                    prov = self.choose(name, "native", ver, pin, u)
            if prov is not None:
                if prov.output is o:
                    continue
                hu = self.unit_in_env(prov, kind, u.platform)
                if hu not in u.host_units:
                    u.host_units.append(hu)
                if normalize_key(name) == "python":
                    u.python_version = prov.version
                continue
            ver = self._tool_spec(name, ver, kind)
            if kind == "native":
                if is_py and not tools.TOOLS.get(tools.classify(name, True)[1]):
                    u.pip_build.append((tools.pypi_name(name), ver))
                else:
                    self._add_tool(u, name, ver, build, u.tools, u.pip_build, True)
            else:
                if tools.is_python_package(name) or (is_py and _maybe_pypi(name)):
                    u.pip_host.append((tools.pypi_name(name), ver))
                    u.notes.append("host dependency %s is not built from a recipe; "
                                   "it is installed from PyPI as a pure-Python wheel" % name)
                else:
                    u.unresolved.append(spec)
                    msg = "%s: host dependency '%s' is not provided by any recipe" % (u.label, spec)
                    u.notes.append(msg)
                    self.warnings.append(msg)

        # ---- run requirements (+ run_exports of host / build deps)
        run_specs = list(reqs["run"])
        ignore_names = set(normalize_key(x) for x in reqs["ignore_run_exports"]["by_name"])
        ignore_from = set(normalize_key(x) for x in reqs["ignore_run_exports"]["from_package"])
        for hu in u.host_units:
            if normalize_key(hu.out["name"]) in ignore_from:
                continue
            rex = hu.out["requirements"]["run_exports"]
            for s in rex["weak"] + rex["strong"]:
                n, _, _ = matchspec.split_spec(s)
                if normalize_key(n) not in ignore_names:
                    run_specs.append(s)
        for bu in u.build_units:
            if normalize_key(bu.out["name"]) in ignore_from:
                continue
            for s in bu.out["requirements"]["run_exports"]["strong"]:
                n, _, _ = matchspec.split_spec(s)
                if normalize_key(n) not in ignore_names:
                    run_specs.append(s)
        seen = set()
        for spec in run_specs:
            name, ver, build = matchspec.split_spec(spec.replace("@@EF:EXACT@@", ""))
            nn = normalize_key(name)
            if nn in seen or self._ignored(name) or nn == normalize_key(o["name"]):
                continue
            seen.add(nn)
            if nn in self.skip:
                continue
            # a run dependency on something this package was built against
            # is that same build
            prov = None
            for hu in u.host_units + u.build_units:
                if normalize_key(hu.out["name"]) == nn and hu.prov.kind in (kind, "native") \
                        and (hu.prov.kind == kind or hu.out["build"].get("noarch")):
                    prov = hu.prov
                    break
            if prov is None:
                prov = self.choose(name, kind, ver, None, u)
            if prov is None and kind == "target":
                nprov = self.lookup(name, "native")
                if nprov is not None and nprov.output["build"].get("noarch"):
                    prov = self.choose(name, "native", ver, None, u)
            if prov is not None:
                if prov.output is o:
                    continue
                ru = self.unit_in_env(prov, kind, u.platform)
                if ru not in u.run_units:
                    u.run_units.append(ru)
                continue
            if kind == "native":
                if is_py and not tools.TOOLS.get(tools.classify(name, True)[1]):
                    u.run_pip.append((tools.pypi_name(name), ver))
                else:
                    self._add_tool(u, name, ver, build, u.run_tools, u.run_pip, True)
            elif tools.is_python_package(name) or (is_py and _maybe_pypi(name)):
                u.run_pip.append((tools.pypi_name(name), ver))
                u.notes.append("run dependency %s is not built from a recipe; it is "
                               "installed from PyPI (pure-Python wheel)" % name)
            else:
                u.notes.append("run dependency %s is not provided by any recipe" % spec)

        # ---- python version for PY_VER etc.
        if u.python_version is None:
            pv = o["variant_used"].get("python")
            if pv:
                u.python_version = str(pv).split()[0].replace("*", "").rstrip(".")

    def _ignored(self, name):
        n = name.strip()
        return (not n or n.startswith(VIRTUAL_PREFIX) or n in IGNORED_NAMES
                or "@@EF" in n)

    def _add_tool(self, u, name, ver, build, tool_list, pip_list, native_platform):
        t = tools.classify(name, native_platform)
        if t is None:
            return
        kind, tname = t
        if kind == "tool":
            entry = (tname, ver)
            if entry not in tool_list:
                tool_list.append(entry)
        elif kind == "pip":
            entry = (tname, ver)
            if entry not in pip_list:
                pip_list.append(entry)

    # ------------------------------------------------------------------- plan
    def resolve(self, names, with_tests=True):
        roots = []
        for spec in names:
            # "name", "name 1.2", "name=1.2", "name>=1.2,<2"
            m = re.match(r"^\s*([A-Za-z0-9_][A-Za-z0-9_.\-]*)\s*(.*?)\s*$", spec)
            if not m:
                raise RecipeError("cannot parse package spec %r" % spec)
            n, ver = m.group(1), (m.group(2) or None)
            kind, plat = "target", self.target_platform
            if self.lookup(n, kind) is None:
                kind, plat = "native", self.build_platform
            if self.lookup(n, kind) is None:
                raise RecipeError("no recipe provides '%s'" % n)

            class _Who:
                label = "command line"
                notes = []
            prov = self.choose(n, kind, ver, None, _Who)
            roots.append(self.unit_for(prov, plat))
        order = []
        state = {}

        def visit(u, stack):
            st = state.get(u.key)
            if st == 2:
                return
            if st == 1:
                cyc = " -> ".join(x.label for x in stack[stack.index(u):] + [u])
                raise RecipeError("dependency cycle: %s (use --skip to break it)" % cyc)
            state[u.key] = 1
            stack.append(u)
            deps = []
            if u.staging:
                deps.append(u.staging)
            for d in u.build_units + u.host_units:
                deps.append(d)
            for d in deps:
                visit(d, stack)
                for r in self.run_closure(d):
                    visit(r, stack)
            stack.pop()
            state[u.key] = 2
            order.append(u)

        for r in roots:
            visit(r, [])
            for x in self.run_closure(r):
                visit(x, [])
        if with_tests:
            main = set(u.key for u in order)
            for u in list(order):
                self.resolve_tests(u)
            for u in list(order):
                for t in u.test_build_units + u.test_run_units:
                    for x in [t] + self.run_closure(t):
                        visit(x, [])
            for u in order:
                u.test_only = u.key not in main
        return roots, order

    def resolve_tests(self, u):
        """Requirements of u's script tests: build -> native recipes / tools
        (test BUILD_PREFIX), run -> recipes / PyPI (test PREFIX)."""
        if u.out["kind"] != "package":
            return
        kind = u.prov.kind
        for t in u.out.get("tests") or []:
            if t.get("type") != "script":
                continue
            for spec in t["requirements"]["build"]:
                name, ver, build = matchspec.split_spec(spec)
                if self._ignored(name) or normalize_key(name) in self.skip:
                    continue
                pin = self.pin_version(name, "native") if ver is None else None
                prov = self.choose(name, "native", ver, pin, u)
                if prov is not None:
                    tu = self.unit_for(prov, self.build_platform)
                    if tu not in u.test_build_units:
                        u.test_build_units.append(tu)
                    continue
                self._add_tool(u, name, self._tool_spec(name, ver, "native"), build,
                               u.test_tools, u.test_pip_build, True)
            for spec in t["requirements"]["run"]:
                name, ver, build = matchspec.split_spec(spec)
                nn = normalize_key(name)
                if self._ignored(name) or nn in self.skip or nn == normalize_key(u.out["name"]):
                    continue
                prov = self.choose(name, kind, ver, None, u)
                if prov is None and kind == "target":
                    nprov = self.lookup(name, "native")
                    if nprov is not None and nprov.output["build"].get("noarch"):
                        prov = self.choose(name, "native", ver, None, u)
                if prov is not None:
                    tu = self.unit_in_env(prov, kind, u.platform)
                    if tu not in u.test_run_units:
                        u.test_run_units.append(tu)
                elif kind == "native" or tools.is_python_package(name) or u.python_version:
                    if (name, ver) not in u.test_pip_run:
                        u.test_pip_run.append((name, ver))
                else:
                    u.notes.append("test dependency %s is not provided by any recipe" % spec)

    def run_closure(self, u, seen=None):
        seen = set() if seen is None else seen
        out = []
        for r in u.run_units:
            if r.key in seen:
                continue
            seen.add(r.key)
            out.append(r)
            out.extend(self.run_closure(r, seen))
        return out

    def install_set(self, units):
        """units + their run-dependency closure, de-duplicated, in order."""
        res = []
        keys = set()
        for u in units:
            for x in [u] + self.run_closure(u):
                if x.key not in keys:
                    keys.add(x.key)
                    res.append(x)
        return res
