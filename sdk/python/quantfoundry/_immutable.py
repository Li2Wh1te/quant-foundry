"""Small owned JSON trees can be shared safely by Pandas metadata copies.

This is an ordinary read-only API object, not a Python reflection sandbox.
Numeric values, schemas, scopes and resource checks are unchanged.
"""

def _readonly(*args, **kwargs):
    raise TypeError('查询和事实元数据是只读的')


class _FrozenDict(dict):
    __slots__ = ()
    __init__ = __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = __ior__ = _readonly

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        memo[id(self)] = self
        return self


class _FrozenList(list):
    __slots__ = ()
    __init__ = __setitem__ = __delitem__ = append = clear = extend = insert = pop = remove = reverse = sort = __iadd__ = __imul__ = _readonly

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        memo[id(self)] = self
        return self


def frozen_json(value):
    """Own a read-only snapshot without changing JSON dict/list shapes."""
    if isinstance(value, (_FrozenDict, _FrozenList)):
        return value
    if isinstance(value, dict):
        result = dict.__new__(_FrozenDict)
        dict.update(result, ((key, frozen_json(item)) for key, item in value.items()))
        return result
    if isinstance(value, list):
        result = list.__new__(_FrozenList)
        list.extend(result, (frozen_json(item) for item in value))
        return result
    if isinstance(value, tuple):
        return tuple(frozen_json(item) for item in value)
    return value
