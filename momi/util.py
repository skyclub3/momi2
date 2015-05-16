from __future__ import division
import functools
import autograd.numpy as np
from functools import partial
from autograd.core import primitive
import os
import itertools

def default_ms_path():
    return os.environ["MS_PATH"]

def aggregate_sfs(sfs_list, fun=lambda x: x):
    config_list = set(sum([sfs.keys() for sfs in sfs_list], []))
    def sfs_config(sfs,config):
        try:
            return sfs[config]
        except KeyError:
            return 0

    aggregate_config = lambda config: sum([fun(sfs_config(sfs,config)) for sfs in sfs_list])
    return {config: aggregate_config(config) for config in config_list}

def polymorphic_configs(demo):
    n = sum([demo.n_lineages(l) for l in demo.leaves])
    ranges = [range(demo.n_lineages(l)) for l in sorted(demo.leaves)]

    config_list = []
    for config in itertools.product(*ranges):
        if sum(config) == n or sum(config) == 0:
            continue
        config_list.append(config)
    return config_list

def check_symmetric(X):
    Xt = np.transpose(X)
    assert np.allclose(X, Xt)
    return 0.5 * (X + Xt)

@primitive
def truncate0(x, axis=None):
    '''make sure everything in x is non-negative'''
    mins = np.maximum(-np.amin(x,axis=axis), 0.0)
    maxes = np.maximum(np.amax(x, axis=axis), 1e-300)
    assert np.all(mins <= 1e-13 * maxes)

    if axis is not None:
        idx = [slice(None)] * x.ndim
        idx[axis] = np.newaxis
        mins = mins[idx]
    
    x[x < mins] = 0.0
    return x
truncate0.defgrad(lambda ans,x,axis=None: lambda g: g)    

@primitive
def set0(x, indices):
    y = np.array(x)
    y[indices] = 0
    return y
set0.defgrad(lambda ans,x,indices: lambda g: set0(g,indices))
    

@primitive
def make_constant(x):
    return x
make_constant.defgrad_is_zero()

def memoize(obj):
    cache = obj.cache = {}
    @functools.wraps(obj)
    def memoizer(*args, **kwargs):
        if args not in cache:
            cache[args] = obj(*args, **kwargs)
        return cache[args]
    return memoizer


class memoize_instance(object):
    """cache the return value of a method
    
    This class is meant to be used as a decorator of methods. The return value
    from a given method invocation will be cached on the instance whose method
    was invoked. All arguments passed to a method decorated with memoize must
    be hashable.
    
    If a memoized method is invoked directly on its class the result will not
    be cached. Instead the method will be invoked like a static method:
    class Obj(object):
        @memoize
        def add_to(self, arg):
            return self + arg
    Obj.add_to(1) # not enough arguments
    Obj.add_to(1, 2) # returns 3, result is not cached
    
    recipe from http://code.activestate.com/recipes/577452-a-memoize-decorator-for-instance-methods/
    """
    def __init__(self, func):
        self.func = func
    def __get__(self, obj, objtype=None):
        if obj is None:
            return self.func
        return partial(self, obj)
    def __call__(self, *args, **kw):
        obj = args[0]
        try:
            cache = obj.__cache
        except AttributeError:
            cache = obj.__cache = {}
        key = (self.func, args[1:], frozenset(kw.items()))
        try:
            res = cache[key]
        except KeyError:
            res = cache[key] = self.func(*args, **kw)
        return res
