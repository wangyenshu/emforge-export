# emforge-export

`ef-export` reads a checkout of
[emscripten-forge/recipes](https://github.com/emscripten-forge/recipes) and
writes a single bash script that builds one or more of its packages, and every
package they depend on, from the recipes. The script does not use conda,
mamba, pixi or rattler-build, and does not download packages from
prefix.dev, anaconda.org or any other conda channel.

```console
$ ./ef-export --clone -r recipes openjdk21     # writes build-openjdk21.sh
$ bash build-openjdk21.sh                      # builds 10 packages into ./ef-build
$ bash build-openjdk21.sh --tests              # also runs openjdk21's script tests
```

Contents:

- [Requirements](#requirements)
- [ef-export](#ef-export)
- [The generated script](#the-generated-script)
- [How a package is built](#how-a-package-is-built)
- [Dependency resolution](#dependency-resolution)
- [Native tools](#native-tools)
- [Network access](#network-access)
- [Differences from rattler-build](#differences-from-rattler-build)
- [Limitations](#limitations)
- [Source layout](#source-layout)

## Requirements

`ef-export`:

- Python 3.8 or later, with PyYAML and Jinja2
- git
- a checkout of emscripten-forge/recipes. Using older recipe revisions
  (see [Dependency resolution](#dependency-resolution)) needs its git history;
  `--clone` creates a suitable clone.

The generated script, on the build machine:

- bash, python3 3.8 or later with the `venv` module
- curl or wget, tar, gzip, xz, bzip2, git, patch
- a C compiler (`cc`), used only to build missing native tools from source
- the usual POSIX utilities

`bash build.sh --check` lists what is missing and the apt, dnf, brew or pacman
package that provides it. `--install-system-deps` installs those packages.

## ef-export

```
ef-export [options] PACKAGE...
```

`PACKAGE` is a recipe output name, optionally with a version spec:
`openjdk21`, `"python=3.13"`, `"numpy>=2,<2.4"`.

| option | meaning |
|---|---|
| `-r, --recipes DIR` | recipes checkout (default `./recipes`) |
| `--clone` | clone emscripten-forge/recipes into `DIR` if it does not exist (blobless, full history); fetch the history of an existing shallow clone |
| `-o, --output FILE` | output script (default `build-<package>.sh`) |
| `-m, --variant-config FILE` | variant config file, repeatable (default `<recipes>/variant.yaml`) |
| `--variant KEY=VALUE` | override one variant value |
| `--build-platform P` | platform the script will run on (default: this machine) |
| `--target-platform P` | default `emscripten-wasm32` |
| `--skip NAME` | do not build `NAME`; assume it is provided |
| `--no-history` | use only the checked-out recipes, never older revisions |
| `--strict-versions` | fail when no recipe revision satisfies a requirement (default: warning) |
| `--list` | print the build plan and exit |
| `--explain` | print the build plan and how each requirement was resolved, and exit |

Examples:

```console
$ ./ef-export -r recipes openjdk21 --explain
$ ./ef-export -r recipes regex numpy -o build.sh
$ ./ef-export -r recipes xeus-python --variant python=3.14 --skip pytester
```

## The generated script

```
bash build.sh [options] [PACKAGE...]
```

Without `PACKAGE` arguments the script builds the packages it was generated
for. With arguments it builds only those packages and their dependencies.

| option | meaning |
|---|---|
| `-C, --root DIR` | work root (default `$EF_ROOT`, else `./ef-build`) |
| `-p, --prefix DIR` | final install prefix (default `<root>/prefix`) |
| `-j, --jobs N` | value of `CPU_COUNT` (default: number of cores) |
| `-l, --list` | print the plan, with up-to-date status, and exit |
| `--only NAME` | build only `NAME`; its dependencies must already be built |
| `--rebuild NAME` | rebuild `NAME` even if up to date (repeatable) |
| `--force` | rebuild everything |
| `--tests` | run the script tests of the requested packages, building what the tests need |
| `--tests-all` | run the script tests of every package in the plan |
| `--skip-tests` | also skip `package_contents` tests |
| `--strict-tests` | a failing `package_contents` test stops the build (default: warning) |
| `--no-assemble` | do not create the final prefix |
| `--conda-pkgs` | also write `.tar.bz2` packages and `repodata.json` to `<root>/channel` |
| `--check` | check and provision native tools, then exit |
| `--tools MODE` | `auto` (default), `system` (never bootstrap), `bootstrap` (prefer pinned bootstraps) |
| `--install-system-deps` | install missing tools with the system package manager |
| `--offline` | do not download; use caches only |
| `--cache DIR` | download cache (default `$EF_CACHE`, else `~/.cache/emforge-export`) |
| `--source-cache DIR` | additional directory of pre-downloaded files, matched by file name or sha256 (repeatable) |
| `--isolation MODE` | `strict` (default): build scripts run under `env -i`; `none`: they inherit the caller's environment |
| `--keep-work` | keep the build directory of the last package |
| `--link-staging` | hard-link staging work directories instead of copying them (see [How a package is built](#how-a-package-is-built)) |
| `--extract DIR` | write the embedded recipe files to `DIR` and exit |

Environment variables: `EF_ROOT`, `EF_PREFIX`, `EF_CACHE`, `CPU_COUNT`;
`EF_MIRRORS` (space-separated URL templates containing `{sha256}` or
`{filename}`, tried before the original URL); `EF_PKG_CONFIG_SYSTEM=1`
(pkg-config also searches `/usr`); `EF_NO_GIT_FALLBACK=1` (disables the
GitHub archive fallback described under [Network access](#network-access)).

A package is rebuilt only when its inputs change: the rendered recipe, the
files of its recipe directory, or the inputs of its dependencies.

### Work root layout

```
<root>/
  prefix/     final installation: requested packages and their run dependencies
  pkgs/       one directory per built package (files/, info/)
  logs/       <package>.log, <package>.test.log, bootstrap-<tool>.log
  tools/      native tools built from source
  recipes/    recipe files unpacked from the script
  emcache/    emscripten cache (system libraries, ports), one per BUILD_PREFIX path
  bld/        work/, host_env/, build_env/ of the package being built
```

Downloads (sources, pip wheels, git clones) are stored in the download cache,
keyed by checksum, and shared between work roots. After a failed build,
`bld/` is kept; `cd <root>/bld/work && bash conda_build.sh` re-runs the
recipe's script in the same environment.

### Script structure

1. Header: build order, and every older recipe revision used, with the reason.
2. Runtime engine (bash) and runtime helper (Python; written to the work
   root on first use).
3. Native tool registry: system checks, pinned bootstrap sources and checksums.
4. One block per package: `ef_uN()` (metadata, dependencies, tools, tests),
   `ef_uN_sources()` (sources and patches), and the inline build script if the
   recipe has one.
5. After the final `exit`: every file of every recipe directory used, verbatim,
   each between a `#@ef-file <tag> <encoding> <mode> <path>` line and an
   `#@ef-end <tag>` line. Binary files are base64-encoded. This section is
   inside a here-document that is never executed, so `bash -n` and shellcheck
   can parse the whole script.

## How a package is built

For each package, in dependency order, the script performs the steps
rattler-build performs for one recipe output:

1. Sources. `url` sources are downloaded and checked against the recipe's
   sha256 or md5, `git` sources are cloned, and `path` sources are copied.
   Patches are applied with automatic strip-level detection (`patch`, then
   `git apply`).
2. Environments.
   - `$PREFIX` (host): the host requirements built earlier, plus pure-Python
     packages from PyPI where no recipe provides them.
   - `$BUILD_PREFIX`: a Python virtual environment, the native packages built
     from `recipes/recipes` (including the emscripten compiler package), and
     the native tools.
3. Build. The recipe script runs in `bld/work` with the variables
   rattler-build defines: `PREFIX`, `BUILD_PREFIX`, `SRC_DIR`, `RECIPE_DIR`,
   `BUILD_DIR`, `PKG_NAME`, `PKG_VERSION`, `PKG_BUILDNUM`,
   `PKG_BUILD_STRING`, `PKG_HASH`, `PYTHON`, `PY_VER`, `SP_DIR`,
   `STDLIB_DIR`, `CPU_COUNT`, `SHLIB_EXT`, `SUBDIR`, `target_platform`,
   `build_platform`, `CONDA_BUILD_CROSS_COMPILATION`, `CONDA_BUILD_STATE`,
   `SOURCE_DATE_EPOCH`, the `PIP_*` settings, and the used variant keys. The
   activation scripts of `$PREFIX` and then `$BUILD_PREFIX` are sourced; the
   emscripten package's activation script sets `CC`, `CXX`, `EMCC_CFLAGS`
   and `CMAKE_ARGS`. With `--isolation strict` the script starts from
   `env -i`, keeping only `PATH`, `http_proxy`, `https_proxy`, `no_proxy`
   (both cases), `SSL_CERT_FILE`, `SSL_CERT_DIR`, `REQUESTS_CA_BUNDLE`,
   `CURL_CA_BUNDLE`, `GIT_SSL_CAINFO`, `NODE_EXTRA_CA_CERTS`, `PIP_CERT`,
   `TMPDIR`, `DISPLAY`, `SSH_AUTH_SOCK` and `EF_PKG_CONFIG_SYSTEM`.
4. Packaging. Files in `$PREFIX` that were not installed as dependencies
   become the package, filtered by `build.files`. Text files that contain the
   build prefix path are rewritten to the install prefix on installation.
   Binary files that contain it are packaged unchanged; the build log reports
   their number.
5. Tests. `package_contents` tests run after each build. `script` tests run
   after all builds when `--tests` or `--tests-all` is given, each in a new
   test prefix (the package, its run dependencies, the test's run
   requirements) and a new build prefix (the test's build requirements).

Outputs of multi-output recipes that `inherit` a staging cache start from a
copy of the cache's prefix files and work directory. The copy uses
`cp --reflink=auto`, so it is copy-on-write on file systems that support it.
With `--link-staging` the work directory is hard-linked instead, and the
cached work directory is fingerprinted (paths, sizes, modification times)
before and after the output's script. If the script changed a cached file in
place, the cache is rebuilt before its next use.

Finally, the requested packages and their run dependencies are installed into
the final prefix, and PyPI run dependencies are installed into its
`site-packages`.

## Dependency resolution

Recipes come from two trees:

- `recipes/recipes_emscripten`: built for the target (`emscripten-wasm32`)
- `recipes/recipes`: built for the build machine (the emscripten compiler,
  cross-python, pytester, ...)

| requirement | resolved to |
|---|---|
| `build` | an output of `recipes/recipes`, else a native tool (see [Native tools](#native-tools)), else a PyPI package installed into `$BUILD_PREFIX` |
| `host` | an output of `recipes/recipes_emscripten`, else a `noarch` output of `recipes/recipes`, else (for Python packages) a pure-Python package from PyPI |
| `run`, `run_exports` | as `host`, installed together with the package |
| test `build` / `run` | as `build` / `run`; these packages are built only when tests run |

Variants: the script builds one variant, the first value of every key in the
variant config, respecting `zip_keys`. Use `--variant` to choose another
value. As in rattler-build, a variant value applies to a `build` or `host`
requirement that has no version spec. When a requirement has its own spec,
the variant value is used as a preference among versions that satisfy it.

Older recipe revisions: emscripten-forge's variant file pins versions that
the current recipe tree may no longer contain. At recipes commit
`2c445457ae89`, for example, it pins `python 3.13.*` while
`recipes/python` is 3.14.3, and `openssl 3.5.3` while `recipes/openssl` is
4.0.3. The conda channel still holds those older builds. Without a channel,
`ef-export` searches the git history of the recipe directory, newest first,
for the latest revision whose version satisfies both the requirement and the
variant value. It falls back to satisfying the requirement alone. A version
already chosen for the same package elsewhere in the plan is preferred, so
the plan uses one version per package where possible. The chosen revision's
directory is embedded in the script, and the header lists each choice:

```
# Older recipe revisions (from the git history of emscripten-forge/recipes),
# used where the current recipe does not satisfy a pin or requirement:
#   - openssl 3.5.3 from aa29ba1ced (2025-09-18): python 3.13.1 [...] needs openssl pin 3.5.3; current recipe has 4.0.3
#   - python 3.13.1 from fecf57a2ce (2026-02-18): regex 2022.6.2 needs python pin 3.13.*; current recipe has 3.14.3
```

Revisions are rendered with the current variant config. With `--no-history`,
or if the checkout has no history, unsatisfied requirements are reported as
warnings and the current recipe is used.

## Native tools

For each `build` requirement that no recipe provides, the script uses a
system executable if it exists and satisfies the version spec. Otherwise it
uses the first available fallback below. A version that comes only from a
variant value (for example `cmake 4.3`) is enforced when the tool is installed
from PyPI, and otherwise only noted in the log.

| fallback | tools |
|---|---|
| PyPI | cmake, meson, ninja, swig |
| pinned upstream source release (sha256-verified, built into `<root>/tools`) | autoconf, automake, bison, diffutils, file, findutils, flex, gawk, gettext, gperf, grep, help2man, libtool, m4, make, patch, pkg-config, rsync, sed, texinfo, unzip, zip, autoconf-archive, gnuconfig |
| Eclipse Temurin (api.adoptium.net, checksum from the API) | openjdk |
| nodejs.org release (pinned sha256) | nodejs |
| upstream release (headers / fonts only) | libcups (CUPS 2.4.11 headers), font-ttf-dejavu (DejaVu 2.37) |
| python.org source release (pinned sha256) | Python for `$BUILD_PREFIX`: 3.12.12, 3.13.9, 3.14.9 |
| shim script | ccache (runs the compiler directly), envsubst, wget (implemented with curl) |
| none: system only, missing ones are reported | bzip2, coreutils, curl, git, go, opam, openssl, perl, qt6-main, r-base, rust, tar, xz, yasm, and others |
| none: system library, checked with pkg-config or a header | cairo, gmp, gobject-introspection, gtest, libgit2, libpng, libtiff, lz4-c, ncurses, pari, pcre2, zlib, zstd |

A tool entry can list files that the conda-forge package provides and the
system installation must also have; currently only libtool does
(`share/aclocal/ltdl.m4`, which Ubuntu ships in `libltdl-dev`). Without them
the system tool is not used and the source build is. Requirements not in the registry are used
from `PATH` if present; otherwise the script prints a warning and continues.

## Network access

The script downloads the following. It never contacts a conda channel.

| what | from | verification |
|---|---|---|
| recipe `url` sources | the URLs in the recipe | sha256 or md5 from the recipe; none if the recipe gives none |
| recipe `git` sources | the repository in the recipe; shallow clone (depth 1, shallow submodules) unless the recipe sets a depth | `expected_commit` if the recipe gives one |
| emscripten | the `emscripten_emscripten-wasm32` recipe runs `emsdk install <version>`: emsdk from GitHub (latest commit of its default branch), prebuilt LLVM, binaryen, emscripten and node from `storage.googleapis.com/webassembly` | none: emsdk does not check checksums |
| emscripten ports | downloaded by emscripten itself during a build that uses e.g. `-sUSE_ZLIB=1`, from GitHub | sha512 by emscripten |
| PyPI packages | pypi.org, files.pythonhosted.org | none pinned; versions follow the recipe and variant specs |
| bootstrap tools, node, pip, Python | upstream release URLs (GNU tools via `ftpmirror.gnu.org`, then `ftp.gnu.org`) | pinned sha256 |
| JDK, if no suitable one is installed | the Eclipse Temurin download link returned by api.adoptium.net | checksum returned by the same API |

GitHub archive fallback: if a source URL of the form
`https://github.com/<owner>/<repo>/archive/...tar.gz` cannot be downloaded,
the script clones that tag or commit and recreates the tarball with
`git archive | gzip -n`, the method GitHub uses. The result must match the
recipe's sha256, or it is rejected. Set `EF_NO_GIT_FALLBACK=1` to disable
this. Emscripten ports are downloaded by emscripten, not by the script, and
have no such fallback.

`tests/netguard.py` is an HTTPS (CONNECT) proxy that logs each host
contacted and refuses prefix.dev, anaconda.org, anaconda.com, conda-forge.org
and emscripten-forge.org, including their subdomains. Every refused attempt
is logged.

```console
$ python3 tests/netguard.py --port 18080 --log hosts.txt &
$ export HTTPS_PROXY=http://127.0.0.1:18080 https_proxy=http://127.0.0.1:18080
$ export NO_PROXY=localhost,127.0.0.1 no_proxy=localhost,127.0.0.1
$ bash build-openjdk21.sh
```

netguard does not handle plain-HTTP proxying. Requests to `http://` URLs bypass
it unless `http_proxy` is also set to it, in which case they are refused.

## Differences from rattler-build

- One variant per script (see [Dependency resolution](#dependency-resolution)).
- Older recipe revisions from git history replace builds that a channel
  would provide.
- Requirements that emscripten-forge takes from conda-forge are provided by
  system tools, pinned source builds, or PyPI, as described above.
- `CMAKE_POLICY_VERSION_MINIMUM=3.5` is set in the build environment (a
  recipe may override it). Recipes last built with CMake 3 whose
  `cmake_minimum_required` is below 3.5 then configure with CMake 4.
- A failing `package_contents` test is a warning unless `--strict-tests` is
  given.
- For `emscripten-wasm32`, a `package_contents` `bin` entry also matches
  `bin/<name>` exactly, in addition to rattler-build's `bin/<name>.js` and
  `bin/<name>.wasm`. Some recipes list `xmlwf.wasm`.
- Test types other than `package_contents` and `script` are skipped, with a
  log message.
- Python files are not byte-compiled; `__pycache__/*.pyc` files created by
  a build are not packaged.
- Binary files containing the build prefix are not relocated.

## Limitations

- R packages: pure-R packages that emscripten-forge takes from conda-forge
  (r-lifecycle, r-cpp11, ...) and the native R that `cross-r-base` requires
  have no recipe. They are reported as unresolved, and R recipes that need
  them do not build.
- Native libraries and toolchains that some recipes take from conda-forge
  (LLVM/MLIR for flang, Qt host tools, cairo, ...) must be installed on the
  system. The script reports missing ones with the distribution package name.
- pytester, the test harness of emscripten-forge's Python packages, requires
  `pyjs_code_runner >= 3.2`, which is published on conda-forge but not on
  PyPI (PyPI has 3.0.0), and browsers installed by `playwright install`. Its
  test runs fail at that requirement; the packages themselves build.
- Not verified against pinned hashes: recipe sources without a checksum, git
  sources without `expected_commit`, PyPI downloads, and the emsdk downloads.
  The emscripten recipe clones the emsdk repository without a revision, so
  the emsdk version depends on when the build runs; the emscripten version
  itself is fixed by `emsdk install <version>`.
- Supported build platform: Linux x86-64. The code has paths for Linux
  aarch64 and macOS, but those platforms are not supported.

## Source layout

```
ef-export                   command-line entry point
ef_export/cli.py            argument parsing, output
ef_export/recipe.py         recipe rendering (context, jinja, conditionals, outputs)
ef_export/variant.py        variant config selection
ef_export/resolve.py        dependency resolution, version choice, test requirements
ef_export/history.py        older recipe revisions from git history
ef_export/tools.py          native tool registry, pinned bootstrap sources
ef_export/matchspec.py      conda version specs (also embedded in the script)
ef_export/generate.py       script generation, embedded file archive
ef_export/runtime/engine.sh runtime engine embedded in every script
ef_export/runtime/helper.py runtime helper embedded in every script
tests/netguard.py           logging/blocking HTTPS proxy
```
