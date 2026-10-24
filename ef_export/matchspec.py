"""Minimal conda MatchSpec / version handling.

Only what is needed to (a) split dependency strings into name/version/build,
(b) decide whether a version satisfies a spec, and (c) translate a conda
version spec into a pip requirement.  This module is also embedded verbatim
into the generated build.sh (runtime helper), so it must stay dependency free.
"""
import re

_NAME_RE = re.compile(r"^\s*([A-Za-z0-9_][A-Za-z0-9_.\-]*)\s*(.*)$")


def split_spec(spec):
    """'cmake >=3.20' -> ('cmake', '>=3.20', None); 'python 3.13.* *_cp313' ->
    ('python', '3.13.*', '*_cp313').  Bracket syntax name[version=..] is
    supported for the version key only."""
    spec = spec.strip()
    m = re.match(r"^([^\[\s]+)\[(.*)\]\s*$", spec)
    if m:
        name = m.group(1)
        ver = None
        build = None
        for part in re.split(r",(?=\s*\w+\s*=)", m.group(2)):
            k, _, v = part.partition("=")
            v = v.strip().strip("'\"")
            if k.strip() == "version":
                ver = v
            elif k.strip() == "build":
                build = v
        return name, ver, build
    m = _NAME_RE.match(spec)
    if not m:
        return spec, None, None
    name, rest = m.group(1), m.group(2).strip()
    if not rest:
        return name, None, None
    parts = rest.split()
    # 'name >= 1.2' (operator separated by a blank)
    if len(parts) >= 2 and parts[0] in ("<", ">", "<=", ">=", "==", "!=", "=", "~="):
        parts = [parts[0] + parts[1]] + parts[2:]
    ver = parts[0] if parts else None
    build = parts[1] if len(parts) > 1 else None
    return name, ver, build


def normalize_name(name):
    return name.strip().lower().replace("_", "-")


# --------------------------------------------------------------------------
# version comparison (conda VersionOrder, simplified)
# --------------------------------------------------------------------------
_SPECIAL = {"dev": -2, "_": -1, "post": 2}


def _components(v):
    v = v.strip().lower()
    epoch = 0
    if "!" in v:
        e, v = v.split("!", 1)
        try:
            epoch = int(e)
        except ValueError:
            epoch = 0
    v = v.split("+", 1)[0]
    out = []
    for part in re.split(r"[._\-]", v):
        if part == "":
            continue
        for tok in re.findall(r"\d+|[a-z]+|\*", part):
            if tok.isdigit():
                out.append((1, int(tok), ""))
            elif tok == "*":
                out.append((3, 0, "*"))
            else:
                # strings sort before numbers, except 'post'
                rank = _SPECIAL.get(tok)
                if rank is None:
                    out.append((0, 0, tok))
                elif rank > 0:
                    out.append((2, 0, tok))
                else:
                    out.append((-1, rank, tok))
    return epoch, out


def compare_versions(a, b):
    ea, ca = _components(str(a))
    eb, cb = _components(str(b))
    if ea != eb:
        return -1 if ea < eb else 1
    n = max(len(ca), len(cb))
    for i in range(n):
        x = ca[i] if i < len(ca) else (1, 0, "")
        y = cb[i] if i < len(cb) else (1, 0, "")
        if x != y:
            return -1 if x < y else 1
    return 0


def _startswith(version, prefix):
    pv = _components(prefix)[1]
    vv = _components(version)[1]
    pv = [c for c in pv if c[2] != "*"]
    if len(vv) < len(pv):
        vv = vv + [(1, 0, "")] * (len(pv) - len(vv))
    return vv[: len(pv)] == pv


def _match_one(version, cond):
    cond = cond.strip()
    if cond in ("", "*"):
        return True
    for op in ("==", "!=", ">=", "<=", "~=", ">", "<", "="):
        if cond.startswith(op):
            target = cond[len(op):].strip()
            break
    else:
        op, target = None, cond
    fuzzy = target.endswith("*")
    t = target.rstrip("*").rstrip(".")
    if op in (None, "="):
        # bare '1.2' and '=1.2' both mean 1.2.* in conda ('1.2' with no
        # operator in 'name 1.2' form); '1.2.*' is explicit
        if op is None and not fuzzy and "*" not in target:
            return _startswith(version, t) if t else True
        return _startswith(version, t) if t else True
    if op == "==":
        if fuzzy:
            return _startswith(version, t)
        return compare_versions(version, t) == 0
    if op == "!=":
        if fuzzy:
            return not _startswith(version, t)
        return compare_versions(version, t) != 0
    c = compare_versions(version, t)
    if op == ">=":
        return c >= 0
    if op == "<=":
        return c <= 0
    if op == ">":
        return c > 0
    if op == "<":
        return c < 0
    if op == "~=":
        parts = t.split(".")
        upper = ".".join(parts[:-1]) if len(parts) > 1 else t
        return c >= 0 and _startswith(version, upper)
    return True


def version_matches(version, spec):
    """True if `version` satisfies a conda version spec such as
    '>=1.2,<2', '1.2|1.3', '3.13.*', '21.*'."""
    if spec is None:
        return True
    spec = str(spec).strip().lstrip("~")
    if spec in ("", "*"):
        return True
    for alt in spec.split("|"):
        if all(_match_one(version, c) for c in alt.split(",")):
            return True
    return False


def conda_to_pip(name, spec):
    """Translate a conda version spec into a pip requirement string."""
    spec = (spec or "").strip().lstrip("~")
    if spec in ("", "*"):
        return name
    alt = spec.split("|")[0]
    out = []
    for c in alt.split(","):
        c = c.strip()
        if not c:
            continue
        m = re.match(r"^(==|!=|>=|<=|~=|>|<|=)?(.*)$", c)
        op, v = m.group(1), m.group(2).strip()
        if op in (None, "="):
            v = v.rstrip("*").rstrip(".")
            out.append("==%s.*" % v if v else "")
        elif op in ("==", "!=") and v.endswith("*"):
            out.append("%s%s" % (op, v if v.endswith(".*") else v[:-1] + ".*"))
        else:
            out.append(op + v)
    out = [o for o in out if o]
    return name + ",".join(out)


def first_version_in(text):
    m = re.search(r"(\d+(?:\.\d+)+[a-z0-9.]*|\d+)", text or "")
    return m.group(1) if m else None
