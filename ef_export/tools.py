"""Registry of native build tools.

conda-forge packages that emscripten-forge recipes list under
`requirements.build` are mapped to providers that do not need any package
channel:

  system     an executable already on PATH whose version satisfies the spec
  pip        a PyPI distribution installed into BUILD_PREFIX (a venv)
  source     an upstream release tarball (pinned sha256) built from source
  download   an upstream binary release (pinned or API-verified sha256)
  shim       a tiny wrapper script written into BUILD_PREFIX/bin
  special    custom materialisation into the BUILD_PREFIX layout that
             recipes expect (e.g. $BUILD_PREFIX/lib/jvm, $BUILD_PREFIX/fonts)
"""
import re

from .recipe import KNOWN_PLATFORMS


# ---------------------------------------------------------------------------
# Upstream source releases used to bootstrap tools that are missing or too
# old on the build machine.  Checksums were verified when the registry was
# written; the generated build.sh re-verifies every download.
# ---------------------------------------------------------------------------
GNU = "https://ftp.gnu.org/gnu"
# GNU's mirror redirector is tried first (ftp.gnu.org itself is often slow)
GNU_MIRROR = "https://ftpmirror.gnu.org"
SOURCES = {
    "make-4.4.1": dict(url=GNU + "/make/make-4.4.1.tar.gz",
                       sha256="dd16fb1d67bfab79a72f5e8390735c49e3e8e70b4945a15ab1f81ddb78658fb3"),
    "m4-1.4.21": dict(url=GNU + "/m4/m4-1.4.21.tar.xz",
                      sha256="f25c6ab51548a73a75558742fb031e0625d6485fe5f9155949d6486a2408ab66"),
    "autoconf-2.72": dict(url=GNU + "/autoconf/autoconf-2.72.tar.xz",
                          sha256="ba885c1319578d6c94d46e9b0dceb4014caafe2490e437a0dbca3f270a223f5a",
                          needs=["m4"]),
    "automake-1.18.1": dict(url=GNU + "/automake/automake-1.18.1.tar.xz",
                            sha256="168aa363278351b89af56684448f525a5bce5079d0b6842bd910fdd3f1646887",
                            needs=["autoconf"]),
    "automake-1.16.5": dict(url=GNU + "/automake/automake-1.16.5.tar.xz",
                            sha256="f01d58cd6d9d77fbdca9eb4bbd5ead1988228fdb73d6f7a201f5f8d6b118b469",
                            needs=["autoconf"]),
    "libtool-2.5.4": dict(url=GNU + "/libtool/libtool-2.5.4.tar.xz",
                          sha256="f81f5860666b0bc7d84baddefa60d1cb9fa6fceb2398cc3baca6afaa60266675",
                          needs=["m4"]),
    "autoconf-archive-2024.10.16": dict(
        url=GNU + "/autoconf-archive/autoconf-archive-2024.10.16.tar.xz",
        sha256="7bcd5d001916f3a50ed7436f4f700e3d2b1bade3ed803219c592d62502a57363"),
    "bison-3.8.2": dict(url=GNU + "/bison/bison-3.8.2.tar.xz",
                        sha256="9bba0214ccf7f1079c5d59210045227bcf619519840ebfa80cd3849cff5a5bf2",
                        needs=["m4"]),
    "flex-2.6.4": dict(url="https://github.com/westes/flex/releases/download/v2.6.4/flex-2.6.4.tar.gz",
                       sha256="e87aae032bf07c26f85ac0ed3250998c37621d95f8bd748b31f15b33c45ee995",
                       needs=["m4"], env='CFLAGS="-O2 -D_GNU_SOURCE"'),
    "gperf-3.3": dict(url=GNU + "/gperf/gperf-3.3.tar.gz",
                      sha256="fd87e0aba7e43ae054837afd6cd4db03a3f2693deb3619085e6ed9d8d9604ad8"),
    "texinfo-7.2": dict(url=GNU + "/texinfo/texinfo-7.2.tar.xz",
                        sha256="0329d7788fbef113fa82cb80889ca197a344ce0df7646fe000974c5d714363a6"),
    "patch-2.8": dict(url=GNU + "/patch/patch-2.8.tar.xz",
                      sha256="f87cee69eec2b4fcbf60a396b030ad6aa3415f192aa5f7ee84cad5e11f7f5ae3"),
    "sed-4.9": dict(url=GNU + "/sed/sed-4.9.tar.xz",
                    sha256="6e226b732e1cd739464ad6862bd1a1aba42d7982922da7a53519631d24975181"),
    "gawk-5.4.1": dict(url=GNU + "/gawk/gawk-5.4.1.tar.xz",
                       sha256="07f6f7342b7febe4313fc2c2542ad93d64fe20ad8717200109f105a826f5fd37",
                       configure="--without-readline --without-mpfr"),
    "grep-3.12": dict(url=GNU + "/grep/grep-3.12.tar.xz",
                      sha256="2649b27c0e90e632eadcd757be06c6e9a4f48d941de51e7c0f83ff76408a07b9"),
    "diffutils-3.12": dict(url=GNU + "/diffutils/diffutils-3.12.tar.xz",
                           sha256="7c8b7f9fc8609141fdea9cece85249d308624391ff61dedaf528fcb337727dfd"),
    "findutils-4.10.0": dict(url=GNU + "/findutils/findutils-4.10.0.tar.xz",
                             sha256="1387e0b67ff247d2abde998f90dfbf70c1491391a59ddfecb8ae698789f0a4f5"),
    "help2man-1.49.3": dict(url=GNU + "/help2man/help2man-1.49.3.tar.xz",
                            sha256="4d7e4fdef2eca6afe07a2682151cea78781e0a4e8f9622142d9f70c083a2fd4f"),
    "gettext-0.26": dict(url=GNU + "/gettext/gettext-0.26.tar.xz",
                         sha256="d1fb86e260cfe7da6031f94d2e44c0da55903dbae0a2fa0fae78c91ae1b56f00",
                         configure="--disable-java --disable-csharp --disable-nls --without-emacs "
                                   "--disable-openmp --disable-acl --without-git --disable-curses"),
    "pkg-config-0.29.2": dict(url="https://pkgconfig.freedesktop.org/releases/pkg-config-0.29.2.tar.gz",
                              sha256="6fc69c01688c9458a57eb9a1664c9aba372ccda420a02bf4429fe610e7e7d591",
                              configure="--with-internal-glib --disable-host-tool",
                              env='CFLAGS="-O2 -std=gnu17 -Wno-int-conversion '
                                  '-Wno-incompatible-pointer-types"'),
    "rsync-3.4.1": dict(url="https://download.samba.org/pub/rsync/src/rsync-3.4.1.tar.gz",
                        sha256="2924bcb3a1ed8b551fc101f740b9f0fe0a202b115027647cf69850d65fd88c52",
                        # Fedora's lookaside cache keeps the upstream tarball byte for byte
                        mirrors=["https://src.fedoraproject.org/repo/pkgs/rsync/rsync-3.4.1.tar.gz/sha512/"
                                 "a3ecde4843ddb795308dca88581b868ac0221eb6f88a1477d7a9a2ecb4e4686042966bdd"
                                 "dbab40866f90a4715d3104daa7b83222ddf0f3387b796a86bde8e5c2/rsync-3.4.1.tar.gz"],
                        configure="--disable-openssl --disable-xxhash --disable-zstd "
                                  "--disable-lz4 --disable-md2man"),
    "file-5.46": dict(url="https://astron.com/pub/file/file-5.46.tar.gz",
                      sha256="c9cc77c7c560c543135edc555af609d5619dbef011997e988ce40a3d75d86088",
                      configure="--disable-libseccomp --disable-bzlib --disable-xzlib "
                                "--disable-zstdlib --disable-lzlib"),
    "zip-3.0": dict(url="https://downloads.sourceforge.net/project/infozip/Zip%203.x%20%28latest%29/3.0/zip30.tar.gz",
                    sha256="f0e8bb1f9b7eb0b01285495a2699df3a4b766784c1765a8f1aeedf63c0806369",
                    build="infozip"),
    "unzip-6.0": dict(url="https://downloads.sourceforge.net/project/infozip/UnZip%206.x%20%28latest%29/UnZip%206.0/unzip60.tar.gz",
                      sha256="036d96991646d0449ed0aa952e4fbe21b476ce994abc276e49d30e686708bd37",
                      build="infozip"),
    "python-3.12.12": dict(url="https://www.python.org/ftp/python/3.12.12/Python-3.12.12.tar.xz",
                           sha256="fb85a13414b028c49ba18bbd523c2d055a30b56b18b92ce454ea2c51edc656c4",
                           configure="--with-ensurepip=install"),
    "python-3.13.9": dict(url="https://www.python.org/ftp/python/3.13.9/Python-3.13.9.tar.xz",
                          sha256="ed5ef34cda36cfa2f3a340f07cac7e7814f91c7f3c411f6d3562323a866c5c66",
                          configure="--with-ensurepip=install"),
    "python-3.14.9": dict(url="https://www.python.org/ftp/python/3.14.9/Python-3.14.9.tar.xz",
                          sha256="e02d9ac21cebda6e46435369744a92c65cc7cd8669697694122cf97ffacd1e53",
                          configure="--with-ensurepip=install"),
    # data / header only
    "cups-2.4.11": dict(url="https://github.com/OpenPrinting/cups/releases/download/v2.4.11/cups-2.4.11-source.tar.gz",
                        sha256="9a88fe1da3a29a917c3fc67ce6eb3178399d68e1a548c6d86c70d9b13651fd71",
                        build="none"),
    "dejavu-fonts-2.37": dict(url="https://github.com/dejavu-fonts/dejavu-fonts/releases/download/version_2_37/dejavu-fonts-ttf-2.37.tar.bz2",
                              sha256="fa9ca4d13871dd122f61258a80d01751d603b4d3ee14095d65453b4e846e17d7",
                              build="none"),
}

NODE_VERSION = "22.23.4"
NODE_SHA256 = {
    "linux-x64": "1805bcd35e6815559bd2eb97db438d3fdb0eb3a5e6a977b7c1badca76337e9ce",
    "linux-arm64": "6287aa46b6c375d816e63ae66d024fbbfd70404825aabdfdf898a970871d2f4b",
    "darwin-x64": "f42abca7aaac61efc57d49fb42ea84fc53bbeba9a749cb03a291ce6e4e7a69c1",
    "darwin-arm64": "037650d0839f457747396f052073fb48333d31bf5abd3c98b74da20d89ebb1ad",
}
PIP_WHEEL = dict(
    url="https://files.pythonhosted.org/packages/3f/06/3e4420302363fbb71be3c7f35ce042f54bfca330e3e66b3a2e3c4cca579f/pip-26.2.2-py3-none-any.whl",
    sha256="fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70")


# ---------------------------------------------------------------------------
# Tools.  exe: candidate executables (first found wins); ver: how to print the
# version; src: bootstrap sources; pip: PyPI fallback; data: paths relative
# to the tool's install prefix that are mirrored into BUILD_PREFIX (conda env
# layout that recipes rely on); pkgs: distro package names for hints.
# ---------------------------------------------------------------------------

def urls_of(src):
    """All download URLs of a bootstrap source, preferred first."""
    u = src["url"]
    extra = list(src.get("mirrors") or [])
    if u.startswith(GNU + "/"):
        return [GNU_MIRROR + u[len(GNU):], u] + extra
    return [u] + extra


def L(pc=None, header=None, apt=None, dnf=None, brew=None, pacman=None):
    """A native library for BUILD_PREFIX, expected from the system: found
    through pkg-config module(s) <pc> or a header under /usr(/local)/include."""
    t = T(special="native_lib", apt=apt, dnf=dnf, brew=brew, pacman=pacman)
    t["pc"] = pc or []
    t["header"] = header
    return t


def T(exe=None, src=None, pip=None, data=None, special=None, ver="--version",
      apt=None, dnf=None, brew=None, pacman=None, shim=None, optional=False, need=None):
    """need: files (relative to the tool's prefix) a system installation
    must have to be used -- conda-forge packages often ship more than the
    distribution package of the same name."""
    return dict(exe=exe or [], src=src or [], pip=pip, data=data or [], special=special,
                ver=ver, shim=shim, optional=optional, need=need or [],
                pkgs=dict(apt=apt, dnf=dnf, brew=brew, pacman=pacman))


ACLOCAL_LIBTOOL = ["share/aclocal/libtool.m4", "share/aclocal/ltoptions.m4",
                   "share/aclocal/ltsugar.m4", "share/aclocal/ltversion.m4",
                   "share/aclocal/lt~obsolete.m4", "share/aclocal/ltargz.m4",
                   "share/aclocal/ltdl.m4"]

TOOLS = {
    "make": T(["make", "gmake"], ["make-4.4.1"], apt="make", dnf="make", brew="make", pacman="make"),
    "cmake": T(["cmake"], pip="cmake", apt="cmake", dnf="cmake", brew="cmake", pacman="cmake",
               data=[]),
    "ninja": T(["ninja", "ninja-build"], pip="ninja", apt="ninja-build", dnf="ninja-build",
               brew="ninja", pacman="ninja"),
    "meson": T(["meson"], pip="meson", apt="meson", dnf="meson", brew="meson", pacman="meson"),
    "m4": T(["m4", "gm4"], ["m4-1.4.21"], apt="m4", dnf="m4", brew="m4", pacman="m4"),
    "autoconf": T(["autoconf"], ["autoconf-2.72"], data=["share/autoconf"],
                  apt="autoconf", dnf="autoconf", brew="autoconf", pacman="autoconf"),
    "automake": T(["automake"], ["automake-1.18.1", "automake-1.16.5"],
                  data=["share/automake-*", "share/aclocal-*"],
                  apt="automake", dnf="automake", brew="automake", pacman="automake"),
    "libtool": T(["libtoolize", "glibtoolize"], ["libtool-2.5.4"],
                 data=["share/libtool"] + ACLOCAL_LIBTOOL,
                 need=["share/aclocal/libtool.m4", "share/aclocal/ltdl.m4"],
                 apt="libtool libtool-bin libltdl-dev", dnf="libtool libtool-ltdl-devel",
                 brew="libtool", pacman="libtool"),
    "autoconf-archive": T(special="aclocal_data", src=["autoconf-archive-2024.10.16"],
                          data=["share/aclocal/ax_*.m4"], apt="autoconf-archive",
                          dnf="autoconf-archive", brew="autoconf-archive",
                          pacman="autoconf-archive"),
    "gnuconfig": T(special="gnuconfig"),
    "pkg-config": T(["pkg-config", "pkgconf"], ["pkg-config-0.29.2"], special="pkg_config",
                    data=["share/aclocal/pkg.m4"],
                    apt="pkgconf", dnf="pkgconf-pkg-config", brew="pkgconf", pacman="pkgconf"),
    "bison": T(["bison"], ["bison-3.8.2"], apt="bison", dnf="bison", brew="bison", pacman="bison"),
    "flex": T(["flex"], ["flex-2.6.4"], apt="flex", dnf="flex", brew="flex", pacman="flex"),
    "gperf": T(["gperf"], ["gperf-3.3"], apt="gperf", dnf="gperf", brew="gperf", pacman="gperf"),
    "texinfo": T(["makeinfo"], ["texinfo-7.2"], apt="texinfo", dnf="texinfo", brew="texinfo",
                 pacman="texinfo"),
    "patch": T(["patch", "gpatch"], ["patch-2.8"], apt="patch", dnf="patch", brew="gpatch",
               pacman="patch"),
    "sed": T(["sed", "gsed"], ["sed-4.9"], apt="sed", dnf="sed", brew="gnu-sed", pacman="sed"),
    "gawk": T(["gawk", "awk"], ["gawk-5.4.1"], apt="gawk", dnf="gawk", brew="gawk", pacman="gawk"),
    "grep": T(["grep", "ggrep"], ["grep-3.12"], apt="grep", dnf="grep", brew="grep", pacman="grep"),
    "diffutils": T(["diff"], ["diffutils-3.12"], apt="diffutils", dnf="diffutils",
                   brew="diffutils", pacman="diffutils"),
    "findutils": T(["find"], ["findutils-4.10.0"], ver=None, apt="findutils", dnf="findutils",
                   brew="findutils", pacman="findutils"),
    "coreutils": T(["env", "install", "tr"], ver=None, apt="coreutils", dnf="coreutils",
                   brew="coreutils", pacman="coreutils"),
    "file": T(["file"], ["file-5.46"], apt="file", dnf="file", brew="libmagic", pacman="file"),
    "zip": T(["zip"], ["zip-3.0"], ver="-v", apt="zip", dnf="zip", brew="zip", pacman="zip"),
    "unzip": T(["unzip"], ["unzip-6.0"], ver="-v", apt="unzip", dnf="unzip", brew="unzip",
               pacman="unzip"),
    "rsync": T(["rsync"], ["rsync-3.4.1"], apt="rsync", dnf="rsync", brew="rsync", pacman="rsync"),
    "help2man": T(["help2man"], ["help2man-1.49.3"], apt="help2man", dnf="help2man",
                  brew="help2man", pacman="help2man"),
    "gettext": T(["msgfmt"], ["gettext-0.26"], data=["share/aclocal/gettext.m4",
                 "share/aclocal/iconv.m4", "share/aclocal/nls.m4", "share/aclocal/po.m4",
                 "share/aclocal/progtest.m4", "share/gettext"],
                 apt="gettext", dnf="gettext", brew="gettext", pacman="gettext"),
    "gettext-tools": T(["envsubst"], shim="envsubst", ver=None, apt="gettext-base",
                       dnf="gettext", brew="gettext", pacman="gettext"),
    "xz": T(["xz"], apt="xz-utils", dnf="xz", brew="xz", pacman="xz"),
    "bzip2": T(["bzip2"], ver=None, apt="bzip2", dnf="bzip2", brew="bzip2", pacman="bzip2"),
    "tar": T(["tar", "gtar"], apt="tar", dnf="tar", brew="gnu-tar", pacman="tar"),
    "curl": T(["curl"], apt="curl", dnf="curl", brew="curl", pacman="curl"),
    "wget": T(["wget"], apt="wget", dnf="wget", brew="wget", pacman="wget", shim="wget"),
    # listed by recipes that no longer use it (e.g. python): optional
    "f2c": T(["f2c"], ver=None, optional=True, apt="f2c", dnf="f2c", brew="f2c"),
    "git": T(["git"], apt="git", dnf="git", brew="git", pacman="git"),
    "perl": T(["perl"], ver="-e print$^V", apt="perl", dnf="perl", brew="perl", pacman="perl"),
    "ccache": T(["ccache"], shim="ccache", apt="ccache", dnf="ccache", brew="ccache",
                pacman="ccache"),
    "yasm": T(["yasm"], apt="yasm", dnf="yasm", brew="yasm", pacman="yasm"),
    "swig": T(["swig"], pip="swig", ver="-version", apt="swig", dnf="swig", brew="swig",
              pacman="swig"),
    "nodejs": T(["node", "nodejs"], special="nodejs", apt="nodejs", dnf="nodejs", brew="node",
                pacman="nodejs"),
    "openjdk": T(["javac"], special="openjdk", ver="-version", apt="openjdk-21-jdk-headless",
                 dnf="java-21-openjdk-devel", brew="openjdk@21", pacman="jdk21-openjdk"),
    "libcups": T(special="libcups", apt="libcups2-dev", dnf="cups-devel", brew="cups",
                 pacman="libcups"),
    "font-ttf-dejavu": T(special="dejavu", apt="fonts-dejavu-core fonts-dejavu-extra",
                         dnf="dejavu-fonts-all", brew="font-dejavu", pacman="ttf-dejavu"),
    "lld": T(["ld.lld"], special="lld", apt="lld", dnf="lld", brew="lld", pacman="lld"),
    "clang": T(["clang"], special="clang", apt="clang", dnf="clang", brew="llvm", pacman="clang"),
    "clangxx": T(["clang++"], special="clang", apt="clang", dnf="clang", brew="llvm",
                 pacman="clang"),
    "gcc": T(["gcc", "cc"], special="cc", apt="gcc", dnf="gcc", brew="gcc", pacman="gcc"),
    "gxx": T(["g++", "c++"], special="cc", apt="g++", dnf="gcc-c++", brew="gcc", pacman="gcc"),
    "gfortran": T(["gfortran"], apt="gfortran", dnf="gcc-gfortran", brew="gcc", pacman="gcc-fortran"),
    "rust": T(["rustc"], apt="rustc cargo", dnf="rust cargo", brew="rust", pacman="rust"),
    "go": T(["go"], ver="version", apt="golang", dnf="golang", brew="go", pacman="go"),
    "opam": T(["opam"], apt="opam", dnf="opam", brew="opam", pacman="opam"),
    "ghostscript": T(["gs"], apt="ghostscript", dnf="ghostscript", brew="ghostscript",
                     pacman="ghostscript"),
    "gengetopt": T(["gengetopt"], apt="gengetopt", dnf="gengetopt", brew="gengetopt"),
    "r-base": T(["R"], apt="r-base", dnf="R", brew="r", pacman="r"),
    "python": T(special="python"),
    "zlib": L(["zlib"], "zlib.h", apt="zlib1g-dev", dnf="zlib-devel", brew="zlib"),
    "cairo": L(["cairo"], "cairo/cairo.h", apt="libcairo2-dev", dnf="cairo-devel", brew="cairo",
               pacman="cairo"),
    "libpng": L(["libpng"], "png.h", apt="libpng-dev", dnf="libpng-devel", brew="libpng",
                pacman="libpng"),
    "libtiff": L(["libtiff-4"], "tiffio.h", apt="libtiff-dev", dnf="libtiff-devel", brew="libtiff",
                 pacman="libtiff"),
    "pcre2": L(["libpcre2-8"], "pcre2.h", apt="libpcre2-dev", dnf="pcre2-devel", brew="pcre2",
               pacman="pcre2"),
    "ncurses": L(["ncursesw", "ncurses"], "ncurses.h", apt="libncurses-dev", dnf="ncurses-devel",
                 brew="ncurses", pacman="ncurses"),
    "gobject-introspection": L(["gobject-introspection-1.0"], None, apt="gobject-introspection libgirepository1.0-dev",
                               dnf="gobject-introspection-devel", brew="gobject-introspection",
                               pacman="gobject-introspection"),
    "gmp": L(["gmp"], "gmp.h", apt="libgmp-dev", dnf="gmp-devel", brew="gmp", pacman="gmp"),
    "gtest": L(["gtest"], "gtest/gtest.h", apt="libgtest-dev", dnf="gtest-devel", brew="googletest",
               pacman="gtest"),
    "libgit2": L(["libgit2"], "git2.h", apt="libgit2-dev", dnf="libgit2-devel", brew="libgit2",
                 pacman="libgit2"),
    "lz4-c": L(["liblz4"], "lz4.h", apt="liblz4-dev", dnf="lz4-devel", brew="lz4", pacman="lz4"),
    "zstd": L(["libzstd"], "zstd.h", apt="libzstd-dev", dnf="libzstd-devel", brew="zstd",
              pacman="zstd"),
    "pari": L(None, "pari/pari.h", apt="libpari-dev pari-gp", dnf="pari-devel", brew="pari"),
    "qt6-main": T(["qmake6", "qtpaths6"], ver="-query QT_VERSION", apt="qt6-base-dev qt6-base-dev-tools",
                  dnf="qt6-qtbase-devel", brew="qt", pacman="qt6-base"),
    "openssl": T(["openssl"], ver="version", apt="libssl-dev", dnf="openssl-devel", brew="openssl"),
    "libiconv": T(special="native_lib", optional=True),
    "flatbuffers": T(["flatc"], apt="flatbuffers-compiler", dnf="flatbuffers-compiler",
                     brew="flatbuffers"),
    "libprotobuf": T(["protoc"], apt="protobuf-compiler", dnf="protobuf-compiler", brew="protobuf"),
    "typescript": T(["tsc"], special="npm:typescript"),
}

ALIASES = {
    "pkgconfig": "pkg-config",
    "pkgconf": "pkg-config",
    "gnu-make": "make",
    "cmake-no-system": "cmake",
    "awk": "gawk",
    "m4-gnu": "m4",
    "gxx_impl": "gxx",
    "gcc_impl": "gcc",
    "clang-tools": "clang",
    "clangdev": "clang",
    "binutils": "lld",
    "libtool-bin": "libtool",
    "git-lfs": "git",
}

# conda-forge names of Python distributions commonly needed at build time.
PYTHON_PACKAGES = {
    "pip", "setuptools", "wheel", "cython", "numpy", "scipy", "meson-python",
    "scikit-build-core", "scikit-build", "maturin", "setuptools-rust", "pybind11",
    "nanobind", "jinja2", "pyyaml", "toml", "tomli", "packaging", "hatchling",
    "hatch-vcs", "hatch-fancy-pypi-readme", "flit-core", "flit", "setuptools-scm",
    "setuptools_scm", "setuptools_scm_git_archive", "cffi", "pythran", "gmpy2",
    "cysignals", "cppy", "versioneer", "extension-helpers", "typing_extensions",
    "typing-extensions", "requests", "crossenv", "expandvars", "memory-allocator",
    "h5py", "pytorch", "uv-build", "poetry-core", "pdm-backend", "build",
    "installer", "ply", "sympy", "mako", "markupsafe", "six", "pkgconfig",
    "passagemath-setup", "passagemath-environment", "passagemath-abi",
    "astropy-base", "dask-core", "pytest", "pyproject-metadata", "calver",
    "trove-classifiers", "pluggy", "editables", "pathspec", "semantic-version",
    "setuptools-git-versioning", "oldest-supported-numpy", "cmake-build-extension",
    "py-cpuinfo", "beniget", "gast", "pybind11-global", "nanobind-abi",
}

PYPI_NAMES = {
    # conda's pybind11 installs headers / CMake files into the prefix
    "pybind11": "pybind11[global]",
    "pytorch": "torch",
    "setuptools_scm": "setuptools-scm",
    "setuptools_scm_git_archive": "setuptools-scm-git-archive",
    "typing_extensions": "typing-extensions",
    "astropy-base": "astropy",
    "dask-core": "dask",
    "pybind11-global": "pybind11",
}


def strip_platform(name):
    for p in KNOWN_PLATFORMS:
        suf = "_" + p
        if name.endswith(suf):
            return name[: -len(suf)]
    return name


def is_python_package(name):
    n = name.lower()
    return (n in PYTHON_PACKAGES or n.replace("_", "-") in PYTHON_PACKAGES
            or n.startswith("passagemath-") or n.startswith("py-"))


def pypi_name(name):
    return PYPI_NAMES.get(name, name)


def classify(name, native_platform):
    """-> ('tool', canonical) | ('pip', pypi-name) | None"""
    n = strip_platform(name.strip())
    if n.endswith("_impl"):
        n = n[:-5]
    n = ALIASES.get(n, n)
    if n in TOOLS:
        return ("tool", n)
    if is_python_package(n):
        return ("pip", pypi_name(n))
    return ("tool", n)


def tool_def(name):
    return TOOLS.get(name)


def sources_for(names):
    out = {}
    for n in names:
        t = TOOLS.get(n)
        if not t:
            continue
        for s in t["src"]:
            out[s] = SOURCES[s]
            for dep in SOURCES[s].get("needs", []):
                for s2 in TOOLS[dep]["src"]:
                    out[s2] = SOURCES[s2]
    return out


def src_version(src_id):
    return src_id.rsplit("-", 1)[1]


_ver_re = re.compile(r"\d+(?:\.\d+)+")
