"""Rendering of rattler-build (v1, recipe.yaml) recipes.

This re-implements the parts of rattler-build's stage0 -> stage1 evaluation
that matter for building: context evaluation, ${{ }} jinja expressions,
if/then/else conditional lists, multi-output / staging-cache recipes, and the
normalisation of sources, scripts, requirements and tests.  Semantics follow
rattler-build 0.67 (the version pinned by emscripten-forge).
"""
import hashlib
import json
import os
import re

import jinja2
import yaml

from . import matchspec

# ---------------------------------------------------------------------------
# YAML loading: like rattler-build, never turn "1.10" into a float.
# ---------------------------------------------------------------------------


class _NoFloatLoader(yaml.SafeLoader):
    pass


_NoFloatLoader.yaml_implicit_resolvers = {
    k: [(tag, rx) for tag, rx in v if tag != "tag:yaml.org,2002:float"]
    for k, v in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def load_yaml(path):
    with open(path, encoding="utf-8") as f:
        return yaml.load(f, Loader=_NoFloatLoader)


# ---------------------------------------------------------------------------
# Platforms
# ---------------------------------------------------------------------------
KNOWN_PLATFORMS = [
    "linux-32", "linux-64", "linux-aarch64", "linux-armv6l", "linux-armv7l",
    "linux-ppc64le", "linux-ppc64", "linux-s390x", "linux-riscv64",
    "osx-64", "osx-arm64", "win-32", "win-64", "win-arm64",
    "emscripten-wasm32", "emscripten-wasm64", "wasi-wasm32",
    "freebsd-64", "zos-z",
]
ARCH_OF = {
    "32": "x86", "64": "x86_64", "aarch64": "aarch64", "arm64": "arm64",
    "armv6l": "armv6l", "armv7l": "armv7l", "ppc64le": "ppc64le",
    "ppc64": "ppc64", "s390x": "s390x", "riscv64": "riscv64",
    "wasm32": "wasm32", "wasm64": "wasm64", "z": "z",
}
ALL_ARCHS = sorted(set(ARCH_OF.values()))


def platform_family(p):
    if p == "noarch":
        return None
    return p.split("-", 1)[0]


def platform_arch(p):
    if p == "noarch" or "-" not in p:
        return None
    return ARCH_OF.get(p.split("-", 1)[1])


def platform_is_unix(p):
    return platform_family(p) in ("linux", "osx", "emscripten", "freebsd", "ios", "android")


def current_build_platform():
    import platform as _pf
    sysname = _pf.system().lower()
    mach = _pf.machine().lower()
    if sysname == "darwin":
        return "osx-arm64" if mach in ("arm64", "aarch64") else "osx-64"
    if mach in ("aarch64", "arm64"):
        return "linux-aarch64"
    if mach in ("ppc64le",):
        return "linux-ppc64le"
    return "linux-64"


def normalize_key(k):
    return str(k).replace("-", "_").replace(".", "_")


# Environment variables rattler-build defines for a build.  In inline scripts
# they are rendered at build time; we turn them into late-bound tokens that
# the generated build.sh substitutes right before running the script.
BUILD_ENV_VARS = [
    "PREFIX", "BUILD_PREFIX", "SRC_DIR", "RECIPE_DIR", "BUILD_DIR", "PYTHON",
    "PY_VER", "SP_DIR", "STDLIB_DIR", "PY3K", "NPY_VER", "R", "R_VER", "R_USER",
    "PKG_NAME", "PKG_VERSION", "PKG_BUILDNUM", "PKG_BUILD_STRING", "PKG_HASH",
    "CPU_COUNT", "SHLIB_EXT", "ARCH", "SUBDIR", "CONDA_BUILD_CROSS_COMPILATION",
    "CONDA_BUILD_STATE", "CONDA_DEFAULT_ENV", "SOURCE_DATE_EPOCH", "HOME",
    "PATH", "LANG", "LC_ALL", "BUILD", "PIP_CACHE_DIR",
]
LATE_PATCH_VARS = ["SRC_DIR", "RECIPE_DIR", "BUILD_DIR"]
LATE_LICENSE_VARS = ["PREFIX", "BUILD_PREFIX", "SRC_DIR", "RECIPE_DIR", "BUILD_DIR"]


def late_token(var):
    return "@@EF:%s@@" % var


class RecipeError(Exception):
    pass


# ---------------------------------------------------------------------------
# Jinja environment
# ---------------------------------------------------------------------------


class _Undefined(jinja2.ChainableUndefined):
    """Lenient undefined (rattler-build uses lenient rendering at build time)."""

    def __str__(self):
        return ""


def _version_to_buildstring(v):
    parts = str(v).split(".")
    return "".join(parts[:2])


def _split(value, sep=None, maxsplit=-1):
    return str(value).split(sep, maxsplit)


class _EnvObj:
    def get(self, name, default=None):
        return os.environ.get(name, default if default is not None else "")

    def exists(self, name):
        return name in os.environ


class _GitObj:
    def head_rev(self, *a, **k):
        return ""

    def latest_tag(self, *a, **k):
        return ""

    def latest_tag_rev(self, *a, **k):
        return ""


class EvalContext:
    def __init__(self, target_platform, build_platform, variant, recipe_dir=None,
                 host_platform=None):
        self.target_platform = target_platform
        self.build_platform = build_platform
        self.host_platform = host_platform or target_platform
        self.variant = dict(variant)
        self.recipe_dir = recipe_dir
        self.warnings = []
        self.vars = {}
        self._setup_platform_vars()
        for k, v in self.variant.items():
            self.vars[normalize_key(k)] = v
        self.env = self._make_env()

    # -- setup -------------------------------------------------------------
    def _setup_platform_vars(self):
        hp = self.host_platform
        v = self.vars
        v["target_platform"] = self.target_platform
        v["build_platform"] = self.build_platform
        v["host_platform"] = hp
        v["unix"] = platform_is_unix(hp)
        arch = platform_arch(hp)
        for a in ALL_ARCHS:
            v[a] = (a == arch)
        fam = platform_family(hp)
        for p in KNOWN_PLATFORMS:
            v[p.replace("-", "")] = (p == hp)
            v[platform_family(p)] = (platform_family(p) == fam)
        for f in ("ios", "android", "iossimulator"):
            v.setdefault(f, False)
        v["win"] = fam == "win"

    def _make_env(self):
        env = jinja2.Environment(
            variable_start_string="${{",
            variable_end_string="}}",
            block_start_string="\x00EF{%",
            block_end_string="%}EF\x00",
            comment_start_string="\x00EF{#",
            comment_end_string="#}EF\x00",
            undefined=_Undefined,
            keep_trailing_newline=True,
            autoescape=False,
        )
        env.filters["version_to_buildstring"] = _version_to_buildstring
        env.filters["split"] = _split
        env.filters["bool"] = lambda x: bool(x) and str(x).lower() not in ("false", "0", "")
        g = env.globals
        g["compiler"] = self._compiler
        g["stdlib"] = self._stdlib
        g["pin_subpackage"] = self._pin
        g["pin_compatible"] = self._pin
        g["cdt"] = lambda name: name
        g["match"] = self._match
        g["cmp"] = self._match
        g["is_linux"] = lambda p: platform_family(p) == "linux"
        g["is_osx"] = lambda p: platform_family(p) == "osx"
        g["is_windows"] = lambda p: platform_family(p) == "win"
        g["is_unix"] = lambda p: platform_is_unix(p)
        g["load_from_file"] = self._load_from_file
        g["env"] = _EnvObj()
        g["git"] = _GitObj()
        return env

    # -- jinja functions -----------------------------------------------------
    def _variant_get(self, key):
        nk = normalize_key(key)
        for k, v in self.variant.items():
            if normalize_key(k) == nk:
                return v
        return None

    def _compiler(self, lang):
        name = self._variant_get("%s_compiler" % lang)
        if name is None:
            fam = platform_family(self.host_platform)
            if lang == "fortran":
                name = "flang" if fam == "win" else "gfortran"
            elif lang not in ("c", "cxx"):
                name = lang
            elif fam == "osx":
                name = "clang" if lang == "c" else "clangxx"
            elif fam == "win":
                name = "vs2022"
            elif self.host_platform == "emscripten-wasm32":
                name = "emscripten"
            else:
                name = "gcc" if lang == "c" else "gxx"
        ver = self._variant_get("%s_compiler_version" % lang)
        res = "%s_%s" % (name, self.host_platform)
        if ver is not None:
            ver = str(ver)
            if re.fullmatch(r"[A-Za-z0-9.]+", ver):
                res += " =" + ver
            else:
                res += " " + ver
        return res

    def _stdlib(self, lang):
        name = self._variant_get("%s_stdlib" % lang)
        if name is None:
            # rattler-build errors here; emscripten-forge has no stdlib variant
            self.warnings.append("stdlib('%s') has no variant; ignored" % lang)
            return ""
        ver = self._variant_get("%s_stdlib_version" % lang)
        res = "%s_%s" % (name, self.host_platform)
        if ver is not None:
            res += " " + str(ver)
        return res

    def _pin(self, name, *args, **kwargs):
        exact = kwargs.get("exact")
        return "%s%s" % (name, " @@EF:EXACT@@" if exact else "")

    def _match(self, value, spec):
        if isinstance(value, jinja2.Undefined) or value is None:
            return False
        v = str(value).split()[0]
        return matchspec.version_matches(v, str(spec))

    def _load_from_file(self, path):
        full = os.path.join(self.recipe_dir or ".", path)
        with open(full, encoding="utf-8") as f:
            if path.endswith((".yaml", ".yml")):
                return yaml.load(f, Loader=_NoFloatLoader)
            if path.endswith(".json"):
                return json.load(f)
            if path.endswith(".toml"):
                try:
                    import tomllib
                except ImportError:  # pragma: no cover
                    import tomli as tomllib
                return tomllib.loads(f.read())
            return f.read()

    # -- rendering -------------------------------------------------------------
    def child(self):
        c = EvalContext.__new__(EvalContext)
        c.__dict__.update(self.__dict__)
        c.vars = dict(self.vars)
        c.env = self._make_env_shared()
        return c

    def _make_env_shared(self):
        return self.env

    def eval_expr(self, expr, extra=None):
        ctx = self.vars if not extra else dict(self.vars, **extra)
        try:
            fn = self.env.compile_expression(expr.strip(), undefined_to_none=False)
            return fn(**ctx)
        except jinja2.TemplateError as e:
            raise RecipeError("cannot evaluate expression %r: %s" % (expr, e))

    def truthy(self, val):
        if isinstance(val, jinja2.Undefined):
            return False
        if isinstance(val, str):
            return val.strip().lower() not in ("", "false", "0", "no", "none")
        return bool(val)

    def eval_cond(self, cond, extra=None):
        if isinstance(cond, bool):
            return cond
        if cond is None:
            return False
        s = str(cond)
        if "${{" in s:
            return self.truthy(self.render_str(s, extra))
        return self.truthy(self.eval_expr(s, extra))

    def render_str(self, s, extra=None):
        if "${{" not in s:
            return s
        m = re.fullmatch(r"\s*\$\{\{(.*?)\}\}\s*", s, re.S)
        if m and "${{" not in m.group(1):
            val = self.eval_expr(m.group(1), extra)
            if isinstance(val, jinja2.Undefined) or val is None:
                return ""
            return val
        ctx = self.vars if not extra else dict(self.vars, **extra)
        try:
            return self.env.from_string(s).render(**ctx)
        except jinja2.TemplateError as e:
            raise RecipeError("cannot render %r: %s" % (s[:200], e))

    def render(self, v, extra=None):
        if isinstance(v, str):
            return self.render_str(v, extra)
        if isinstance(v, list):
            return self.render_list(v, extra)
        if isinstance(v, dict):
            if "if" in v and "then" in v and len(set(v) - {"if", "then", "else"}) == 0:
                # conditional outside of a list: pick branch
                br = v.get("then") if self.eval_cond(v["if"], extra) else v.get("else")
                return self.render(br, extra) if br is not None else None
            return {k: self.render(x, extra) for k, x in v.items()}
        return v

    def render_list(self, lst, extra=None, drop_empty=True):
        out = []
        if lst is None:
            return out
        if not isinstance(lst, list):
            lst = [lst]
        for item in lst:
            if isinstance(item, dict) and "if" in item and "then" in item:
                branch = item.get("then") if self.eval_cond(item["if"], extra) else item.get("else")
                if branch is None:
                    continue
                if not isinstance(branch, list):
                    branch = [branch]
                out.extend(self.render_list(branch, extra, drop_empty))
                continue
            r = self.render(item, extra)
            if drop_empty and isinstance(item, str) and "${{" in item and (r is None or r == ""):
                continue
            out.append(r)
        return out

    def preserve_list(self, lst, extra=None):
        """Expand conditionals only, keep strings as templates (scripts)."""
        out = []
        if lst is None:
            return out
        if not isinstance(lst, list):
            lst = [lst]
        for item in lst:
            if isinstance(item, dict) and "if" in item and "then" in item:
                branch = item.get("then") if self.eval_cond(item["if"], extra) else item.get("else")
                if branch is None:
                    continue
                out.extend(self.preserve_list(branch if isinstance(branch, list) else [branch], extra))
            else:
                out.append(item)
        return out

    def eval_context_section(self, context):
        for k, v in (context or {}).items():
            self.vars[k] = self.render(v)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _as_list(x):
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


def _s(x):
    if x is None:
        return None
    if isinstance(x, bool):
        return "true" if x else "false"
    return str(x)


def render_template_late(ctx, s, allowed):
    extra = {v: late_token(v) for v in allowed if v not in ctx.vars}
    r = ctx.render(s, extra)
    return _s(r)


def render_script_template(ctx, s):
    extra = {v: late_token(v) for v in BUILD_ENV_VARS if v not in ctx.vars}
    r = ctx.render_str(s, extra) if isinstance(s, str) else s
    return _s(r) if r is not None else ""


# ---------------------------------------------------------------------------
# stage0 -> stage1
# ---------------------------------------------------------------------------

def _eval_source(ctx, src):
    out = []
    for item in ctx.render_list(_as_list(src), drop_empty=True) if False else _expand_sources(ctx, src):
        out.append(item)
    return out


def _expand_sources(ctx, src):
    """Expand conditionals in the source list and render each source."""
    res = []
    for item in ctx.preserve_list(_as_list(src)):
        if not isinstance(item, dict):
            continue
        s = {}
        patches = item.get("patches")
        for k, v in item.items():
            if k == "patches":
                continue
            s[k] = ctx.render(v)
        rendered_patches = []
        for p in ctx.preserve_list(_as_list(patches)):
            r = render_template_late(ctx, p, LATE_PATCH_VARS)
            if r:
                rendered_patches.append(r)
        res.append(_normalize_source(s, rendered_patches))
    return res


def _normalize_source(s, patches):
    out = {"patches": patches}
    if "url" in s:
        out["kind"] = "url"
        out["urls"] = [_s(u) for u in _as_list(s["url"])]
        out["sha256"] = _s(s.get("sha256"))
        out["md5"] = _s(s.get("md5"))
        out["file_name"] = _s(s.get("file_name"))
    elif "git" in s:
        out["kind"] = "git"
        out["url"] = _s(s["git"])
        rev = None
        for key in ("rev", "tag", "branch"):
            if s.get(key) is not None:
                rev = (key, _s(s[key]))
        out["rev"] = rev
        out["depth"] = s.get("depth")
        out["lfs"] = bool(s.get("lfs", False))
        out["submodules"] = s.get("submodules", True)
        out["expected_commit"] = _s(s.get("expected_commit"))
    elif "path" in s:
        out["kind"] = "path"
        out["path"] = _s(s["path"])
        out["use_gitignore"] = bool(s.get("use_gitignore", True))
        out["filter"] = s.get("filter")
        out["sha256"] = _s(s.get("sha256"))
        out["md5"] = _s(s.get("md5"))
        out["file_name"] = _s(s.get("file_name"))
    else:
        raise RecipeError("unknown source type: %r" % (s,))
    out["target_directory"] = _s(s.get("target_directory"))
    return out


def _eval_script(ctx, script, recipe_dir, default_mode):
    """Return a normalized script dict.

    default_mode: 'default' (single output: look for build.sh) or 'none'
    (multi-output: an unset script is a no-op).
    """
    res = {"type": None, "path": None, "content": None, "env": {}, "interpreter": None,
           "cwd": None}
    if script is None or script == [] or script == {}:
        if default_mode == "default":
            p = _find_script_file(recipe_dir, "build")
            if p:
                res.update(type="file", path=p)
            else:
                res.update(type="none")
        else:
            res.update(type="none")
        return res
    if isinstance(script, dict):
        env = {}
        for k, v in (script.get("env") or {}).items():
            vv = _s(ctx.render(v))
            if vv:
                env[k] = vv
        res["env"] = env
        if script.get("interpreter"):
            res["interpreter"] = _s(ctx.render(script["interpreter"]))
        if script.get("cwd"):
            res["cwd"] = _s(ctx.render(script["cwd"]))
        if script.get("file") is not None:
            f = _s(ctx.render(script["file"]))
            p = _find_script_file(recipe_dir, f)
            if not p:
                raise RecipeError("script file %s not found" % f)
            res.update(type="file", path=p)
            if res["interpreter"] is None:
                res["interpreter"] = _interp_from_ext(p)
            return res
        content = ctx.preserve_list(_as_list(script.get("content")))
        res.update(type="commands", content=[render_script_template(ctx, c) for c in content])
        return res
    items = ctx.preserve_list(_as_list(script))
    if len(items) == 1 and isinstance(items[0], str):
        cmd = items[0]
        if "\n" not in cmd and "${{" not in cmd:
            p = _find_script_file(recipe_dir, cmd)
            if p:
                res.update(type="file", path=p, interpreter=_interp_from_ext(p))
                return res
        res.update(type="commands", content=[render_script_template(ctx, cmd)])
        return res
    res.update(type="commands", content=[render_script_template(ctx, c) for c in items])
    return res


def _interp_from_ext(path):
    ext = os.path.splitext(path)[1].lower()
    return {".py": "python", ".pl": "perl", ".r": "rscript", ".nu": "nushell",
            ".js": "nodejs", ".rb": "ruby"}.get(ext)


def _find_script_file(recipe_dir, rel):
    if recipe_dir is None or not rel:
        return None
    if "\n" in rel or len(rel) > 300:
        return None
    full = rel if os.path.isabs(rel) else os.path.join(recipe_dir, rel)
    if os.path.splitext(full)[1] == "":
        for ext in ("sh",):
            cand = full + "." + ext
            if os.path.isfile(cand):
                return os.path.relpath(cand, recipe_dir)
        return None
    if os.path.isfile(full):
        return os.path.relpath(full, recipe_dir)
    return None


def _eval_files(ctx, files):
    if files is None:
        return None
    r = ctx.render(files)
    if isinstance(r, list):
        return {"include": [_s(x) for x in r if x], "exclude": []}
    if isinstance(r, dict):
        return {"include": [_s(x) for x in _as_list(r.get("include")) if x],
                "exclude": [_s(x) for x in _as_list(r.get("exclude")) if x]}
    return None


def _eval_skip(ctx, skip):
    if skip is None:
        return False
    if isinstance(skip, bool):
        return skip
    for item in ctx.preserve_list(_as_list(skip)):
        if ctx.eval_cond(item):
            return True
    return False


def _eval_build(ctx, build, recipe_dir, default_mode, with_script=True):
    build = build or {}
    out = {}
    out["number"] = build.get("number")
    if out["number"] is not None:
        out["number"] = int(str(ctx.render(out["number"])) or 0)
    out["string"] = _s(ctx.render(build.get("string"))) if build.get("string") is not None else None
    out["skip"] = _eval_skip(ctx, build.get("skip"))
    out["noarch"] = _s(ctx.render(build.get("noarch"))) if build.get("noarch") else None
    out["files"] = _eval_files(ctx, build.get("files"))
    out["always_include_files"] = [_s(x) for x in ctx.render_list(build.get("always_include_files"))]
    out["always_copy_files"] = [_s(x) for x in ctx.render_list(build.get("always_copy_files"))]
    m = build.get("merge_build_and_host_envs")
    out["merge_build_and_host_envs"] = bool(ctx.render(m)) if m is not None else False
    if with_script:
        out["script"] = _eval_script(ctx, build.get("script"), recipe_dir, default_mode)
        out["script_set"] = build.get("script") is not None
    else:
        out["script"] = None
        out["script_set"] = False
    out["python"] = ctx.render(build.get("python")) or {}
    out["dynamic_linking"] = ctx.render(build.get("dynamic_linking")) or {}
    out["prefix_detection"] = ctx.render(build.get("prefix_detection")) or {}
    return out


def _merge_build(top, own):
    """merge_stage1_build for outputs inheriting from top-level."""
    res = dict(top)
    for k, v in own.items():
        if k == "script":
            if own.get("script_set"):
                res["script"] = v
            continue
        if k == "number" and v is None:
            continue
        if k in ("files",) and v is None:
            continue
        if k in ("string", "noarch") and v is None:
            continue
        if isinstance(v, (list, dict)) and not v:
            continue
        if k == "skip":
            res["skip"] = bool(top.get("skip")) or bool(v)
            continue
        if k == "merge_build_and_host_envs":
            res[k] = bool(v) or bool(top.get(k))
            continue
        res[k] = v
    res["script_set"] = own.get("script_set") or top.get("script_set")
    return res


def _spec_list(ctx, lst):
    out = []
    for x in ctx.render_list(_as_list(lst)):
        if x is None:
            continue
        s = _s(x).strip()
        if s:
            out.append(s)
    return out


def _eval_requirements(ctx, req):
    req = req or {}
    out = {}
    for k in ("build", "host", "run", "run_constraints"):
        out[k] = _spec_list(ctx, req.get(k))
    rex = req.get("run_exports")
    rexd = {"weak": [], "strong": [], "noarch": [], "weak_constraints": [], "strong_constraints": []}
    if isinstance(rex, dict):
        for k in rexd:
            rexd[k] = _spec_list(ctx, rex.get(k))
    elif rex is not None:
        rexd["weak"] = _spec_list(ctx, rex)
    out["run_exports"] = rexd
    ire = req.get("ignore_run_exports") or {}
    out["ignore_run_exports"] = {
        "by_name": _spec_list(ctx, ire.get("by_name")),
        "from_package": _spec_list(ctx, ire.get("from_package")),
    }
    return out


def _eval_tests(ctx, tests, recipe_dir):
    out = []
    for t in ctx.preserve_list(_as_list(tests)):
        if not isinstance(t, dict):
            continue
        if "package_contents" in t:
            pc = ctx.render(t["package_contents"]) or {}
            out.append({"type": "package_contents", "spec": pc})
        elif "script" in t:
            sc = _eval_script(ctx, t["script"], recipe_dir, "none")
            reqs = t.get("requirements") or {}
            files = t.get("files") or {}
            out.append({
                "type": "script",
                "script": sc,
                "requirements": {
                    "build": _spec_list(ctx, reqs.get("build")),
                    "run": _spec_list(ctx, reqs.get("run")),
                },
                "files": {
                    "recipe": [_s(x) for x in ctx.render_list(files.get("recipe"))],
                    "source": [_s(x) for x in ctx.render_list(files.get("source"))],
                },
            })
        elif "python" in t:
            out.append({"type": "python", "spec": ctx.render(t["python"])})
        else:
            out.append({"type": next(iter(t.keys())), "spec": None})
    return out


def _eval_about(ctx, about):
    about = about or {}
    res = {}
    for k, v in about.items():
        if k == "license_file":
            res[k] = [render_template_late(ctx, x, LATE_LICENSE_VARS)
                      for x in ctx.preserve_list(_as_list(v))]
        else:
            try:
                res[k] = ctx.render(v)
            except RecipeError:
                res[k] = v
    return res


def _hash_of(variant_used):
    blob = json.dumps(variant_used, sort_keys=True, default=str).encode()
    return "h" + hashlib.sha1(blob).hexdigest()[:7]


def _used_variant(ctx, text, reqs):
    """Approximate rattler-build's 'used variant': variant keys referenced in
    the recipe text or matching a free (version-less) dependency name."""
    used = {}
    idents = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text))
    free = set()
    for k in ("build", "host"):
        for s in reqs.get(k, []):
            n, v, b = matchspec.split_spec(s)
            if v is None:
                free.add(normalize_key(n))
    for k, v in ctx.variant.items():
        nk = normalize_key(k)
        if nk in idents or nk in free or nk in ("target_platform",):
            used[k] = v
    for lang in ("c", "cxx", "fortran", "rust"):
        if "compiler('%s')" % lang in text or 'compiler("%s")' % lang in text:
            for suffix in ("_compiler", "_compiler_version"):
                if lang + suffix in ctx.variant:
                    used[lang + suffix] = ctx.variant[lang + suffix]
    used["target_platform"] = ctx.target_platform
    return used


class RenderedRecipe:
    def __init__(self, recipe_dir, platform, outputs, raw_text, warnings):
        self.recipe_dir = recipe_dir
        self.name = os.path.basename(recipe_dir.rstrip("/"))
        self.platform = platform
        self.outputs = outputs
        self.raw_text = raw_text
        self.warnings = warnings


def render_recipe(recipe_dir, target_platform, build_platform, variant):
    path = os.path.join(recipe_dir, "recipe.yaml")
    with open(path, encoding="utf-8") as f:
        raw_text = f.read()
    data = yaml.load(raw_text, Loader=_NoFloatLoader) or {}
    if not isinstance(data, dict):
        raise RecipeError("recipe.yaml is not a mapping")
    if data.get("schema_version") not in (None, 1):
        raise RecipeError("unsupported schema_version %r" % data.get("schema_version"))
    ctx = EvalContext(target_platform, build_platform, variant, recipe_dir)
    ctx.eval_context_section(data.get("context"))
    outputs = []
    if "outputs" in data:
        outputs = _render_multi(ctx, data, recipe_dir, raw_text)
    else:
        outputs = [_render_single(ctx, data, recipe_dir, raw_text)]
    outputs = [o for o in outputs if o is not None]
    return RenderedRecipe(recipe_dir, target_platform, outputs, raw_text, ctx.warnings)


def _finish_output(ctx, out, raw_text):
    out["variant_used"] = _used_variant(ctx, raw_text, out["requirements"])
    h = _hash_of(out["variant_used"])
    ctx2 = ctx
    out["hash"] = h
    number = out["build"].get("number") or 0
    out["build"]["number"] = number
    if out["build"].get("string") is None:
        out["build_string"] = "%s_%d" % (h, number)
    else:
        # ${{ hash }} is only known now (rendered as a placeholder before)
        out["build_string"] = out["build"]["string"].replace("hEFHASH", h)
    if out["build"].get("noarch"):
        out["subdir"] = "noarch"
    else:
        out["subdir"] = ctx2.target_platform
    return out


def _render_single(ctx, data, recipe_dir, raw_text):
    pkg = data.get("package") or {}
    name = _s(ctx.render(pkg.get("name")))
    version = _s(ctx.render(pkg.get("version")))
    if not name or version is None:
        raise RecipeError("package.name/version missing")
    ctx.vars.setdefault("name", name)
    ctx.vars.setdefault("version", version)
    # hash is late in rattler-build; give a stable provisional one
    ctx.vars.setdefault("hash", "hEFHASH")
    build = _eval_build(ctx, data.get("build"), recipe_dir, "default")
    if build["skip"]:
        return None
    out = {
        "kind": "package",
        "name": name,
        "version": version,
        "build": build,
        "sources": _expand_sources(ctx, data.get("source")),
        "requirements": _eval_requirements(ctx, data.get("requirements")),
        "tests": _eval_tests(ctx, data.get("tests"), recipe_dir),
        "about": _eval_about(ctx, data.get("about")),
        "inherit": None,
        "context": {k: _jsonable(ctx.vars.get(k)) for k in (data.get("context") or {})},
    }
    return _finish_output(ctx, out, raw_text)


def _jsonable(v):
    try:
        json.dumps(v)
        return v
    except (TypeError, ValueError):
        return str(v)


def _render_multi(ctx, data, recipe_dir, raw_text):
    top_recipe = data.get("recipe") or data.get("package") or {}
    top_name = _s(ctx.render(top_recipe.get("name"))) if top_recipe.get("name") else None
    top_version = _s(ctx.render(top_recipe.get("version"))) if top_recipe.get("version") is not None else None
    if top_name:
        ctx.vars.setdefault("name", top_name)
    if top_version:
        ctx.vars.setdefault("version", top_version)
    ctx.vars.setdefault("hash", "hEFHASH")
    top_build = _eval_build(ctx, data.get("build"), recipe_dir, "none")
    top_sources_raw = data.get("source")
    outputs = []
    staging_names = set()
    for raw in ctx.preserve_list(_as_list(data.get("outputs"))):
        if not isinstance(raw, dict):
            continue
        if "staging" in raw:
            st = raw["staging"] or {}
            sname = _s(ctx.render(st.get("name")))
            staging_names.add(sname)
            sbuild = _eval_build(ctx, raw.get("build"), recipe_dir, "none")
            sources = _expand_sources(ctx, top_sources_raw) + _expand_sources(ctx, raw.get("source"))
            out = {
                "kind": "staging",
                "name": sname,
                "version": top_version or "0",
                "build": sbuild,
                "sources": sources,
                "requirements": _eval_requirements(ctx, raw.get("requirements")),
                "tests": [],
                "about": {},
                "inherit": None,
                "context": {},
            }
            outputs.append(_finish_output(ctx, out, raw_text))
            continue
        pkg = raw.get("package") or {}
        name = _s(ctx.render(pkg.get("name")))
        version = _s(ctx.render(pkg.get("version"))) if pkg.get("version") is not None else top_version
        if version is None:
            raise RecipeError("output %s has no version" % name)
        inherit = raw.get("inherit")
        inh = None
        if inherit is not None:
            if isinstance(inherit, dict):
                inh = {"from": _s(ctx.render(inherit.get("from"))),
                       "run_exports": bool(inherit.get("run_exports", True))}
            else:
                inh = {"from": _s(ctx.render(inherit)), "run_exports": True}
        own_build = _eval_build(ctx, raw.get("build"), recipe_dir, "none")
        if inh is None:
            build = _merge_build(top_build, own_build)
        else:
            build = own_build
            for k in ("dynamic_linking", "prefix_detection"):
                if not build.get(k):
                    build[k] = top_build.get(k)
            for k in ("string", "noarch"):
                if build.get(k) is None:
                    build[k] = top_build.get(k)
            if build.get("number") is None:
                build["number"] = top_build.get("number")
            for k in ("always_copy_files", "always_include_files"):
                if not build.get(k):
                    build[k] = top_build.get(k)
            build["skip"] = bool(build.get("skip")) or bool(top_build.get("skip"))
        if build["script"] is None or build["script"].get("type") is None:
            build["script"] = {"type": "none", "path": None, "content": None, "env": {},
                               "interpreter": None, "cwd": None}
        if build["skip"]:
            continue
        sources = []
        if inh is None:
            sources += _expand_sources(ctx, top_sources_raw)
        sources += _expand_sources(ctx, raw.get("source"))
        tests = []
        if inh is None:
            tests += _eval_tests(ctx, data.get("tests"), recipe_dir)
        tests += _eval_tests(ctx, raw.get("tests"), recipe_dir)
        about = _eval_about(ctx, data.get("about"))
        about.update(_eval_about(ctx, raw.get("about")))
        out = {
            "kind": "package",
            "name": name,
            "version": version,
            "build": build,
            "sources": sources,
            "requirements": _eval_requirements(ctx, raw.get("requirements")),
            "tests": tests,
            "about": about,
            "inherit": inh,
            "context": {k: _jsonable(ctx.vars.get(k)) for k in (data.get("context") or {})},
        }
        outputs.append(_finish_output(ctx, out, raw_text))
    for o in outputs:
        if o["inherit"] and o["inherit"]["from"] not in staging_names:
            raise RecipeError("output %s inherits unknown staging %s" % (o["name"], o["inherit"]["from"]))
    return outputs


def quick_names(recipe_dir, target_platform, build_platform, variant):
    """Names (and versions) of the outputs a recipe provides."""
    r = render_recipe(recipe_dir, target_platform, build_platform, variant)
    return [(o["name"], o["version"]) for o in r.outputs if o["kind"] == "package"]
