from __future__ import division
from momi import Demography, expected_sfs, expected_total_branch_len, simulate_ms, sfs_list_from_ms, run_ms
import momi
import pytest
import random
import autograd.numpy as np
import scipy, scipy.stats
import itertools
import sys

from demo_utils import *

import os
try:
    ms_path = os.environ["MS_PATH"]
except KeyError:
    raise Exception("Need to set system variable $MS_PATH, or run py.test with 'MS_PATH=/path/to/ms py.test ...'")

def admixture_demo_str():
    x = np.random.normal(size=7)
    t = np.cumsum(np.exp(x[:5]))
    p = 1.0 / (1.0 + np.exp(x[5:]))

    ms_cmd = "-I 2 2 3 -es %f 2 %f -es %f 2 %f -ej %f 3 4 -ej %f 4 1 -ej %f 2 1" % (t[0], p[0], t[1], p[1], t[2], t[3], t[4])
    return ms_cmd
    
def exp_growth_str():
    n = 10
    growth_rate = random.uniform(-50,50)
    N_bottom = random.uniform(0.1,10.0)
    tau = .01
    demo_cmd = "-I 1 %d -G %f -eG %f 0.0" % (n, growth_rate, tau)
    return demo_cmd


def tree_demo_2_str():
    demo_cmd = "-I 2 5 5 -ej %f 2 1" % (2 * np.random.random() + 0.1,)
    return demo_cmd

def tree_demo_4_str():
    n = [2,2,2,2]

    times = np.random.random(len(n)-1) * 2.0 + 0.1
    for i in range(1,len(times)):
        times[i] += times[i-1]

    demo_cmd = "-I %d %s -ej %f 2 1 -ej %f 3 1 -ej %f 4 1" % tuple([len(n), " ".join(map(str,n))] + list(times))
    return demo_cmd

demo_funcs1 = (admixture_demo_str, exp_growth_str, tree_demo_2_str, tree_demo_4_str)
demo_funcs1 = {f.__name__ : f for f in demo_funcs1}

@pytest.mark.parametrize("k,folded",
                         ((fname, bool(b))
                          for fname,b in zip(demo_funcs1.keys(),
                                             np.random.choice([True,False],len(demo_funcs1)))))
def test_sfs_counts1(k, folded):
    """Test to make sure converting ms cmd to momi demography works"""
    check_sfs_counts(demo_ms_cmd=demo_funcs1[k](), folded=folded)

demo_funcs2 = {f.__name__ : f for f in [simple_admixture_demo, simple_two_pop_demo, piecewise_constant_demo, exp_growth_model]}
    
@pytest.mark.parametrize("k,folded",
                         ((fname, bool(b))
                          for fname,b in zip(demo_funcs2.keys(),
                                             np.random.choice([True,False],len(demo_funcs2)))))
def test_sfs_counts2(k,folded):
    """Test to make sure converting momi demography to ms cmd works"""
    check_sfs_counts(demo=demo_funcs2[k](), folded=folded)


def check_sfs_counts(demo_ms_cmd=None, demo=None, mu=1e-3, r=1e-3, num_loci=1000, ref_size=1e4, folded=False):
    assert (demo_ms_cmd is None) != (demo is None)

    if demo is None:
        demo = Demography.from_ms(ref_size,demo_ms_cmd)
        n = sum(demo.n_at_leaves)

        ms_cmd = "%d %d -t %f -r %f 1000 %s -seed %s" % (n, num_loci, ref_size*mu, ref_size*r,
                                                         demo_ms_cmd,
                                                         " ".join([str(random.randint(0,999999999))
                                                                   for _ in range(3)]))

        sfs_list = sfs_list_from_ms(run_ms(ms_cmd, ms_path = ms_path))
    elif demo_ms_cmd is None:        
        sfs_list = sfs_list_from_ms(simulate_ms(ms_path, demo, num_loci=num_loci, mu_per_locus=mu, additional_ms_params='-r %f 1000' % (ref_size * r), rescale=ref_size))

    if folded:
        sfs_list = [momi.util.folded_sfs(sfs, demo.n_at_leaves) for sfs in sfs_list]
        
    config_list = sorted(set(sum([sfs.keys() for sfs in sfs_list],[])))
    
    sfs_vals,branch_len = expected_sfs(demo, config_list, folded=folded), expected_total_branch_len(demo)
    theoretical = sfs_vals * mu

    observed = np.zeros((len(config_list), len(sfs_list)))
    for j,sfs in enumerate(sfs_list):
        for i,config in enumerate(config_list):
            try:
                observed[i,j] = sfs[config]
            except KeyError:
                pass

    labels = list(config_list)

    p_val = my_t_test(labels, theoretical, observed)
    print "p-value of smallest p-value under beta(1,num_configs)\n",p_val
    cutoff = 0.05
    #cutoff = 1.0
    assert p_val > cutoff


def my_t_test(labels, theoretical, observed, min_samples=25):

    assert theoretical.ndim == 1 and observed.ndim == 2
    assert len(theoretical) == observed.shape[0] and len(theoretical) == len(labels)

    n_observed = np.sum(observed > 0, axis=1)
    theoretical, observed = theoretical[n_observed > min_samples], observed[n_observed > min_samples, :]
    labels = np.array(map(str,labels))[n_observed > min_samples]
    n_observed = n_observed[n_observed > min_samples]

    runs = observed.shape[1]
    observed_mean = np.mean(observed,axis=1)
    bias = observed_mean - theoretical
    variances = np.var(observed,axis=1)

    t_vals = bias / np.sqrt(variances) * np.sqrt(runs)

    # get the p-values
    abs_t_vals = np.abs(t_vals)
    p_vals = 2.0 * scipy.stats.t.sf(abs_t_vals, df=runs-1)
    print("# labels, p-values, empirical-mean, theoretical-mean, nonzero-counts")
    toPrint = np.array([labels, p_vals, observed_mean, theoretical, n_observed]).transpose()
    toPrint = toPrint[np.array(toPrint[:,1],dtype='float').argsort()[::-1]] # reverse-sort by p-vals
    print(toPrint)

    print("Note p-values are for t-distribution, which may not be a good approximation to the true distribution")
    
    # p-values should be uniformly distributed
    # so then the min p-value should be beta distributed
    return scipy.stats.beta.cdf(np.min(p_vals), 1, len(p_vals))


if  __name__=="__main__":
    demo = Demography.from_ms(1.0," ".join(sys.argv[3:]))
    check_sfs_counts(demo, mu=float(sys.argv[2]), num_loci=int(sys.argv[1]))