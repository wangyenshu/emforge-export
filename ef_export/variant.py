"""Variant configuration (variant.yaml) handling.

rattler-build builds the cartesian product of all *used* variant keys.  A
standalone build script builds exactly one variant, so we pick, for every
key, the first value (respecting zip_keys) unless the user overrides it.
"""
import yaml

from .recipe import EvalContext, _NoFloatLoader, normalize_key


def _scalar(v):
    if isinstance(v, bool):
        return v
    if v is None:
        return None
    return str(v)


def load_variant_files(paths, target_platform, build_platform, overrides=None):
    merged = {}
    zip_keys = []
    for p in paths:
        with open(p, encoding="utf-8") as f:
            data = yaml.load(f, Loader=_NoFloatLoader) or {}
        ctx = EvalContext(target_platform, build_platform, {})
        for k, v in data.items():
            if k == "zip_keys":
                for group in v or []:
                    zip_keys.append([str(x) for x in group])
                continue
            if isinstance(v, dict):
                # pin_run_as_build and friends: not relevant for a single build
                continue
            if isinstance(v, list):
                vals = ctx.render_list(v, drop_empty=False)
                vals = [_scalar(x) for x in vals]
            else:
                vals = [_scalar(ctx.render(v))]
            if not vals:
                continue
            merged[k] = vals
    return select_variant(merged, zip_keys, overrides or {}), merged, zip_keys


def select_variant(all_values, zip_keys, overrides):
    chosen_index = {}
    norm_over = {normalize_key(k): v for k, v in overrides.items()}
    # an override picks the index inside its zip group
    for group in zip_keys:
        idx = 0
        for key in group:
            ov = norm_over.get(normalize_key(key))
            vals = all_values.get(key, [])
            if ov is not None and ov in vals:
                idx = vals.index(ov)
        for key in group:
            chosen_index[key] = idx
    variant = {}
    for key, vals in all_values.items():
        i = chosen_index.get(key, 0)
        if i >= len(vals):
            i = 0
        variant[key] = vals[i]
    for k, v in overrides.items():
        # override replaces the key (or adds it)
        hit = [key for key in variant if normalize_key(key) == normalize_key(k)]
        if hit:
            for key in hit:
                variant[key] = v
        else:
            variant[k] = v
    return variant
