"""Random names like the ``names`` package, but with the lists loaded once.

``names.get_name`` opens and scans its distribution file on every call
(about 2 ms); anonymizing millions of rows spent hours there.
"""
import bisect
import random
from functools import lru_cache


@lru_cache(maxsize=None)
def _load(key):
    import names

    entries, cumulative = [], []
    with open(names.FILES[key]) as name_file:
        for line in name_file:
            name, _, cum, _ = line.split()
            entries.append(name.capitalize())
            cumulative.append(float(cum))
    return entries, cumulative


def _pick(key):
    entries, cumulative = _load(key)
    # same distribution as names.get_name: first entry above random() * 90
    index = bisect.bisect_right(cumulative, random.random() * 90)
    return entries[index] if index < len(entries) else entries[-1]


def get_first_name():
    return _pick(random.choice(("first:male", "first:female")))


def get_last_name():
    return _pick("last")


def get_full_name():
    return f"{get_first_name()} {get_last_name()}"
