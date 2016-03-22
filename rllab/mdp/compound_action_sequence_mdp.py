from rllab.mdp.proxy_mdp import ProxyMDP
from rllab.core.serializable import Serializable
import numpy as np
from rllab.misc import special


class CompoundActionSequenceMDP(ProxyMDP, Serializable):

    """
    Takes a discrete action mdp, and turns it into an mdp with compound actions so that each original action is
    mapped from a sequence of discrete actions. An invalid sequence will simply map to nothing in the original MDP.

    To succeed in this mdp, the agent needs to learn the low-level action sequences required to trigger actions in the
    original mdp, and perform exploration using these learned action sequence primitives (thus, a hierarchy).
    """

    def __init__(self, mdp, action_map, action_dim=None, reset_history=False, obs_include_actions=False):
        """
        Constructs a compound mdp.
        :param mdp: The original mdp.
        :param action_map: The map from each action in the original mdp to a sequence of actions, required to trigger
        the corresponding action in the original mdp
        :param action_dim: The action dimension of the compound mdp. By default this is the same as the original mdp
        :param reset_history: This only works if all the action sequences are of the same length. This flag controls
        whether the history will be cleared after it reaches the action sequence length, whether it matches an
        original action or not.
        :param obs_include_actions: Whether to include the actions in the observation. This only works if all the action
        sequences are of the same length.
        :return:
        """
        Serializable.quick_init(self, locals())
        super(CompoundActionSequenceMDP, self).__init__(mdp)
        assert len(action_map) == mdp.action_dim
        action_strs = [",".join(map(str, x)) for x in action_map]
        # ensure no duplicates
        assert len(set(action_strs)) == len(action_strs)
        # ensure that no action sequence is a prefix or suffix of the other
        assert not any([x.startswith(y) for x in action_strs for y in action_strs if x != y])
        assert not any([x.endswith(y) for x in action_strs for y in action_strs if x != y])
        if reset_history or obs_include_actions:
            assert len(set([len(x) for x in action_map])) == 1
        self._action_map = map(np.array, action_map)
        # self._action_strs = action_strs
        self._action_history = []
        if action_dim is None:
            self._action_dim = self._mdp.action_dim
        else:
            self._action_dim = action_dim
        self._raw_obs = None
        self._reset_history = reset_history
        self._obs_include_actions = obs_include_actions
        self._mdp.reset()

    @property
    def action_dim(self):
        return self._action_dim

    @property
    def _history_length(self):
        return len(self._action_map[0])

    def reset(self):
        obs = self._mdp.reset()
        self._action_history = []
        self._raw_obs = obs
        return self._get_current_obs()

    def _get_current_obs(self):
        if self._obs_include_actions:
            # If history_length=5, and action_history = [1, 2, 3], then the observation would include a vector of the
            # form [3, 2, 1, 0, 0]. Note that the most recent action appears first, and any blank spaces are padded
            # at the end
            # If the action history is longer than history_length, then only the last few will be included
            included = self._action_history[::-1][:self._history_length]
            # make shape checking happy
            if len(included) == 0:
                one_hots = np.zeros((0, self._action_dim))
            else:
                one_hots = np.array([special.to_onehot(x, self._action_dim) for x in included])
            padded = np.concatenate(
                [one_hots, np.zeros((self._history_length - len(included), self._action_dim))],
                axis=0
            )
            return np.concatenate([self._raw_obs.flatten(), padded.flatten()])
        else:
            return self._raw_obs

    @property
    def observation_shape(self):
        if self._obs_include_actions:
            return (self._mdp.observation_dim + self._history_length * self._action_dim,)
        else:
            return self._mdp.observation_shape

    def step(self, action):
        self._action_history.append(special.from_onehot(action))
        # check if the last few actions match any real action
        real_action = None
        for idx, action_list in enumerate(self._action_map):
            if np.array_equal(action_list, self._action_history[-len(action_list):]):
                real_action = idx
                break
        if real_action is not None:
            next_raw_obs, reward, done = self._mdp.step(special.to_onehot(real_action, self._mdp.action_dim))
            self._raw_obs = next_raw_obs
            # clear the action history so far
            self._action_history = []
        else:
            # next_obs = self._raw_obs
            reward = 0
            done = False
            if len(self._action_history) == len(self._action_map[0]):
                self._action_history = []
        return self._get_current_obs(), reward, done
