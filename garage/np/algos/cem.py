from itertools import chain, zip_longest

import numpy as np

from garage.core import Serializable
from garage.logger import logger, snapshotter, tabular
from garage.misc.special import discount_cumsum
from garage.np.algos.base import RLAlgorithm
from garage.plotter import Plotter
from garage.sampler import parallel_sampler, stateful_pool
from garage.sampler.utils import rollout


def _get_stderr_lb(x):
    mu = np.mean(x, 0)
    stderr = np.std(x, axis=0, ddof=1 if len(x) > 1 else 0) / np.sqrt(len(x))
    return mu - stderr


def _get_stderr_lb_varyinglens(x):
    mus, stds, ns = [], [], []
    for temp_list in zip_longest(*x, fillvalue=np.nan):
        mus.append(np.nanmean(temp_list))
        n = len(temp_list) - np.sum(np.isnan(temp_list))
        stds.append(np.nanstd(temp_list, ddof=1 if n > 1 else 0))
        ns.append(n)
    return np.array(mus) - np.array(stds) / np.sqrt(ns)


def _worker_rollout_policy(g, args):
    sample_std = args['sample_std'].flatten()
    cur_mean = args['cur_mean'].flatten()
    n_evals = args['n_evals']
    k = len(cur_mean)
    params = np.random.standard_normal(k) * sample_std + cur_mean
    g.policy.set_param_values(params)
    paths, returns, undiscounted_returns = [], [], []
    for _ in range(n_evals):
        path = rollout(g.env, g.policy, args['max_path_length'])
        path['returns'] = discount_cumsum(path['rewards'], args['discount'])
        path['undiscounted_return'] = sum(path['rewards'])
        paths.append(path)
        returns.append(path['returns'])
        undiscounted_returns.append(path['undiscounted_return'])

    result_path = {'full_paths': paths}
    result_path['undiscounted_return'] = _get_stderr_lb(undiscounted_returns)
    result_path['returns'] = _get_stderr_lb_varyinglens(returns)

    # not letting n_evals count towards below cases since n_evals is multiple
    # eval for single paramset
    if args['criterion'] == 'samples':
        inc = len(path['rewards'])
    elif args['criterion'] == 'paths':
        inc = 1
    else:
        raise NotImplementedError
    return (params, result_path), inc


class CEM(RLAlgorithm, Serializable):
    def __init__(self,
                 env,
                 policy,
                 n_itr=500,
                 max_path_length=500,
                 discount=0.99,
                 init_std=1.,
                 n_samples=100,
                 batch_size=None,
                 best_frac=0.05,
                 extra_std=1.,
                 extra_decay_time=100,
                 plot=False,
                 n_evals=1,
                 **kwargs):
        """
        :param n_itr: Number of iterations.
        :param max_path_length: Maximum length of a single rollout.
        :param batch_size: # of samples from trajs from param distribution,
         when this is set, n_samples is ignored
        :param discount: Discount.
        :param plot: Plot evaluation run after each iteration.
        :param init_std: Initial std for param distribution
        :param extra_std: Decaying std added to param distribution at each
         iteration
        :param extra_decay_time: Iterations that it takes to decay extra std
        :param n_samples: #of samples from param distribution
        :param best_frac: Best fraction of the sampled params
        :param n_evals: # of evals per sample from the param distr. returned
         score is mean - stderr of evals
        :return:
        """
        Serializable.quick_init(self, locals())
        self.env = env
        self.policy = policy
        self.batch_size = batch_size
        self.plot = plot
        self.extra_decay_time = extra_decay_time
        self.extra_std = extra_std
        self.best_frac = best_frac
        self.n_samples = n_samples
        self.init_std = init_std
        self.discount = discount
        self.max_path_length = max_path_length
        self.n_itr = n_itr
        self.n_evals = n_evals
        self.plotter = Plotter()

    def train(self):
        parallel_sampler.populate_task(self.env, self.policy)
        if self.plot:
            self.plotter.init_plot(self.env, self.policy)

        cur_std = self.init_std
        cur_mean = self.policy.get_param_values()
        # K = cur_mean.size
        n_best = max(1, int(self.n_samples * self.best_frac))

        for itr in range(self.n_itr):
            # sample around the current distribution
            extra_var_mult = max(1.0 - itr / self.extra_decay_time, 0)
            sample_std = np.sqrt(
                np.square(cur_std) +
                np.square(self.extra_std) * extra_var_mult)
            if self.batch_size is None:
                criterion = 'paths'
                threshold = self.n_samples
            else:
                criterion = 'samples'
                threshold = self.batch_size
            infos = stateful_pool.singleton_pool.run_collect(
                _worker_rollout_policy,
                threshold=threshold,
                args=(dict(
                    cur_mean=cur_mean,
                    sample_std=sample_std,
                    max_path_length=self.max_path_length,
                    discount=self.discount,
                    criterion=criterion,
                    n_evals=self.n_evals), ))
            xs = np.asarray([info[0] for info in infos])
            paths = [info[1] for info in infos]

            fs = np.array([path['returns'][0] for path in paths])
            print((xs.shape, fs.shape))
            best_inds = (-fs).argsort()[:n_best]
            best_xs = xs[best_inds]
            cur_mean = best_xs.mean(axis=0)
            cur_std = best_xs.std(axis=0)
            best_x = best_xs[0]
            logger.push_prefix('itr #{} | '.format(itr))
            tabular.record('Iteration', itr)
            tabular.record('CurStdMean', np.mean(cur_std))
            undiscounted_returns = np.array(
                [path['undiscounted_return'] for path in paths])
            tabular.record('AverageReturn', np.mean(undiscounted_returns))
            tabular.record('StdReturn', np.std(undiscounted_returns))
            tabular.record('MaxReturn', np.max(undiscounted_returns))
            tabular.record('MinReturn', np.min(undiscounted_returns))
            tabular.record('AverageDiscountedReturn', np.mean(fs))
            tabular.record('NumTrajs', len(paths))
            paths = list(chain(
                *[d['full_paths']
                  for d in paths]))  # flatten paths for the case n_evals > 1
            tabular.record('AvgTrajLen',
                           np.mean([len(path['returns']) for path in paths]))

            self.policy.set_param_values(best_x)
            self.policy.log_diagnostics(paths)
            snapshotter.save_itr_params(
                itr,
                dict(
                    itr=itr,
                    policy=self.policy,
                    env=self.env,
                    cur_mean=cur_mean,
                    cur_std=cur_std,
                ))
            logger.log(tabular)
            logger.pop_prefix()
            if self.plot:
                self.plotter.update_plot(self.policy, self.max_path_length)
        parallel_sampler.terminate_task()
        self.plotter.close()