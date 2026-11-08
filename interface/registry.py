"""The list of questions the interface can answer, and the checks every answer passes before it leaves.

A question ("feature") is one plain function in interface/features/, marked with @feature:

    @feature
    def how_many_devices() -> dict:
        \"\"\"Say how many devices are known.\"\"\"
        return {"count": 3}

The MCP server and the HTTP API both build themselves from this list, so a feature declared once appears in both,
and a feature switched off here disappears from both. Nothing else needs editing.

The rules, kept here so they hold for every feature:
  * parameters are str, int, float or bool, and have type hints (the front-ends turn them into a schema);
  * the function has a docstring (it is what the AI assistant or API user reads);
  * the function returns a dict that is valid JSON;
  * the answer never contains a secret kept in .env (see check_no_secrets).
Python 3.9+, standard library only.
"""
import inspect
import json
import math
import os
import typing
from dataclasses import dataclass

ALLOWED_TYPES = (str, int, float, bool)
DISABLE_ENV = "FLEET_INTERFACE_DISABLE"  # comma-separated feature names to switch off, in both front-ends
REQUIRED = inspect.Parameter.empty
MIN_SECRET_LENGTH = 8  # shorter values would match ordinary words and refuse harmless answers
MAX_STRING = 256       # the longest text a caller may pass in (device ids are at most 63)


class FeatureError(Exception):
    """A problem the caller can act on (the stack is down, a value is wrong). The message is shown as it is.
    `code` is the HTTP status the API uses: 400 for a bad request, 503 when the stack cannot answer."""

    def __init__(self, message, code=400):
        super().__init__(message)
        self.code = code


class SecretLeak(Exception):
    """An answer contained a secret. It is never returned."""


@dataclass(frozen=True)
class Param:
    name: str
    type: type
    default: object  # REQUIRED when the caller must give it

    @property
    def required(self):
        return self.default is REQUIRED


@dataclass(frozen=True)
class Feature:
    name: str
    summary: str       # the first line of the docstring: the one-line description
    description: str   # the whole docstring
    params: tuple
    func: typing.Callable
    module: str        # the file it lives in, for the index and for error messages


_features = {}
_secret_sources = []


def feature(func=None, *, name=None):
    """Declare a feature. Use as @feature, or @feature(name="other_name")."""

    def register(fn):
        feature_name = name or fn.__name__
        if feature_name in _features:
            raise ValueError(f"feature '{feature_name}' is declared twice "
                             f"({_features[feature_name].module} and {fn.__module__})")
        doc = inspect.getdoc(fn)
        if not doc:
            raise TypeError(f"feature '{feature_name}' needs a docstring: it is what the AI assistant or API user reads")
        try:
            hints = typing.get_type_hints(fn)
        except Exception as exc:
            raise TypeError(f"feature '{feature_name}': cannot read the type hints ({exc})")
        params = []
        for p in inspect.signature(fn).parameters.values():
            if p.kind not in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY):
                raise TypeError(f"feature '{feature_name}': '*{p.name}' is not allowed; name every parameter")
            if hints.get(p.name) not in ALLOWED_TYPES:
                raise TypeError(f"feature '{feature_name}': parameter '{p.name}' must have a type hint of "
                                f"str, int, float or bool")
            params.append(Param(p.name, hints[p.name], p.default))
        _features[feature_name] = Feature(feature_name, doc.splitlines()[0].strip(), doc, tuple(params), fn, fn.__module__)
        return fn

    return register(func) if func is not None else register


def load_features():
    """Import every file in interface/features/ (which declares their features) and return the enabled ones.
    Raises ValueError for a name in FLEET_INTERFACE_DISABLE that is not a feature: a typo in a safety switch must
    not pass silently."""
    from interface import features as package  # noqa: F401  (importing the package imports every file in it)
    return enabled()


def disabled_names():
    return [n.strip() for n in os.environ.get(DISABLE_ENV, "").split(",") if n.strip()]


def enabled():
    off = disabled_names()
    unknown = [n for n in off if n not in _features]
    if unknown:
        raise ValueError(f"{DISABLE_ENV} names {', '.join(unknown)}, which is not a feature. "
                         f"Features: {', '.join(sorted(_features))}")
    return [f for f in _features.values() if f.name not in off]


def get(name):
    for f in enabled():
        if f.name == name:
            return f
    return None


# ---------------------------------------------------------------- the checks every answer passes
def register_secret_source(fn):
    """fn() returns the secret values currently in use. The adapter registers one that reads .env."""
    _secret_sources.append(fn)


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _strings(v)


def check_no_secrets(result):
    """Refuse an answer that contains a secret kept in .env (a password, key or token). Features are written not to
    return one; this is the net underneath, so a mistake in a future feature cannot send a password to an AI."""
    secrets_in_use = {s for source in _secret_sources for s in source() if s and len(s) >= MIN_SECRET_LENGTH}
    if not secrets_in_use:
        return
    for text in _strings(result):
        if any(s in text for s in secrets_in_use):
            raise SecretLeak("this answer contained a secret from .env, so it was not returned. This is a bug in "
                             "the feature; it never sees or returns passwords on purpose.")


def call(name, **kwargs):
    """Run an enabled feature and check its answer. The one way both front-ends run a feature."""
    f = get(name)
    if f is None:
        raise FeatureError(f"no feature named '{name}'", code=404)
    known = {p.name for p in f.params}
    stray = sorted(set(kwargs) - known)
    if stray:
        raise FeatureError(f"'{name}' has no parameter {', '.join(stray)}. It takes: {', '.join(sorted(known)) or 'nothing'}")
    missing = [p.name for p in f.params if p.required and p.name not in kwargs]
    if missing:
        raise FeatureError(f"'{name}' needs: {', '.join(missing)}")
    for key, value in kwargs.items():
        if isinstance(value, str) and len(value) > MAX_STRING:
            raise FeatureError(f"'{key}' is too long (the limit is {MAX_STRING} characters)")
    result = f.func(**kwargs)
    if not isinstance(result, dict):
        raise TypeError(f"feature '{name}' ({f.module}) must return a dict, not {type(result).__name__}")
    try:
        json.dumps(result, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"feature '{name}' ({f.module}) returned something that is not valid JSON: {exc}")
    check_no_secrets(result)
    return result


_TYPE_WORDS = {int: "a whole number", float: "a number", bool: "true or false", str: "text"}


def coerce(f, raw):
    """Turn strings (an HTTP query) into the types a feature declares. Raises FeatureError on a value that does not fit,
    and on a name it does not take (a typo such as ?stat=offline must not quietly return everything)."""
    known = [p.name for p in f.params]
    stray = sorted(set(raw) - set(known))
    if stray:
        raise FeatureError(f"'{f.name}' has no parameter {', '.join(stray)}. It takes: {', '.join(known) or 'nothing'}")
    out = {}
    for p in f.params:
        if p.name not in raw:
            continue
        text = raw[p.name]
        try:
            if p.type is bool:
                if text.lower() not in ("true", "false", "1", "0"):
                    raise ValueError
                out[p.name] = text.lower() in ("true", "1")
            else:
                out[p.name] = p.type(text)
        except ValueError:
            raise FeatureError(f"'{p.name}' must be {_TYPE_WORDS[p.type]}, not {text!r}")
    return out


def from_json(f, arguments):
    """Check arguments that arrived as JSON values (the MCP server) against the declared types, strictly: a whole number
    is not text, true is not the number 1. Returns the keyword arguments, or raises FeatureError saying what is wrong."""
    if arguments is None:
        return {}
    if not isinstance(arguments, dict):
        raise FeatureError("the arguments must be an object of name: value pairs")
    kinds = {p.name: p.type for p in f.params}
    stray = sorted(set(arguments) - set(kinds))
    if stray:
        raise FeatureError(f"'{f.name}' has no parameter {', '.join(stray)}. It takes: {', '.join(kinds) or 'nothing'}")
    for name, value in arguments.items():
        wanted = kinds[name]
        ok = {str: isinstance(value, str),
              bool: isinstance(value, bool),
              int: isinstance(value, int) and not isinstance(value, bool),
              float: isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)}[wanted]
        if not ok:
            raise FeatureError(f"'{name}' must be {_TYPE_WORDS[wanted]}")
    return dict(arguments)


def describe(f):
    """What the HTTP index shows for one feature."""
    return {
        "name": f.name,
        "summary": f.summary,
        "description": f.description,
        "parameters": [{"name": p.name, "type": p.type.__name__, "required": p.required,
                        **({} if p.required else {"default": p.default})} for p in f.params],
        "url": f"/v1/{f.name}",
    }
