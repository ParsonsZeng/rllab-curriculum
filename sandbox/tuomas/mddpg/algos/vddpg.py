from sandbox.haoran.mddpg.misc.rllab_util import split_paths
from sandbox.haoran.mddpg.misc.data_processing import create_stats_ordered_dict
from sandbox.tuomas.mddpg.algos.online_algorithm import OnlineAlgorithm
from sandbox.rocky.tf.misc.tensor_utils import flatten_tensor_variables
from sandbox.tuomas.mddpg.policies.stochastic_policy import StochasticNNPolicy
from sandbox.tuomas.mddpg.misc.sampler import ParallelSampler

# for debugging
from sandbox.tuomas.mddpg.misc.sim_policy import rollout, rollout_alg

from rllab.misc.overrides import overrides
from rllab.misc import logger
from sandbox.tuomas.mddpg.misc import special
from rllab.envs.proxy_env import ProxyEnv
from rllab.core.serializable import Serializable

from collections import OrderedDict
import numpy as np
import scipy.stats
import tensorflow as tf
import matplotlib.pyplot as plt
import os
import gc

TARGET_PREFIX = "target_"


def tf_shape(shape):
    """Converts a list of python and tf scalars tensors into a tf vector."""
    tf_shape_list = []
    for d in shape:
        if type(d) not in (np.int32, int, tf.Tensor):
            d = d.astype('int32')
        if type(d) == tf.Tensor:
            tf_shape_list.append(d)
        else:
            tf_shape_list.append(tf.constant(d))

    return tf.pack(tf_shape_list)


class VDDPG(OnlineAlgorithm, Serializable):
    """
    Variational DDPG with Stein Variational Gradient Descent using stochastic
    net.
    """

    def __init__(
            self,
            env,
            exploration_strategy,
            policy,
            kernel,
            qf,
            K,
            # Number of static particles (used only if actor_sparse_update=True)
            K_fixed=1,
            Ks=None,  # Use different number of particles for different temps.
            q_prior=None,
            q_target_type="max",
            qf_learning_rate=1e-3,
            policy_learning_rate=1e-4,
            Q_weight_decay=0.,
            alpha=1.,
            qf_extra_training=0,
            temperatures=None,
            train_critic=True,
            train_actor=True,
            actor_sparse_update=False,
            resume=False,
            n_eval_paths=2,
            svgd_target="action",
            plt_backend="TkAgg",
            **kwargs
    ):
        """
        :param env: Environment
        :param exploration_strategy: ExplorationStrategy
        :param policy: a multiheaded policy
        :param kernel: specifies discrepancy between heads
        :param qf: QFunctions that is Serializable
        :param K: number of policies
        :param q_target_type: how to aggregate targets from multiple heads
        :param qf_learning_rate: Learning rate of the critic
        :param policy_learning_rate: Learning rate of the actor
        :param Q_weight_decay: How much to decay the weights for Q
        :return:
        """
        assert ((Ks is None and temperatures is None) or
                Ks.shape[0] == temperatures.shape[0])
        Serializable.quick_init(self, locals())
        self.kernel = kernel
        self.qf = qf
        self.q_prior = q_prior
        self.prior_coeff = 0.#1.
        self.K = K
        self.K_static = K_fixed
        self.Ks = Ks
        self.K_critic = 99  # Number of actions used to estimate the target.
        Da = env.action_space.flat_dim
        self.critic_proposal_sigma = np.ones((Da,))
        self.critic_proposal_mu = np.zeros((Da,))
        self.q_target_type = q_target_type
        self.critic_lr = qf_learning_rate
        self.critic_weight_decay = Q_weight_decay
        self.actor_learning_rate = policy_learning_rate
        self.alpha = alpha
        self.qf_extra_training = qf_extra_training
        self.train_critic = train_critic
        self.train_actor = train_actor
        self.actor_sparse_update = actor_sparse_update
        self.resume = resume
        self.temperatures = temperatures

        self.alpha_placeholder = tf.placeholder(tf.float32,
                                                shape=(),
                                                name='alpha')

        self.prior_coeff_placeholder = tf.placeholder(tf.float32,
                                                      shape=(),
                                                      name='prior_coeff')
        self.K_pl = tf.placeholder(tf.int32, shape=(), name='K')
        # # Number of particles for computing critic target.
        # self.K_critic_pl = tf.placeholder(tf.int32, shape=(), name='K')

        if q_target_type == 'soft':
            self.importance_weights_pl = tf.placeholder(
                tf.float32, shape=(None, self.K_critic, None),
                name='importance_weights'
            )

        self.svgd_target = svgd_target
        if svgd_target == "pre-action":
            assert policy.output_nonlinearity == tf.nn.tanh
            assert policy.output_scale == 1.

        assert train_actor or train_critic
        #assert isinstance(policy, StochasticNNPolicy)
        #assert isinstance(exploration_strategy, MNNStrategy)

        #if resume:
        #    qf_params = qf.get_param_values()
        #    policy_params = policy.get_param_values()
        super().__init__(env, policy, exploration_strategy, **kwargs)
        #if resume:
        #    qf.set_param_values(qf_params)
        #    policy.set_param_values(policy_params)

        self.eval_sampler = ParallelSampler(self)
        self.n_eval_paths = n_eval_paths
        plt.switch_backend(plt_backend)

    @overrides
    def _init_tensorflow_ops(self):

        # Useful dimensions.
        Da = self.env.action_space.flat_dim

        # Initialize variables for get_copy to work
        self.sess.run(tf.global_variables_initializer())

        self.target_policy = self.policy.get_copy(
            scope_name=TARGET_PREFIX + self.policy.scope_name,
        )
        self.dummy_policy = self.policy.get_copy(
            scope_name="dummy_" + self.policy.scope_name,
        )

        if self.q_target_type == 'soft':
            # For soft target, we don't feed in the actions from the policy.
            self.target_qf = self.qf.get_copy(
                scope_name=TARGET_PREFIX + self.qf.scope_name,
            )
        else:
            self.target_qf = self.qf.get_copy(
                scope_name=TARGET_PREFIX + self.qf.scope_name,
                action_input=self.target_policy.output
            )

        # TH: It's a bit weird to set class attributes (kernel.kappa and
        # kernel.kappa_grads) outside the class. Could we do this somehow
        # differently?
        # Note: need to reshape policy output from N*K x Da to N x K x Da
        shape = tf_shape((-1, self.K_pl, Da))
        if self.svgd_target == "action":
            if self.actor_sparse_update:
                updated_actions = tf.reshape(self.policy.output, shape)
                self.kernel.kappa = self.kernel.get_asymmetric_kappa(
                    updated_actions
                )
                self.kernel.kappa_grads = (
                    self.kernel.get_asymmetric_kappa_grads(updated_actions)
                )

            else:
                actions_reshaped = tf.reshape(self.policy.output, shape)
                self.kernel.kappa = self.kernel.get_kappa(actions_reshaped)
                self.kernel.kappa_grads = self.kernel.get_kappa_grads(
                    actions_reshaped)
        elif self.svgd_target == "pre-action":
            if self.actor_sparse_update:
                raise NotImplementedError

            pre_actions_reshaped = tf.reshape(self.policy.pre_output, shape)
            self.kernel.kappa = self.kernel.get_kappa(pre_actions_reshaped)
            self.kernel.kappa_grads = self.kernel.get_kappa_grads(
                pre_actions_reshaped)
        else:
            raise NotImplementedError

        self.kernel.sess = self.sess
        self.qf.sess = self.sess
        self.policy.sess = self.sess
        if self.eval_policy:
            self.eval_policy.sess = self.sess
        self.target_policy.sess = self.sess
        self.dummy_policy.sess = self.sess

        self._init_ops()

        self.sess.run(tf.global_variables_initializer())

    def _init_ops(self):
        self._init_actor_ops()
        self._init_critic_ops()
        self._init_target_ops()

    def _init_actor_ops(self):
        """
        Note: critic is given as an argument so that we can have several critics

        SVGD
        For easy coding, we can run a session first to update the kernel.
            But it means we need to compute the actor outputs twice. A
            benefit is the kernel sizes can be easily adapted. Otherwise
            tensorflow may differentiate through the kernel size as well.
        An alternative is to manually compute the gradient, but within
            one session.
        A third way is to feed gradients w.r.t. actions to tf.gradients by
            specifying grad_ys.
        Need to write a test case.
        """
        if not self.train_actor:
            pass

        all_true_params = self.policy.get_params_internal()
        all_dummy_params = self.dummy_policy.get_params_internal()
        Da = self.env.action_space.flat_dim

        # TODO: not sure if this is needed
        self.critic_with_policy_input = self.qf.get_weight_tied_copy(
            action_input=self.policy.output,
            observation_input=self.policy.observations_placeholder,
        )
        if self.actor_sparse_update:
            self.critic_with_static_actions = self.qf.get_weight_tied_copy(
            action_input=self.kernel.fixed_actions_pl,
            observation_input=self.policy.observations_placeholder,
        )

        if self.svgd_target == "action":
            if self.q_prior is not None and self.actor_sparse_update:
                raise NotImplementedError
            if self.q_prior is not None:
                self.prior_with_policy_input = self.q_prior.get_weight_tied_copy(
                    action_input=self.policy.output,
                    observation_input=self.policy.observations_placeholder,
                )
                p = self.prior_coeff_placeholder
                log_p = ((1.0 - p) * self.critic_with_policy_input.output
                         + p * self.prior_with_policy_input.output)
            elif self.actor_sparse_update:
                log_p = self.critic_with_static_actions.output
            else:
                log_p = self.critic_with_policy_input.output
            log_p = tf.squeeze(log_p)

            if self.actor_sparse_update:
                grad_log_p = tf.gradients(log_p,
                                          self.critic_with_static_actions.action_input)

                grad_log_p = tf.reshape(grad_log_p,
                                        tf_shape((-1, self.K_static, 1, Da)))
            else:
                grad_log_p = tf.gradients(log_p, self.policy.output)

                grad_log_p = tf.reshape(grad_log_p,
                                        tf_shape((-1, self.K_pl, 1, Da)))
            # N x K x 1 x Da

            kappa = tf.expand_dims(
                self.kernel.kappa,
                dim=3,
            )  # N x K x K x 1

            # grad w.r.t. left kernel input
            kappa_grads = self.kernel.kappa_grads  # N x K x K x Da

            # Stein Variational Gradient!
            action_grads = tf.reduce_mean(
                kappa * grad_log_p
                + self.alpha_placeholder * kappa_grads,
                reduction_indices=1,
            ) # N x K x Da

            # The first two dims needs to be flattened to correctly propagate the
            # gradients to the policy network.
            action_grads = tf.reshape(action_grads, (-1, Da))

            # Propagate the grads through the policy net.
            grads = tf.gradients(
                self.policy.output,
                self.policy.get_params_internal(),
                grad_ys=action_grads,
            )
        elif self.svgd_target == "pre-action":
            if self.q_prior is not None:
                self.prior_with_policy_input = self.q_prior.get_weight_tied_copy(
                    action_input=self.policy.output,
                    observation_input=self.policy.observations_placeholder,
                )
                p = self.prior_coeff_placeholder
                log_p_from_Q = ((1.0 - p) * self.critic_with_policy_input.output
                    + p * self.prior_with_policy_input.output)
            else:
                log_p_from_Q = self.critic_with_policy_input.output # N*K x 1
            log_p_from_Q = tf.squeeze(log_p_from_Q) # N*K

            grad_log_p_from_Q = tf.gradients(log_p_from_Q, self.policy.pre_output)
                # N*K x Da
            grad_log_p_from_tanh = - 2. * self.policy.output # N*K x Da
                # d/dx(log(1-tanh^2(x))) = -2tanh(x)
            grad_log_p = (
                grad_log_p_from_Q +
                self.alpha_placeholder * grad_log_p_from_tanh
            )
            grad_log_p = tf.reshape(grad_log_p,
                                    tf_shape((-1, self.K_pl, 1, Da)))
            # N x K x 1 x Da

            kappa = tf.expand_dims(
                self.kernel.kappa,
                dim=3,
            )  # N x K x K x 1

            # grad w.r.t. left kernel input
            kappa_grads = self.kernel.kappa_grads  # N x K x K x Da

            # Stein Variational Gradient!
            pre_action_grads = tf.reduce_mean(
                kappa * grad_log_p
                + self.alpha_placeholder * kappa_grads,
                reduction_indices=1,
            ) # N x K x Da

            # The first two dims needs to be flattened to correctly propagate the
            # gradients to the policy network.
            pre_action_grads = tf.reshape(pre_action_grads, (-1, Da))

            # Propagate the grads through the policy net.
            grads = tf.gradients(
                self.policy.pre_output,
                self.policy.get_params_internal(),
                grad_ys=pre_action_grads,
            )
        else:
            raise NotImplementedError

        self.actor_surrogate_loss = tf.reduce_mean(
            - flatten_tensor_variables(all_dummy_params) *
            flatten_tensor_variables(grads)
        )

        self.train_actor_op = [
            tf.train.AdamOptimizer(
                self.actor_learning_rate).minimize(
                self.actor_surrogate_loss,
                var_list=all_dummy_params)
        ]

        self.finalize_actor_op = [
            tf.assign(true_param, dummy_param)
            for true_param, dummy_param in zip(
                all_true_params,
                all_dummy_params,
            )
        ]

    def _init_critic_ops(self):
        if not self.train_critic:
            return
        M = self.qf.dim if hasattr(self.qf, 'dim') else 1

        if hasattr(self.target_qf, 'outputs'):
            q_next = self.target_qf.outputs
            q_curr = self.qf.outputs
        else:
            q_next = self.target_qf.output
            q_curr = self.qf.output

        # N x K x M
        q_next = tf.reshape(q_next, tf_shape((-1, self.K_critic, M)))
        q_curr = tf.reshape(q_curr, (-1, M))  # N x M

        if self.q_target_type == 'mean':
            q_next = tf.reduce_mean(q_next, reduction_indices=1, name='q_next',
                                    keep_dims=False)  # N x M
        elif self.q_target_type == 'max':
            # TODO: This is actually wrong. Now the max of each critic might
            # be attained with a different actions. We should consistently
            # pick a single action and stick with that for all critics.
            q_next = tf.reduce_max(q_next, reduction_indices=1, name='q_next',
                                   keep_dims=False)  # N x M
        elif self.q_target_type == 'soft':
            # Note: q_next is actually soft V!
            exp_q_next = tf.exp(q_next)  # N x K x M
            # N x K x M
            weighted_exp_q_samples = exp_q_next / self.importance_weights_pl
            # N x M
            q_next = tf.log(tf.reduce_mean(weighted_exp_q_samples, axis=1))

        else:
            raise NotImplementedError
        # q_next: N x M

        assert_op = tf.assert_equal(
            tf.shape(self.rewards_placeholder), tf.shape(q_next)
        )

        with tf.control_dependencies([assert_op]):
            # TODO: Discount should be set independently for each critic.
            self.ys = (
                self.rewards_placeholder + (1 - self.terminals_placeholder) *
                self.discount * q_next
            )  # N x M

        self.critic_loss = tf.reduce_mean(tf.reduce_mean(
            tf.square(self.ys - q_curr)
        ))

        self.critic_reg = tf.reduce_sum(
            tf.pack(
                [tf.nn.l2_loss(v)
                 for v in
                 self.qf.get_params_internal(only_regularizable=True)]
            ),
            name='weights_norm'
        )
        self.critic_total_loss = (
            self.critic_loss + self.critic_weight_decay * self.critic_reg)

        self.train_critic_op = tf.train.AdamOptimizer(self.critic_lr).minimize(
            self.critic_total_loss,
            var_list=self.qf.get_params_internal()
        )

    def _init_target_ops(self):

        if self.train_critic:
            # Set target policy
            actor_vars = self.policy.get_params_internal()
            target_actor_vars = self.target_policy.get_params_internal()
            assert len(actor_vars) == len(target_actor_vars)
            self.update_target_actor_op = [
                tf.assign(target, (self.tau * src + (1 - self.tau) * target))
                for target, src in zip(target_actor_vars, actor_vars)]

            # Set target Q-function
            critic_vars = self.qf.get_params_internal()
            target_critic_vars = self.target_qf.get_params_internal()
            self.update_target_critic_op = [
                tf.assign(target, self.tau * src + (1 - self.tau) * target)
                for target, src in zip(target_critic_vars, critic_vars)
            ]

    @overrides
    def _init_training(self):
        super()._init_training()
        self.target_qf.set_param_values(self.qf.get_param_values())
        self.target_policy.set_param_values(self.policy.get_param_values())
        self.dummy_policy.set_param_values(self.policy.get_param_values())

    @overrides
    def _get_training_ops(self):
        train_ops = list()
        if self.train_actor:
            train_ops += self.train_actor_op
        if self.train_critic:
            train_ops += [self.train_critic_op,
                          self.update_target_actor_op,
                          self.update_target_critic_op]

        return train_ops

    def _get_finalize_ops(self):
        return [self.finalize_actor_op]

    #def _sample_temps(self, N):
    #    inds = np.random.randint(0, self.temperatures.shape[0], size=(N,))
    #    temps = self.temperatures[inds]
    #    return temps

    @overrides
    def _update_feed_dict(self, rewards, terminals, obs, actions, next_obs):
        # Note: each sample in a batch need to have the same K. That's why
        # also the temperature is same for all of them (we want associate
        # specific temperature values to specific Ks in order not to confuse
        # the network.
        N = obs.shape[0]
        if self.temperatures is not None:
            ind = np.random.randint(0, self.temperatures.shape[0])
            K = self.Ks[ind]
            temp = self.temperatures[[ind]]
            temp = self._replicate_rows(temp, N)
        else:
            temp = None
            K = self.K

        feeds = dict()
        if self.train_actor:
            if self.actor_sparse_update:
                # Update feed dict first with larger number of fixed particles
                feeds.update(self._actor_feed_dict_for(
                    self.critic_with_static_actions, obs, temp, self.K_static))
                feeds.update(
                    self.kernel.update(self,
                                       feeds,
                                       multiheaded=False,
                                       K=self.K_static)
                )

                # Then update the feeds again with smaller number of particles.
                feeds.update(self._actor_feed_dict_for(None, obs, temp, self.K))
            else:
                feeds.update(self._actor_feed_dict_for(
                    self.critic_with_policy_input, obs, temp, K)
                )
                feeds.update(
                    self.kernel.update(self, feeds, multiheaded=False,
                                       K=self.K)
                )

        if self.train_critic:
            feeds.update(self._critic_feed_dict(
                rewards, terminals, obs, actions, next_obs, temp, self.K_critic
            ))

        feeds[self.K_pl] = K

        return feeds

    def _actor_feed_dict_for(self, critic, obs, temp, K):
        # Note that we want K samples for each observation. Therefore we
        # first need to replicate the observations.
        obs = self._replicate_rows(obs, K)
        temp = self._replicate_rows(temp, K)

        # Make sure we're not giving extra arguments for policies not supporting
        # temperature input.
        actor_inputs = (obs,) if temp is None else (obs, temp)
        critic_inputs = (obs,) if temp is None else (obs, None, temp)

        feed = self.policy.get_feed_dict(*actor_inputs)
        #feed.update(self.critic_with_policy_input.get_feed_dict(*critic_inputs))
        if critic is not None:
            feed.update(critic.get_feed_dict(*critic_inputs))
        #feed.update(self.critic_with_static_actions.get_feed_dict(*critic_inputs))
        feed[self.alpha_placeholder] = self.alpha
        feed[self.prior_coeff_placeholder] = self.prior_coeff
        return feed

    def _critic_feed_dict(self, rewards, terminals, obs, actions, next_obs,
                          temp, K):
        N = obs.shape[0]
        Da = self.env.action_space.flat_dim
        feed = {}
        # Again, we'll need to replicate next_obs.
        next_obs = self._replicate_rows(next_obs, K)

        # TODO: we should make the next temp really low (actually high since
        # it is inverse temperature)
        temp = self._replicate_rows(temp, K)

        target_policy_input = [next_obs]
        critic_input = [obs, actions]

        if self.q_target_type == 'soft':
            # We'll use the same actions for each sample (first dimension).
            actions = (np.random.randn(N, self.K_critic, Da)
                       * self.critic_proposal_sigma + self.critic_proposal_mu)
            weights = scipy.stats.multivariate_normal(
                mean=self.critic_proposal_mu,
                cov=self.critic_proposal_sigma**2,
            ).pdf(actions)
            # N*K_critic x Da
            actions = np.reshape(actions, (N*self.K_critic, Da))

            feed[self.importance_weights_pl] = weights[:, :, None]
            target_critic_input = [next_obs, actions]
        else:
            target_critic_input = [next_obs, None]

        if temp is not None:
            target_policy_input.append(temp)
            critic_input.append(temp)
            target_critic_input.append(temp)

        #curr_inputs = (obs, actions) if temp is None else (obs, actions, temp)
        #next_inputs = (next_obs,) if temp is None else (next_obs, temp)

        feed.update(self.target_policy.get_feed_dict(*target_policy_input))
        feed.update(self.qf.get_feed_dict(*critic_input))
        feed.update(self.target_qf.get_feed_dict(*target_critic_input))


        # Adjust rewards dims for backward compatibility.
        if rewards.ndim == 1:
            rewards = np.expand_dims(rewards, axis=1)

        feed.update({
            self.rewards_placeholder: rewards,
            self.terminals_placeholder: np.expand_dims(terminals, axis=1),
        })

        return feed

    def _replicate_rows(self, t, K):
        """Replicates each row in t K times."""
        if t is None:
            return t

        assert t.ndim == 2
        N = t.shape[0]

        t = np.expand_dims(t, axis=1)  # N x 1 x X
        t = np.tile(t, (1, K, 1))  # N x K x X
        t = np.reshape(t, (N * K, -1))  # N*K x Do

        return t

    @overrides
    def evaluate(self, epoch, train_info):
        logger.log("Collecting samples for evaluation")
        paths = self.eval_sampler.obtain_samples(
            n_paths=self.n_eval_paths,
            max_path_length=self.max_path_length,
            policy=self.eval_policy
        )
        rewards, terminals, obs, actions, next_obs = split_paths(paths)
        feed_dict = self._update_feed_dict(rewards, terminals, obs, actions,
                                           next_obs)

        # Compute statistics
        (
            policy_loss,
            qf_loss,
            policy_outputs,
            target_policy_outputs,
            qf_outputs,
            target_qf_outputs,
            ys,
            kappa,  # N x K x K
        ) = self.sess.run(
            [
                self.actor_surrogate_loss,
                self.critic_loss,
                self.policy.output,
                self.target_policy.output,
                self.qf.output,
                self.target_qf.output,
                self.ys,
                self.kernel.kappa,
            ],
            feed_dict=feed_dict)
        average_discounted_return = np.mean(
            [special.discount_return(path["rewards"], self.discount)
             for path in paths]
        )
        returns = np.asarray([sum(path["rewards"]) for path in paths])
        rewards = np.hstack([path["rewards"] for path in paths])
        Da = self.env.action_space.flat_dim
        policy_vars = np.mean(
            np.var(
                policy_outputs.reshape((-1, self.K, Da)),
                axis=1
            ), axis=1
        )
        kappa_sum = np.sum(kappa, axis=1).ravel()

        # Log statistics
        self.last_statistics.update(OrderedDict([
            ('Epoch', epoch),
            # ('PolicySurrogateLoss', policy_loss),
            #HT: why are the policy outputs info helpful?
            # ('PolicyMeanOutput', np.mean(policy_outputs)),
            # ('PolicyStdOutput', np.std(policy_outputs)),
            # ('TargetPolicyMeanOutput', np.mean(target_policy_outputs)),
            # ('TargetPolicyStdOutput', np.std(target_policy_outputs)),
            ('CriticLoss', qf_loss),
            ('AverageDiscountedReturn', average_discounted_return),
        ]))
        # self.last_statistics.update(create_stats_ordered_dict('Ys', ys))
        self.last_statistics.update(create_stats_ordered_dict('QfOutput',
                                                              qf_outputs))
        # self.last_statistics.update(create_stats_ordered_dict('TargetQfOutput',
        #                                                       target_qf_outputs))
        # self.last_statistics.update(create_stats_ordered_dict('Rewards', rewards))
        self.last_statistics.update(create_stats_ordered_dict('returns', returns))
        self.last_statistics.update(
           create_stats_ordered_dict('PolicyVars',policy_vars)
        )
        self.last_statistics.update(
            create_stats_ordered_dict('KappaSum',kappa_sum)
        )

        es_path_returns = train_info["es_path_returns"]
        if len(es_path_returns) == 0 and epoch == 0:
            es_path_returns = [0]
        if len(es_path_returns) > 0:
            # if eval is too often, training may not even have collected a full
            # path
            train_returns = np.asarray(es_path_returns) / self.scale_reward
            self.last_statistics.update(create_stats_ordered_dict(
                'TrainingReturns', train_returns))

        es_path_lengths = train_info["es_path_lengths"]
        if len(es_path_lengths) == 0 and epoch == 0:
            es_path_lengths = [0]
        if len(es_path_lengths) > 0:
            # if eval is too often, training may not even have collected a full
            # path
            self.last_statistics.update(create_stats_ordered_dict(
                'TrainingPathLengths', es_path_lengths))


        # Create figure for plotting the environment.
        fig = plt.figure(figsize=(12, 7))
        ax = fig.add_subplot(111)

        true_env = self.env
        while isinstance(true_env, ProxyEnv):
            true_env = true_env._wrapped_env
        if hasattr(true_env, "log_stats"):
            env_stats = true_env.log_stats(self, epoch, paths, ax)
            self.last_statistics.update(env_stats)

        # Close and save figs.
        snapshot_dir = logger.get_snapshot_dir()
        img_file = os.path.join(snapshot_dir, 'itr_%d_test_paths.png' % epoch)

        plt.draw()
        plt.pause(0.001)

        plt.savefig(img_file, dpi=100)
        plt.cla()
        plt.close('all')

        for key, value in self.last_statistics.items():
            logger.record_tabular(key, value)

        gc.collect()

    def get_epoch_snapshot(self, epoch):
        return dict(
            epoch=epoch,
            # env=self.env,
            # policy=self.policy,
            # es=self.exploration_strategy,
            # qf=self.qf,
            # kernel=self.kernel,
            algo=self,
        )

    def __getstate__(self):
        d = Serializable.__getstate__(self)
        d.update({
            "policy_params": self.policy.get_param_values(),
            "qf_params": self.qf.get_param_values(),
        })
        return d

    def __setstate__(self, d):
        Serializable.__setstate__(self, d)
        self.qf.set_param_values(d["qf_params"])
        self.policy.set_param_values(d["policy_params"])
