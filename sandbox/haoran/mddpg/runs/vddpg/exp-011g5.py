"""
Variational DDPG (online, consevative)

Anneal temperature to zero for policies and qfs learned from exp-011g
"""
# imports -----------------------------------------------------
from sandbox.haoran.myscripts.retrainer import Retrainer

""" others """
from sandbox.haoran.myscripts.myutilities import get_time_stamp
from sandbox.haoran.ec2_info import instance_info, subnet_info
from rllab import config
from rllab.misc.instrument import stub, run_experiment_lite
import sys,os
import copy
import numpy as np

stub(globals())

from rllab.misc.instrument import VariantGenerator, variant

# exp setup --------------------------------------------------------
exp_index = os.path.basename(__file__).split('.')[0] # exp_xxx
exp_prefix = "tuomas/vddpg/" + exp_index
mode = "ec2_test"
subnet = "us-west-1b"
ec2_instance = "c4.4xlarge"
config.DOCKER_IMAGE = "tsukuyomi2044/rllab3" # needs psutils
config.AWS_IMAGE_ID = "ami-85d181e5" # with docker already pulled

n_task_per_instance = 1
n_parallel = 4 # only for local exp
snapshot_mode = "gap"
snapshot_gap = 10
plot = False

# variant params ---------------------------------------------------
class VG(VariantGenerator):
    @variant
    def exp_info(self):
        return [
            dict(
                exp_prefix="mddpg/vddpg/exp-011g4",
                exp_name="exp-011g4_20170213_135455_217120_swimmer_undirected",
                snapshot_file="itr_199.pkl",
                env_name="swimmer_undirected",
                seed=0
            ),

            dict(
                exp_prefix="mddpg/vddpg/exp-011g4",
                exp_name="exp-011g4_20170213_135457_106436_swimmer_undirected",
                snapshot_file="itr_199.pkl",
                env_name="swimmer_undirected",
                seed=100
            ),

            dict(
                exp_prefix="mddpg/vddpg/exp-011g4",
                exp_name="exp-011g4_20170213_135459_517729_swimmer_undirected",
                snapshot_file="itr_199.pkl",
                env_name="swimmer_undirected",
                seed=200
            ),

            dict(
                exp_prefix="mddpg/vddpg/exp-011g4",
                exp_name="exp-011g4_20170213_135501_503999_swimmer_undirected",
                snapshot_file="itr_199.pkl",
                env_name="swimmer_undirected",
                seed=300
            ),

            dict(
                exp_prefix="mddpg/vddpg/exp-011g4",
                exp_name="exp-011g4_20170213_135503_561513_swimmer_undirected",
                snapshot_file="itr_199.pkl",
                env_name="swimmer_undirected",
                seed=400
            ),

            dict(
                exp_prefix="mddpg/vddpg/exp-011g4",
                exp_name="exp-011g4_20170213_135505_283574_swimmer_undirected",
                snapshot_file="itr_199.pkl",
                env_name="swimmer_undirected",
                seed=500
            ),

            dict(
                exp_prefix="mddpg/vddpg/exp-011g4",
                exp_name="exp-011g4_20170213_135507_732676_swimmer_undirected",
                snapshot_file="itr_199.pkl",
                env_name="swimmer_undirected",
                seed=600
            ),

            dict(
                exp_prefix="mddpg/vddpg/exp-011g4",
                exp_name="exp-011g4_20170213_135510_180744_swimmer_undirected",
                snapshot_file="itr_199.pkl",
                env_name="swimmer_undirected",
                seed=700
            ),

            dict(
                exp_prefix="mddpg/vddpg/exp-011g4",
                exp_name="exp-011g4_20170213_135511_927600_swimmer_undirected",
                snapshot_file="itr_199.pkl",
                env_name="swimmer_undirected",
                seed=800
            ),

            dict(
                exp_prefix="mddpg/vddpg/exp-011g4",
                exp_name="exp-011g4_20170213_135514_310807_swimmer_undirected",
                snapshot_file="itr_199.pkl",
                env_name="swimmer_undirected",
                seed=900
            ),
        ]
    @variant
    def max_path_length(self):
        return [500]

    @variant
    def scale_reward_annealer_final_value(self):
        return [1000]

    @variant
    def scale_reward_annealer_stop_iter(self):
        return [20]

    @variant
    def update_target_frequency(self):
        return [10000, 1000]

variants = VG().variants()
batch_tasks = []
print("#Experiments: %d" % len(variants))
for v in variants:
    # non-variant params -----------------------------------
    # >>>>>>
    if "local" in mode and sys.platform == "darwin":
        plt_backend = "MacOSX"
    else:
        plt_backend = "Agg"
    shared_alg_kwargs = dict(
        max_path_length=v["max_path_length"],
        plt_backend=plt_backend,
        K_critic=10,
        K=10,
        critic_train_frequency=1,
        actor_train_frequency=1,
        update_target_frequency=v["update_target_frequency"],
        eval_kl_n_sample=1,
        eval_kl_n_sample_part=1,
        scale_reward_annealer_final_value=v["scale_reward_annealer_final_value"],
        scale_reward_annealer_stop_iter=v["scale_reward_annealer_stop_iter"],
    )
    # algo
    if mode == "local_test" or mode == "local_docker_test":
        alg_kwargs = dict(
            epoch_length=200,
            min_pool_size=100,
                # beware that the algo doesn't finish an epoch
                # until it finishes one path
            n_epochs=100,
            n_eval_paths=2,
        )
    else:
        alg_kwargs = dict(
            epoch_length=10000,
            n_epochs=500,
            n_eval_paths=10,
            min_pool_size=20000, # perhaps better to wait for more samples
        )
    alg_kwargs.update(shared_alg_kwargs)

    # other exp setup --------------------------------------
    exp_name = "{exp_index}_{time}_{env_name}".format(
        exp_index=exp_index,
        time=get_time_stamp(),
        env_name=v["exp_info"]["env_name"],
    )
    if ("ec2" in mode) and (len(exp_name) > 64):
        print("Should not use experiment name with length %d > 64.\nThe experiment name is %s.\n Exit now."%(len(exp_name),exp_name))
        sys.exit(1)

    if "local_docker" in mode:
        actual_mode = "local_docker"
    elif "local" in mode:
        actual_mode = "local"
    elif "ec2" in mode:
        actual_mode = "ec2"
        # configure instance
        info = instance_info[ec2_instance]
        config.AWS_INSTANCE_TYPE = ec2_instance
        config.AWS_SPOT_PRICE = str(info["price"])
        n_parallel = int(info["vCPU"] /2)

        # choose subnet
        config.AWS_NETWORK_INTERFACES = [
            dict(
                SubnetId=subnet_info[subnet]["SubnetID"],
                Groups=subnet_info[subnet]["Groups"],
                DeviceIndex=0,
                AssociatePublicIpAddress=True,
            )
        ]
    elif "kube" in mode:
        actual_mode = "lab_kube"
        info = instance_info[ec2_instance]
        n_parallel = int(info["vCPU"] /2)

        config.KUBE_DEFAULT_RESOURCES = {
            "requests": {
                "cpu": int(info["vCPU"]*0.75)
            }
        }
        config.KUBE_DEFAULT_NODE_SELECTOR = {
            "aws/type": ec2_instance
        }
        exp_prefix = exp_prefix.replace('/','-') # otherwise kube rejects
    else:
        raise NotImplementedError

    # construct objects ----------------------------------
    exp_info = v["exp_info"]

    # the script should have not indentation
    configure_script = """

# make it like DDPG
_algo = self.algo
_algo.policy._freeze = True
_algo.policy._samples = _algo.policy._samples[:{K}]
_algo.policy._K = {K}
_algo.K = {K}

# annealer
from sandbox.haoran.mddpg.misc.annealer import LogLinearAnnealer
scale_reward_annealer = LogLinearAnnealer(
    init_value=_algo.scale_reward_annealer.final_value,
    final_value={scale_reward_annealer_final_value},
    stop_iter={scale_reward_annealer_stop_iter},
    n_iter={n_epochs},
)

from sandbox.tuomas.mddpg.algos import vddpg
qf_params = _algo.qf.get_param_values()
pi_params = _algo.policy.get_param_values()
# avoid errors from NeuralNetwork.get_copy()
vddpg.TARGET_PREFIX = "new_target_"
vddpg.DUMMY_PREFIX = "new_dummy_"
self.algo = vddpg.VDDPG(
    env=_algo.env,
    exploration_strategy=_algo.exploration_strategy,
    policy=_algo.policy,
    kernel=_algo.kernel,
    qf=_algo.qf,
    K=_algo.K,
    K_critic={K_critic},
    q_target_type=\"max\", # avoid extra sampling to compute soft target
    n_eval_paths={n_eval_paths},
    svgd_target=_algo.svgd_target,
    plt_backend=\"{plt_backend}\",
    critic_train_frequency={critic_train_frequency},
    actor_train_frequency={actor_train_frequency},
    update_target_frequency={update_target_frequency},
    q_plot_settings=_algo.q_plot_settings,
    env_plot_settings=_algo.env_plot_settings,
    eval_kl_n_sample={eval_kl_n_sample},
    eval_kl_n_sample_part={eval_kl_n_sample_part},
    max_path_length={max_path_length},
    n_epochs={n_epochs},
    epoch_length={epoch_length},
    min_pool_size={min_pool_size},
    batch_size=_algo.batch_size,
    discount=_algo.discount,
    soft_target_tau=1,
    scale_reward_annealer=scale_reward_annealer,
)
self.algo.qf.set_param_values(qf_params)
self.algo.policy.set_param_values(pi_params)
self.algo.target_qf.set_param_values(qf_params)
self.algo.target_policy.set_param_values(pi_params)
self.algo.dummy_policy.set_param_values(pi_params)
    """.format(**alg_kwargs)

    retrainer = Retrainer(
        exp_prefix=exp_info["exp_prefix"],
        exp_name=exp_info["exp_name"],
        snapshot_file=exp_info["snapshot_file"],
        configure_script=configure_script,
    )

    # run -----------------------------------------------------------
    print(v)
    batch_tasks.append(
        dict(
            stub_method_call=retrainer.retrain(),
            exp_name=exp_name,
            seed=v["exp_info"]["seed"],
            snapshot_mode=snapshot_mode,
            snapshot_gap=snapshot_gap,
            variant=v,
            plot=plot,
            n_parallel=n_parallel,
        )
    )
    if len(batch_tasks) >= n_task_per_instance:
        run_experiment_lite(
            batch_tasks=batch_tasks,
            exp_prefix=exp_prefix,
            mode=actual_mode,
            sync_s3_pkl=True,
            sync_s3_log=True,
            sync_s3_png=True,
            sync_log_on_termination=True,
            sync_all_data_node_to_s3=True,
            terminate_machine=("test" not in mode),
        )
        batch_tasks = []
        if "test" in mode:
            sys.exit(0)

if ("local" not in mode) and ("test" not in mode):
    os.system("chmod 444 %s"%(__file__))
