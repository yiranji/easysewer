"""Cooperative operations on preserved JSON trees, without changing wire values.

Plain dictionaries/lists get bounded work checks. Other values retain Python's
ordinary copy/equality semantics at call boundaries. This is not a validator
and does not make arbitrary extension code preemptible.
"""
from copy import deepcopy
from ...validation._cooperative import _active, checkpoint, checkpointed


def copy_json(value):
    if _active.get() is None:
        return deepcopy(value)
    memo = {}

    def walk(item):
        kind = type(item)
        if kind in (str, int, float, bool, type(None)):
            return item
        identity = id(item)
        if identity in memo:
            return memo[identity]
        if kind is list:
            result = []
            memo[identity] = result
            memo.setdefault(id(memo), []).append(item)
            result.extend(walk(child) for child in checkpointed(item))
            return result
        if kind is dict:
            result = {}
            memo[identity] = result
            memo.setdefault(id(memo), []).append(item)
            for key, child in checkpointed(item.items()):
                result[walk(key)] = walk(child)
            return result
        checkpoint()
        result = deepcopy(item, memo)
        checkpoint()
        return result

    checkpoint()
    result = walk(value)
    checkpoint()
    return result


def equal_json(left, right):
    if _active.get() is None:
        return left == right

    def same(a, b):
        if type(a) is type(b) and type(a) in (dict, list):
            if a is b:
                return True
            if len(a) != len(b):
                return False
            if type(a) is dict:
                for key, value in checkpointed(a.items()):
                    if key not in b:
                        return False
                    other = b[key]
                    if value is not other and not same(value, other):
                        return False
            else:
                for value, other in checkpointed(zip(a, b)):
                    if value is not other and not same(value, other):
                        return False
            return True
        # Scalars need no extra callback for every leaf; their enclosing
        # container already checks bounded batches. Keep Python equality.
        scalar = (str, int, float, bool, type(None))
        if type(a) in scalar and type(b) in scalar:
            return a == b
        checkpoint()
        result = a == b
        checkpoint()
        return result

    checkpoint()
    result = same(left, right)
    checkpoint()
    return result
