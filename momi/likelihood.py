
from .util import make_constant, _minimize, _npstr, truncate0, check_psd, memoize_instance, mypartial, _get_stochastic_optimizer, count_calls, logger
import autograd.numpy as np
from .compute_sfs import expected_sfs, expected_total_branch_len
from .math_functions import einsum2, inv_psd
from .demography import DemographyError, Demography
from .data_structure import _sfs_subset
import scipy, scipy.stats
import autograd
from autograd import grad, hessian_vector_product, hessian
from collections import Counter
import random, functools, logging

class SfsLikelihoodSurface(object):
    def __init__(self, data, demo_func=None, mut_rate=None, folded=False, error_matrices=None, truncate_probs=1e-100, batch_size=200):
        self.data = data
        
        try: self.sfs = self.data.sfs
        except AttributeError: self.sfs = self.data
        
        self.demo_func = demo_func

        self.mut_rate = mut_rate
        self.folded = folded
        self.error_matrices = error_matrices

        self.truncate_probs = truncate_probs

        self.batch_size = batch_size
        self.sfs_batches = _build_sfs_batches(self.sfs, batch_size)
        #self.sfs_batches = [self.sfs]

    def log_likelihood(self, x, differentiable=True):       
        if self.demo_func:
            logger.debug("Computing log-likelihood at x = {0}".format(x))            
            demo = self.demo_func(*x)
        else:
            demo = x

        G,(diff_keys,diff_vals) = demo._get_graph_structure(), demo._get_differentiable_part()
        ret = 0.0
        for batch in self.sfs_batches:
            ret = ret + _log_lik(differentiable, diff_vals, diff_keys, G, batch, self.truncate_probs, self.folded, self.error_matrices)

        if self.mut_rate:
            ret = ret + _mut_factor(self.sfs, demo, self.mut_rate, False)

        logger.debug("log-likelihood = {0}".format(ret))
        return ret

    def kl_divergence(self, x, differentiable=True):
        log_lik = self.log_likelihood(x, differentiable)
        ret = -log_lik + self.sfs.n_snps() * self.sfs._entropy
        if self.mut_rate:
            ## KL divergence is for empirical distribution over all sites (monomorphic and polymorphic)
            ## taking the limit so that each site has infinitessimal width
            counts_i = self.sfs.n_snps(vector=True)
            mu = self.mut_rate * np.ones(len(counts_i))
            ret = ret + np.sum(-counts_i + counts_i * np.log(np.sum(counts_i) * mu / float(np.sum(mu))))

        ret = ret / float(self.sfs.n_snps())
        assert ret >= 0, "kl-div: %s, log_lik: %s, total_count: %s" % (str(ret), str(log_lik), str(self.sfs.n_snps()))

        return ret
    
    def find_optimum(self, x0, maxiter=100, bounds=None, method="newton", log_file=None, **kwargs):
        if log_file is not None:
            prev_level, prev_propagate = logger.level, logger.propagate
            logger.propagate = False
            logger.setLevel(min([logger.getEffectiveLevel(), logging.INFO]))
            log_file = logging.StreamHandler(log_file)
            logger.addHandler(log_file)

        try:
            if method=="newton":
                return self.newton(x0, maxiter, bounds, **kwargs)
            elif method=="adam":
                try: kwargs['n_chunks']
                except KeyError: raise TypeError("Argument n_chunks required for method='%s'"%method)

                return self.adam(x0, maxiter, bounds, **kwargs)
            else:
                raise ValueError("Unrecognized method %s" % method)
        except:
            raise
        finally:
            if log_file:
                logger.removeHandler(log_file)
                logger.propagate, logger.level = prev_propagate, prev_level
    
    def newton(self, x0, maxiter, bounds, opt_method='tnc',
               xtol=None, ftol=None, gtol=None, finite_diff_eps=None, subsample_steps=0, rgen=np.random):
        #options = {'ftol':ftol,'xtol':xtol,'gtol':gtol}
        options = {}
        for tolname,tolval in (('xtol',xtol), ('ftol',ftol), ('gtol',gtol)):
            if tolval is not None:
                options[tolname] = tolval
        if not finite_diff_eps: jac = True
        else:
            jac = False
            options['eps'] = finite_diff_eps

        def print_subsample_step(p, uniq, total):
            logger.info("Fitting on dataset with {tot} total SNPs, {uniq} unique SFS entries, making up {p} percent of the full data".format(tot=total, uniq=uniq, p=100.0*p))
            
        subsample_results = []
        for substep in reversed(list(range(1,subsample_steps+1))):
            p = (2.0)**(-substep)
            assert p <= .5

            subsample_counts = rgen.binomial(np.array(self.sfs._total_freqs, dtype=int), p*2.0)
            
            validation_counts = rgen.binomial(subsample_counts, .5)
            subsample_counts = subsample_counts - validation_counts

            print_subsample_step(p, np.sum(subsample_counts != 0), np.sum(subsample_counts))
            
            if np.sum(validation_counts) == 0 or np.sum(subsample_counts) == 0:
                continue
            
            sub_mutrate = self.mut_rate
            if sub_mutrate:
                sub_mutrate = np.sum(sub_mutrate * p * np.ones(self.sfs.n_loci))
            
            sub_lik, validation_lik = [SfsLikelihoodSurface(_sfs_subset(self.sfs, counts),
                                                            demo_func=self.demo_func, mut_rate=sub_mutrate,
                                                            folded=self.folded, error_matrices=self.error_matrices,
                                                            truncate_probs=self.truncate_probs, batch_size=self.batch_size)
                                       for counts in (subsample_counts, validation_counts)]

            res = _minimize(f=sub_lik.kl_divergence, start_params=x0, jac=jac, method=opt_method, maxiter=maxiter, bounds=bounds, options=options, f_name="KL-Divergence", f_validation=lambda x: validation_lik.kl_divergence(x, differentiable=False))

            x0 = res.x
            res.update({'p':p,
                        'uniq_snps': len(sub_lik.sfs._total_freqs),
                        'total_snps': np.sum(sub_lik.sfs._total_freqs)})
            subsample_results += [res]

        print_subsample_step(1.0, self.sfs.n_nonzero_entries, self.sfs.n_snps())
            
        ret = _minimize(f=self.kl_divergence, start_params=x0, jac=jac, method=opt_method, maxiter=maxiter, bounds=bounds, options=options, f_name="KL-Divergence")

        if subsample_steps:
            subsample_results.append(scipy.optimize.OptimizeResult(dict(list(ret.items()))))
            subsample_results[-1].update({'p':1.0,
                                          'uniq_snps': len(self.sfs._total_freqs),
                                          'total_snps': int(np.sum(self.sfs._total_freqs))})
            ret.subsample_results = subsample_results            
        return ret
            
    def adam(self, x0, maxiter, bounds, n_chunks, **kwargs):
        if len(self.sfs_batches) != 1:
            ## TODO: make this work, by making LikelihoodSurface for each minibatch, with the correct batch_size
            raise NotImplementedError("adam not yet implemented for finite SFS batch_size -- set batch_size=float('inf') in constructor")
        start_params = np.array(x0)
        
        f = lambda minibatch, params, minibatch_mut_rate: -_composite_log_likelihood(minibatch, self.demo_func(*params), minibatch_mut_rate, truncate_probs = self.truncate_probs, folded=self.folded, error_matrices=self.error_matrices)
        
        sgd_fun = _get_stochastic_optimizer("adam")
        
        return _sgd_composite_lik(sgd_fun, start_params, f, self.sfs, mut_rate=self.mut_rate, maxiter=maxiter, bounds=bounds, n_chunks=n_chunks, **kwargs)

    ## TODO: make this the main confidence region, deprecate the other one
    class _ConfidenceRegion(object):
        pass

def _build_sfs_batches(sfs, batch_size):
    _,counts = sfs._idxs_counts(locus=None)
    sfs_len = len(counts)
    
    if sfs_len <= batch_size:
        return [sfs]

    batch_size = int(batch_size)
    slices = []
    prev_idx, next_idx = 0, batch_size    
    while prev_idx < sfs_len:
        slices.append(slice(prev_idx, next_idx))
        prev_idx = next_idx
        next_idx = prev_idx + batch_size

    assert len(slices) == int(np.ceil(sfs_len / float(batch_size)))

    ## sort configs so that "(very) similar" configs end up in the same batch,
    ## thus avoiding redundant computation
    ## "similar" == configs have same num missing alleles
    ## "very similar" == configs are folded copies of each other
    
    a = sfs.configs.value[:,:,0] # ancestral counts
    d = sfs.configs.value[:,:,1] # derived counts
    n = a+d # totals

    n = list(map(tuple, n))
    a = list(map(tuple, a))
    d = list(map(tuple, d))
    
    folded = list(map(min, list(zip(a,d))))

    keys = list(zip(n,folded))
    sorted_idxs = sorted(list(range(sfs_len)), key=lambda i:keys[i])
    sorted_idxs = np.array(sorted_idxs, dtype=int)

    idx_list = [sorted_idxs[s] for s in slices]
    
    subcounts_list = []
    for idx in idx_list:
        curr = np.zeros(len(counts))
        curr[idx] = counts[idx]
        subcounts_list.append(curr)

    return [_sfs_subset(sfs, c) for c in subcounts_list]

## a decorator that rearranges gradient computations to be more memory efficient
## it turns the subroutine into a primitive so that its computations are not stored on the tape
## subroutine is computed by value_and_grad and its gradient stored, to avoid computing subroutine in both the forward and backward passes
def rearrange_gradient(subroutine):
    @count_calls # used in some tests to make sure autograd is calling inner_gradmaker
    def inner_gradmaker(ans, x, gx_container, *args, **kwargs):
        gx, = gx_container
        def inner_grad(g):
            if any([not t.complete for t in x.tapes]):
                raise NotImplementedError("For second order derivatives, use expected_sfs() directly instead of SfsLikelihoodSurface")
            return tuple(g*y for y in gx)
        return inner_grad

    @autograd.primitive
    def inner_fun(x, gx_container, *args, **kwargs):
        fx,gx = autograd.value_and_grad(subroutine)(x, *args, **kwargs)
        gx_container.append(gx)
        return fx
    inner_fun.defgrad(inner_gradmaker)

    def outer_fun(x, *args, **kwargs):
        return inner_fun(x, [], *args, **kwargs)
    functools.update_wrapper(outer_fun, subroutine)

    outer_fun.num_grad_calls = inner_gradmaker.num_calls
    outer_fun.reset_grad_count = inner_gradmaker.reset_count
    return outer_fun
            
def _pre_log_lik(diff_vals, diff_keys, G, data, truncate_probs, folded, error_matrices):
    demo = Demography(G, diff_keys, diff_vals)
    return _composite_log_likelihood(data, demo, truncate_probs=truncate_probs, folded=folded, error_matrices=error_matrices)

_log_lik_diff = rearrange_gradient(_pre_log_lik) # differentiable version of _log_lik
_log_lik_nodiff = autograd.primitive(_pre_log_lik) # non-differentiable version that is more efficient (since it doesnt use autograd.value_and_grad)

def _log_lik(differentiable, *args, **kwargs):
    if differentiable:
        return _log_lik_diff(*args, **kwargs)
    if not differentiable:
        return _log_lik_nodiff(*args, **kwargs)
    
def _composite_log_likelihood(data, demo, mut_rate=None, truncate_probs = 0.0, vector=False, **kwargs):
    """
    Returns the composite log likelihood for the data.

    Parameters
    ----------
    data : SegSites or Sfs
          if data.folded==True, functions returns composite log likelihood for folded SFS
    demo : Demography
    mut_rate : None or float or list of floats
           if None, function returns the multinomial composite log likelihood
           if float or list of floats, it is the mutation rate per locus, 
           and returns Poisson random field approximation.
           Note the Poisson model is not implemented for missing data.
    vector : boolean
           if False, return composite log-likelihood for the whole SFS
           if True, return list of the composite log-likelihood for each locus

    Other Parameters
    ----------------
    truncate_probs : float, optional
        Replace log(sfs_probs) with log(max(sfs_probs, truncate_probs)),
        where sfs_probs are the normalized theoretical SFS entries.
        Setting truncate_probs to a small positive number (e.g. 1e-100)
        will avoid taking log(0) due to precision or underflow error.
    **kwargs : additional arguments to pass to expected_sfs(), i.e. error_matrices, folded
    """
    try:
        sfs = data.sfs
    except AttributeError:
        sfs = data

    # if sfs.configs.has_monomorphic:
    #     raise ValueError("Need to remove monomorphic sites from dataset before computing likelihood")
    sfs_probs = np.maximum(expected_sfs(demo, sfs.configs, normalized=True, **kwargs),
                           truncate_probs)
    log_lik = sfs._integrate_sfs(np.log(sfs_probs), vector=vector)

    # add on log likelihood of poisson distribution for total number of SNPs
    if mut_rate is not None:
        log_lik = log_lik + _mut_factor(sfs, demo, mut_rate, vector)
        
    if not vector:
        log_lik = np.squeeze(log_lik)
    return log_lik

def _mut_factor(sfs, demo, mut_rate, vector):
    mut_rate = mut_rate * np.ones(sfs.n_loci)

    if sfs.configs.has_missing_data:
        raise NotImplementedError("Poisson model not implemented for missing data.")
    E_total = expected_total_branch_len(demo)
    lambd = mut_rate * E_total
    
    ret = -lambd + sfs.n_snps(vector=True) * np.log(lambd)
    if not vector:
        ret = np.sum(ret)
    return ret

### stuff for stochastic gradient descent
def _sgd_composite_lik(sgd_method, x0, lik_fun, data, n_chunks=None, random_generator=np.random, mut_rate=None, **sgd_kwargs):
    if n_chunks is None:
        raise ValueError("n_chunks must be specified")
    
    liks = _sgd_liks(lik_fun, data, n_chunks, random_generator, mut_rate)
    meta_lik = lambda x,minibatch: liks[minibatch](x)
    meta_lik.n_minibatches = len(liks)

    ret = sgd_method(meta_lik, x0, random_generator=random_generator, **sgd_kwargs)
    return ret

def _sgd_liks(lik_fun, data, n_chunks, rnd, mut_rate):
    try:
        sfs = data.sfs
    except AttributeError:
        sfs = data
    
    chunks = _subsfs_list(sfs, n_chunks, rnd)
    logger.info("Constructed %d minibatches for Stochastic Gradient Descent, with an average of %f total SNPs per minibatch, of which %f are unique" % (n_chunks, np.mean([chnk.n_snps() for chnk in chunks]), np.mean([chnk.n_nonzero_entries for chnk in chunks])))
    
    if mut_rate is not None:
        mut_rate = np.sum(mut_rate * np.ones(len(sfs.loci))) / float(n_chunks)

    # def lik_fun(sfs_chunk, params):
    #     return composite_log_likelihood(sfs_chunk, demo_func(*params), mut_rate=mut_rate, **kwargs)
    return [mypartial(lik_fun, chnk, minibatch_mut_rate=mut_rate) for chnk in chunks]

def _subsfs_list(sfs, n_chunks, rnd):
    #configs = sfs.configs
    
    #total_counts = np.array([sfs.freq(conf) for conf in configs], dtype=int)
    total_counts = sfs._total_freqs

    # row = SFS entry, column=chunk
    random_counts = np.array([rnd.multinomial(cnt_i, [1./float(n_chunks)]*n_chunks)
                              for cnt_i in total_counts], dtype=int)
    assert random_counts.shape == (len(total_counts), n_chunks)
    assert np.sum(random_counts) == np.sum(total_counts)
    
    return [_sfs_subset(sfs, column) for column in np.transpose(random_counts)]


### stuff for confidence intervals
class ConfidenceRegion(object):
    """
    Constructs asymptotic confidence regions and hypothesis tests,
    using the Limit of Experiments theory.
    """
    def __init__(self, point_estimate, demo_func, data, regime="long", **kwargs):
        """
        Parameters
        ----------
        point_estimate : array
                 a statistically consistent estimate for the true parameters.
                 confidence regions and hypothesis tests are computed for a (shrinking)
                 neighborhood around this point.
        demo_func : function that returns a Demography from parameters
        data : SegSites (or Sfs, if regime="many")
        regime : the limiting regime for the asymptotic confidence region
              if "long", number of loci is fixed, and the length of the loci -> infinity.
                 * uses time series information to estimate covariance structure
                 * requires isinstance(data, SegSites)
                 * loci should be independent. they don't have to be identically distributed
              if "many", the number of loci -> infinity
                 * loci should be independent, and roughly identically distributed
        **kwargs : additional arguments passed into composite_log_likelihood
        """
        if regime not in ("long","many"):
            raise ValueError("Unrecognized regime '%s'" % regime)
        
        self.point = np.array(point_estimate)
        self.demo_func = demo_func
        self.data = data
        self.regime = regime
        self.kwargs = kwargs

        self.score = autograd.grad(self.lik_fun)(self.point)
        self.score_cov = _observed_score_covariance(self.regime, self.point, self.data,
                                                   self.demo_func, **self.kwargs)
        self.fisher = _observed_fisher_information(self.point, self.data, self.demo_func,
                                                  assert_psd=False, **self.kwargs)
        
    def lik_fun(self, params, vector=False):
        """Returns composite log likelihood from params"""
        return _composite_log_likelihood(self.data, self.demo_func(*params), vector=vector, **self.kwargs)
    
    @memoize_instance
    def godambe(self, inverse=False):
        """
        Returns Godambe Information.
        If the true params are in the interior of the parameter space,
        the composite MLE will be approximately Gaussian with mean 0,
        and covariance given by the Godambe information.
        """
        fisher_inv = inv_psd(self.fisher)
        ret = check_psd(np.dot(fisher_inv, np.dot(self.score_cov, fisher_inv)))
        if not inverse:
            ret = inv_psd(ret)
        return ret

    def test(self, null_point, sims=int(1e3), test_type="ratio", alt_point=None, null_cone=None, alt_cone=None, p_only=True):
        """
        Returns p-value for a single or several hypothesis tests.
        By default, does a simple hypothesis test with the log-likelihood ratio.

        Note that for tests on the boundary, the MLE for the null and alternative
        models are often the same (up to numerical precision), leading to a p-value of 1

        Parameters
        ----------
        null_point : array or list of arrays
              the MLE of the null model
              if a list of points, will do a hypothesis test for each point
        sims : the number of Gaussian simulations to use for computing null distribution
              ignored if test_type="wald"
        test_type : "ratio" for likelihood ratio, "wald" for Wald test
              only simple hypothesis tests are implemented for "wald"

              For "ratio" test:
              Note that we set the log likelihood ratio to 0 if the two
              likelihoods are within numerical precision (as defined by numpy.isclose)

              For tests on interior of parameter space, it generally shouldn't happen
              to get a log likelihood ratio of 0 (and hence p-value of 1).
              But this can happen on the interior of the parameter space.

        alt_point : the MLE for the alternative models
              if None, use self.point (the point estimate used for this ConfidenceRegion)
              dimensions should be compatible with null_point
        null_cone, alt_cone : the nested Null and Alternative models
              represented as a list, whose length is the number of parameters
              each entry of the list should be in (None,0,1,-1)
                     None: parameter is unconstrained around the "truth"
                     0: parameter is fixed at "truth"
                     1: parameter can be >= "truth"
                     -1: parameter can be <= "truth"

              if null_cone=None, it is set to (0,0,...,0), i.e. totally fixed
              if alt_cone=None, it is set to (None,None,...), i.e. totally unconstrained
        p_only : bool
              if True, only return the p-value (probability of observing a more extreme statistic)
              if False, return 3 values per test:
                   [0] the p-value: (probability of more extreme statistic)
                   [1] probability of equally extreme statistic (up to numerical precision)
                   [2] probability of less extreme statistic

              [1] should generally be 0 in the interior of the parameter space.
              But on the boundary, the log likelihood ratio will frequently be 0,
              leading to a point mass at the boundary of the null distribution.
        """
        in_shape = np.broadcast(np.array(null_point), np.array(alt_point),
                                np.array(null_cone), np.array(alt_cone)).shape

        null_point = np.array(null_point, ndmin=2)

        if null_cone is None:
            null_cone = [0]*null_point.shape[1]
        null_cone = np.array(null_cone, ndmin=2)
        
        if alt_point is None:
            alt_point = self.point
        alt_point = np.array(alt_point, ndmin=2)
        
        if alt_cone is None:
            alt_cone = [None]*null_point.shape[1]
        alt_cone = np.array(alt_cone, ndmin=2)

        b = np.broadcast_arrays(null_point, null_cone, alt_point, alt_cone)
        try:
            assert all(bb.shape[1:] == (len(self.point),) for bb in b)
        except AssertionError:
            raise ValueError("points, cones have incompatible shapes")
        b = [list(map(tuple, x)) for x in b]
        null_point, null_cone, alt_point, alt_cone = b
        
        if test_type == "ratio":
            sims = np.random.multivariate_normal(self.score, self.score_cov, size=sims)
            
            liks = {}
            for p in list(null_point) + list(alt_point):
                if p not in liks:
                    liks[p] = self.lik_fun(np.array(p))

            sim_mls = {}
            for nc, ac in zip(null_cone, alt_cone):
                if (nc,ac) not in sim_mls:
                    nml, nmle = _project_scores(sims, self.fisher, nc)
                    aml, amle = _project_scores(sims, self.fisher, ac, init_vals=nmle)
                    sim_mls[(nc,ac)] = (nml,aml)

            ret = []
            for n_p,n_c,a_p,a_c in zip(null_point, null_cone, alt_point, alt_cone):
                lr = _trunc_lik_ratio(liks[n_p], liks[a_p])
                lr_distn = _trunc_lik_ratio(*sim_mls[(n_c,a_c)])
                ret += [list(map(np.mean, [lr > lr_distn,
                                      lr == lr_distn,
                                      lr < lr_distn]))]
            ret = np.array(ret)
        elif test_type == "wald":
            if np.any(np.array(null_cone) != 0) or any(a_c != tuple([None]*len(self.point)) for a_c in alt_cone):
                raise NotImplementedError("Only simple tests implemented for wald")

            gdmb = self.godambe(inverse=False)

            resids = np.array(alt_point) - np.array(null_point)
            ret = np.einsum("ij,ij->i", resids,
                            np.dot(resids, gdmb)) 
            ret = 1.-scipy.stats.chi2.cdf(ret, df=len(self.point))           
            ret = np.array([ret, [0]*len(ret), 1.-ret]).T
        else:
            raise NotImplementedError("%s tests not implemented" % test_type)

        if p_only:
            ret = ret[:,0]
        if len(in_shape) == 1:
            ret = np.squeeze(ret)
        return ret        

    def wald_intervals(self, lower=.025, upper=.975):
        """
        Marginal wald-type confidence intervals.
        """
        conf_lower, conf_upper = scipy.stats.norm.interval(.95,
                                                           loc = self.point,
                                                           scale = np.sqrt(np.diag(self.godambe(inverse=True))))
        return np.array([conf_lower, conf_upper]).T

def _trunc_lik_ratio(null, alt):
    return (1-np.isclose(alt,null)) * (null - alt)
    
def _observed_fisher_information(params, data, demo_func, assert_psd=True, **kwargs):
    params = np.array(params)
    f = lambda x: _composite_log_likelihood(data, demo_func(*x), **kwargs)
    ret = -autograd.hessian(f)(params)
    if assert_psd:
        try:
            ret = check_psd(ret)
        except AssertionError:
            raise Exception("Observed Fisher Information is not PSD (either due to numerical instability, or because the parameters are not a local maxima in the interior)")
    return ret

def _observed_score_covariance(method, params, seg_sites, demo_func, **kwargs):
    if method == "long":
        if "mut_rate" in kwargs:
            raise NotImplementedError("'long' godambe method not implemented for Poisson approximation")
        ret = _long_score_cov(params, seg_sites, demo_func, **kwargs)
    elif method == "many":
        ret = _many_score_cov(params, seg_sites, demo_func, **kwargs)
    else:
        raise Exception("Unrecognized method")

    try:
        ret = check_psd(ret)
    except AssertionError:
        raise Exception("Numerical instability: score covariance is not PSD")
    return ret
    
def _many_score_cov(params, data, demo_func, **kwargs):    
    params = np.array(params)

    def f_vec(x):
        ret = _composite_log_likelihood(data, demo_func(*x), vector=True, **kwargs)
        # centralize
        return ret - np.mean(ret)
    
    # g_out = einsum('ij,ik', jacobian(f_vec)(params), jacobian(f_vec)(params))
    # but computed in a roundabout way because jacobian implementation is slow
    def _g_out_antihess(x):
        l = f_vec(x)
        lc = make_constant(l)
        return np.sum(0.5 * (l**2 - l*lc - lc*l))
    return autograd.hessian(_g_out_antihess)(params)


def _long_score_cov(params, seg_sites, demo_func, **kwargs):
    if "mut_rate" in kwargs:
        raise NotImplementedError("Currently only implemented for multinomial composite likelihood")    
    params = np.array(params)
   
    configs = seg_sites.sfs.configs
    _,snp_counts = seg_sites.sfs._idxs_counts(None)
    weights = snp_counts / float(np.sum(snp_counts))
    
    def snp_log_probs(x):
        ret = np.log(expected_sfs(demo_func(*x), configs, normalized=True, **kwargs))
        return ret - np.sum(weights * snp_counts) # subtract off mean
       
    idx_series_list = [np.array(idxs) for idxs in seg_sites.idx_list]

    # g_out = sum(autocov(einsum("ij,ik->ikj",jacobian(idx_series), jacobian(idx_series))))
    # computed in roundabout way, in case jacobian is slow for many snps
    # autocovariance is truncated at sqrt(len(idx_series)), to avoid statistical/numerical issues
    def g_out_antihess(y):
        lp = snp_log_probs(y)
        ret = 0.0
        for idx_series in idx_series_list:
            L = len(idx_series)
            
            l = lp[idx_series]
            lc = make_constant(l)

            fft = np.fft.fft(l)
            # (assumes l is REAL)
            assert np.all(np.imag(l) == 0.0)
            fft_rev = np.conj(fft) * np.exp(2 * np.pi * 1j * np.arange(L) / float(L))

            curr = 0.5 * (fft * fft_rev - fft * make_constant(fft_rev) - make_constant(fft) * fft_rev)        
            curr = np.fft.ifft(curr)[(L-1)::-1]

            # make real
            assert np.allclose(np.imag(curr / L), 0.0)
            curr = np.real(curr)
            curr = curr[0] + 2.0 * np.sum(curr[1:int(np.sqrt(L))])
            ret = ret + curr
        return ret
    g_out = autograd.hessian(g_out_antihess)(params)
    g_out = 0.5 * (g_out + np.transpose(g_out))
    return g_out


def _project_scores(simulated_scores, fisher_information, polyhedral_cone, init_vals=None, method="tnc"):
    """
    Under usual theory, the score is asymptotically 
    Gaussian, with covariance == Fisher information.
    
    For Gaussian location model w~N(x,Fisher^{-1}),
    with location parameter x, the likelihood ratio
    of x vs. 0 is
    
    LR0 = x*z - x*Fisher*x / 2
    
    where the "score" z=Fisher*w has covariance==Fisher.
    
    This function takes a bunch of simulated scores,
    and returns LR0 for the corresponding Gaussian 
    location MLEs on a polyhedral cone.
    
    Input:
    simulated_scores: list or array of simulated values.
        For a correctly specified model in the interior
        of parameter space, this would be Gaussian with
        mean 0 and cov == Fisher information.
        For composite or misspecified model, or model
        on boundary of parameter space, this may have
        a different distribution.
    fisher information: 2d array
    polyhedral_cone: list
        length is the number of parameters
        entries are 0,1,-1,None
            None: parameter is unconstrained
            0: parameter == 0
            1: parameter is >= 0
            -1: parameter is <= 0
    """    
    if init_vals is None:
        init_vals = np.zeros(simulated_scores.shape)
    
    fixed_params = [c == 0 for c in polyhedral_cone]
    if any(fixed_params):
        if all(fixed_params):
            return np.zeros(init_vals.shape[0]), init_vals
        fixed_params = np.array(fixed_params)
        proj = np.eye(len(polyhedral_cone))[~fixed_params,:]
        fisher_information = np.dot(proj, np.dot(fisher_information, proj.T))
        simulated_scores = np.einsum("ij,kj->ik", simulated_scores, proj)
        polyhedral_cone = [c for c in polyhedral_cone if c != 0 ]
        init_vals = np.einsum("ij,kj->ik", init_vals, proj)
        
        liks, mles = _project_scores(simulated_scores, fisher_information, polyhedral_cone, init_vals, method)
        mles = np.einsum("ik,kj->ij", mles, proj)
        return liks,mles
    else:
        if all(c is None for c in polyhedral_cone):
           # solve analytically
           try:
               fisher_information = check_psd(fisher_information)
           except AssertionError:
               raise Exception("Optimization problem is unbounded and unconstrained")
           mles = np.linalg.solve(fisher_information, simulated_scores.T).T
           liks = np.einsum("ij,ij->i", mles, simulated_scores)
           liks = liks-.5*np.einsum("ij,ij->i", mles, 
                                    np.dot(mles, fisher_information))
           return liks,mles
        
        bounds = []
        for c in polyhedral_cone:
            assert c in (None,-1,1)
            if c == -1:
                bounds += [(None,0)]
            elif c == 1:
                bounds += [(0,None)]
            else:
                bounds += [(None,None)]
        
        assert init_vals.shape == simulated_scores.shape
        def obj(x):
            return -np.dot(z,x) + .5 * np.dot(x, np.dot(fisher_information, x))
        def jac(x):
            return -z + np.dot(fisher_information, x)
        sols = []
        for z,i in zip(simulated_scores,init_vals):
            sols += [scipy.optimize.minimize(obj, i, method=method, jac=jac, bounds=bounds)]
        liks = np.array([-s.fun for s in sols])
        mles = np.array([s.x for s in sols])
        return liks, mles

# def _simulate_log_lik_ratios(n_sims, cone0, coneA, score, score_covariance, fisher_information):
#     """
#     Returns a simulated asymptotic null distribution for the log likelihood
#     ratio.
#     Under the "usual" theory this distribution is known to be chi-square.
#     But for composite or misspecified likelihood, or parameter on the
#     boundary, this will have a nonstandard distribution.
    
#     Input:
#     n_sims : the number of simulated Gaussians
#     cone0, coneA: 
#         the constraints for the null, alternate models,
#         in a small neighborhood around the "truth".
        
#         cone is represented as a list,
#         whose length is the number of parameters,
#         with entries 0,1,-1,None.
#             None: model is unconstrained
#             0: model is fixed at "truth"
#             1: model can be >= "truth"
#             -1: model can be <= "truth"
#     score : the score.
#         Typically 0, but may be non-0 at the boundary
#     score_covariance : the covariance of the score
#         For correct model, this will be Fisher information
#         But this is not true for composite or misspecified likelihood
#     fisher_information : the fisher information
#     """
#     assert len(cone0) == len(score) and len(coneA) == len(score)
#     for c0,cA in zip(cone0,coneA):
#         if not all(c in (0,1,-1,None) for c in (c0,cA)):
#             raise Exception("Invalid cone")
#         if c0 != 0 and cA is not None and c0 != cA:
#             raise Exception("Null and alternative cones not nested")
#     score_sims = np.random.multivariate_normal(score, score_covariance, size=n_sims)
#     null_liks,null_mles = _project_scores(score_sims, fisher_information, cone0)    
#     alt_liks,alt_mles = _project_scores(score_sims, fisher_information, coneA, init_vals=null_mles)
#     return alt_liks, alt_mles, null_liks, null_mles
#     #ret = (1. - np.isclose(alt_liks, null_liks)) * (null_liks - alt_liks)
#     #assert np.all(ret <= 0.0)
#     #return ret
   
# def test_log_lik_ratio(null_lik, alt_lik, n_sims, cone0, coneA, score, score_covariance, fisher_information):
#     if alt_lik > 0 or null_lik > 0:
#         raise Exception("Log likelihoods should be non-positive.")
#     lik_ratio = _trunc_lik_ratio(null_lik, alt_lik)
#     if lik_ratio > 0:
#         raise Exception("Likelihood of full model is less than likelihood of sub-model")
#     alt_lik_distn,_,null_lik_distn,_ = _simulate_log_lik_ratios(n_sims, cone0, coneA, 
#                                                                score, score_covariance, fisher_information)
#     lik_ratio_distn = _trunc_lik_ratio(null_lik_distn, alt_lik_distn)
#     assert np.all(lik_ratio_distn <= 0.0)
#     return tuple(map(np.mean, [lik_ratio > lik_ratio_distn, 
#                                lik_ratio == lik_ratio_distn, 
#                                lik_ratio < lik_ratio_distn]))

# def log_lik_ratio_p(method, n_sims, unconstrained_mle, constrained_mle,
#                     constrained_params, seg_sites, demo_func, **kwargs):
#     unconstrained_mle, constrained_mle = (np.array(x) for x in (unconstrained_mle,
#                                                                 constrained_mle))

#     #sfs_list = seg_sites.sfs_list
#     #combined_sfs = sum_sfs_list(sfs_list)
    
#     log_lik_fun = lambda x: composite_log_likelihood(seg_sites, demo_func(*x))
#     alt_lik = log_lik_fun(unconstrained_mle)
#     null_lik = log_lik_fun(constrained_mle)
    
#     score = autograd.grad(log_lik_fun)(unconstrained_mle)
#     score_cov = observed_score_covariance(method, unconstrained_mle, seg_sites, demo_func)
#     fish = observed_fisher_information(unconstrained_mle, seg_sites, demo_func, assert_psd=False)

#     null_cone = [0 if c else None for c in constrained_params]
#     alt_cone = [None] * len(constrained_params)
    
#     return test_log_lik_ratio(null_lik, alt_lik, n_sims,
#                               null_cone, alt_cone,
#                               score, score_cov, fish)[0]
