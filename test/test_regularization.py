from scipy.optimize import minimize
import math
import random
import autograd.numpy as np
from autograd.numpy import sum
from autograd import grad, hessian_vector_product

from momi import make_demography, simulate_ms, sfs_list_from_ms, sum_sfs_list
from momi import unlinked_log_likelihood
from momi import unlinked_mle_search

def test_regularization():
    num_runs = 1000
    mu=10.0
    def get_demo(t):
        print t
        t0,t1 = t
        return make_demography("-I 3 5 5 5 -ej $0 1 2 -ej $1 2 3", np.exp(t0),np.exp(t0) + np.exp(t1))
    true_x = np.array([np.log(.5),np.log(.2)])
    true_demo = get_demo(true_x)

    sfs = sum_sfs_list(sfs_list_from_ms(simulate_ms(true_demo, num_sims=num_runs, mu=mu),
                                        true_demo.n_at_leaves))
    
    optimize_res = unlinked_mle_search(sfs, get_demo, mu * num_runs, np.array([np.log(0.1),np.log(100.0)]), hessp=True, method='newton-cg')
    print optimize_res
    
    inferred_x = optimize_res.x
    error = (true_x - inferred_x) / true_x
    print "# Truth:\n", true_x
    print "# Inferred:\n", inferred_x
    print "# Max Relative Error: %f" % max(abs(error))
    print "# Relative Error:","\n", error

    assert max(abs(error)) < .1

if __name__=="__main__":
    test_regularization()
