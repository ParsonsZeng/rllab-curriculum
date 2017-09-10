from rllab.envs.base import Step
from rllab.misc.overrides import overrides
from rllab.envs.mujoco.mujoco_env import MujocoEnv
import numpy as np
import os.path as osp
import pickle
import cloudpickle
from rllab.core.serializable import Serializable
from rllab.misc import logger
from rllab.misc import autoargs
from contextlib import contextmanager
from sandbox.young_clgan.state.utils import StateCollection
from sandbox.young_clgan.envs.start_env import generate_starts


class InitPR2_key_env(MujocoEnv, Serializable):
    # FILE = 'arm3d_key_tight.xml'
    FILE = "pr2_key_find_init.xml"

    @autoargs.arg('ctrl_cost_coeff', type=float,
                  help='cost coefficient for controls')
    def __init__(
            self,
            ctrl_cost_coeff=1e1,
            goal_dist=3e-2,
            kill_radius=0.4,
            shift_val = 0.0,
            *args, **kwargs):
        self.shift_val = shift_val
        self.ctrl_cost_coeff = ctrl_cost_coeff
        super(InitPR2_key_env, self).__init__(*args, **kwargs)
        Serializable.quick_init(self, locals())
        self.goal_dist = goal_dist
        self.kill_radius = kill_radius
        self.key_hole_center = np.array([0.0, 0.3, -0.55])
        self.ee_indices = [14, 23] # the hill z-axis is 16
        self.frame_skip = 1
        self.init_qpos = np.array([0.1, 0.1, -1.54, -1.7, 1.54, -0.2, 0])
        self.kill_outside = False

        theta = -np.pi / 2
        d = 0.15
        self.goal_position = np.array(
            [0.0, 0.3, -0.55 - d,  # heel
             0.0, 0.3, -0.25 - d,  # top
             0.0 + 0.15 * np.sin(theta), 0.3 + 0.15 * np.cos(theta), -0.4 - d])  # side
        self.cost_params = {
            'wp': np.array([1, 1, 1, 1, 1, 1, 1, 1, 1]),
            'l1': 0.1,
            'l2': 10.0,
            'alpha': 1e-5}


    def get_current_obs(self):
        return np.concatenate([
            self.model.data.qpos.flat,
            self.model.data.qvel.flat,
            self.model.data.site_xpos[0].flat,
            self.model.data.site_xpos[1].flat
            # self.model.data.site_xpos.flat,
        ]).reshape(-1)

    @contextmanager
    def set_kill_outside(self, kill_outside=True, radius=None):
        self.kill_outside = kill_outside
        old_kill_radius = self.kill_radius
        if radius is not None:
            self.kill_radius = radius
        try:
            yield
        finally:
            self.kill_outside = False
            self.kill_radius = old_kill_radius

    def step(self, action):
        # print("entering step, kill_outside is: ", self.kill_outside)
        # action = np.zeros_like(action)
        # print(action)

        # xfrc = np.zeros_like(self.model.data.xfrc_applied)
        # id_kh = self.model.body_names.index('keyhole')
        # xfrc[id_kh, 2] = -9.81 * 1
        # self.model.data.xfrc_applied = xfrc

        # print(self.model.data.xfrc_applied
        if len(action) == 9:
            action[-2] = 0
            action[-1] = 0

        self.forward_dynamics(action)
        next_obs = self.get_current_obs()
        lb, ub = self.action_bounds
        scaling = (ub - lb) * 0.5
        ctrl_cost = 0.5 * self.ctrl_cost_coeff * np.sum(
            np.square(action / scaling))
        # todo check which object has to be at goal position
        # todo also check the meaning of alpha
        # key_position = self.get_body_com('key_head1')
        # ee_position = self.model.data.site_xpos[0]

        # top_of_key = self.model.data.xpos[self.model.body_names.index('key')]
        velocity = np.linalg.norm(self.model.data.qvel)
        reward_velocity = velocity * 1e-4
        key_pos = self.model.data.site_xpos[0]
        goal_pos = self.model.data.site_xpos[-1]
        reward_distance = np.linalg.norm(key_pos - goal_pos) #+ np.linalg.norm(key_pos[:2] - goal_pos[:2]) * 3 # want to make sure XY is correct
        reward = - (reward_distance + reward_velocity)
        # print(top_of_key, key_pos, goal_pos, reward)
        done = False
        # todo: uncomment below til step
        # ee_position = next_obs[self.ee_indices[0]:self.ee_indices[1]]
        # hill_pos = np.array(ee_position[:3])
        # dist = np.sum(np.square(self.goal_position - ee_position) * self.cost_params['wp'])
        # dist_cost = np.sqrt(dist) * self.cost_params['l1'] + dist * self.cost_params['l2']
        # reward = - dist_cost - ctrl_cost
        # done = True if np.sqrt(dist) < self.goal_dist else False
        #
        # # print("making a step in the env, we have kill_outside: ", self.kill_outside)
        # if self.kill_outside and np.linalg.norm(hill_pos - self.key_hole_center) > self.kill_radius:
        # # if self.kill_outside and np.linalg.norm(hill_pos - self.goal_position[:3]) > self.kill_radius:
        #     print("\n****** OUT of region ******")
        #     done = True
        # if np.isnan(reward):
        #     reward = -100
        return Step(next_obs, reward, done, velocity = velocity, reward_distance=reward_distance)

    def log_diagnostics(self, paths):
        velocities = [path["env_infos"]["velocity"] for path in paths]
        logger.record_tabular('velocity', np.mean([np.mean(d) for d in velocities]))
        rd = [path["env_infos"]["reward_distance"] for path in paths]
        logger.record_tabular('reward_distance', np.mean([d[-1] for d in rd]))

    # def reset(self, init_state=None, *args, **kwargs):
    #     # init_state = [0.32735376160809521, -0.52170347540410189, 2.0336760360359354, -1.8511337078149441, 1.3562810265832648,
    #     #  -0.95029451024504419, -2.0000607832102406, -0.10000008191586322, 2.2566119141622387e-07]
    #     # init_state = (0.387, 1.137, -2.028, -1.744, 2.029, -0.873, 1.55)
    #
    #     # if init_state is None and abs(self.shift_val) > 1e-4:
    #     #     init_state = np.zeros(9)
    #     #     init_state[-2] = self.shift_val # moves in plane parallel to robot
    #     # init_state = (0.387, 1.137, -2.028, -1.744, 2.029, -0.873, 1.55, 0, 0) # TODO: used for debugging only!
    #     ret = super(InitPR2_key_env, self).reset(init_state, *args, **kwargs)
    #     xfrc = np.zeros_like(self.model.data.xfrc_applied)
    #     id_kh = self.model.body_names.index('keyhole')
    #     xfrc[id_kh, 2] = -9.81 * 0.1
    #     self.model.data.xfrc_applied = xfrc
    #     return ret


def find_out_feasible_states(env, log_dir, distance_threshold=0.1, brownian_variance=1, animate=False):
    no_new_states = 0
    with env.set_kill_outside():
        load_dir = 'data_upload/state_collections/'
        old_all_feasible_starts = pickle.load(open(osp.join(load_dir, 'all_feasible_states.pkl'), 'rb'))
        out_feasible_starts = StateCollection(distance_threshold=distance_threshold)
        print('number of feasible starts: ', old_all_feasible_starts.size)
        for start in old_all_feasible_starts.state_list:
            obs = env.reset(init_state=start)
            if obs[16] > -0.5:
                # print("got one more up to ", out_feasible_starts.size)
                out_feasible_starts.append([start])
        print("number of out feasible starts:", out_feasible_starts.size)
        while no_new_states < 5:
            total_num_starts = out_feasible_starts.size
            starts = out_feasible_starts.sample(100)
            new_starts = generate_starts(env, starts=starts, horizon=1000, size=100000, variance=brownian_variance,
                                         animated=animate, speedup=10)
            out_feasible_starts.append(new_starts)
            num_new_starts = out_feasible_starts.size - total_num_starts
            logger.log("number of new states: " + str(num_new_starts))
            if num_new_starts < 10:
                no_new_states += 1
            with open(osp.join(log_dir, 'all_out_feasible_states.pkl'), 'wb') as f:
                cloudpickle.dump(out_feasible_starts, f, protocol=3)



