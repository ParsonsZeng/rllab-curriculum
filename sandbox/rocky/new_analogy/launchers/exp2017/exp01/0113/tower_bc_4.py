import pickle

from rllab.misc import logger
from rllab.misc.instrument import run_experiment_lite, variant, VariantGenerator
from sandbox.rocky.new_analogy.tf.policies.residual_policy import ResidualPolicy
from sandbox.rocky.tf.envs.base import TfEnv

"""
Behavior clone single trajectory
"""

MODE = "local_docker"  # _docker"  # _docker"
# MODE = launch_cirrascale("pascal")
N_PARALLEL = 1#8


class VG(VariantGenerator):
    @variant
    def seed(self):
        return [11, 21, 31]#, 41, 51]


def run_task(vv):
    from gpr_package.bin import tower_copter_policy as tower
    from sandbox.rocky.new_analogy.envs.gpr_env import GprEnv
    import tensorflow as tf
    from sandbox.rocky.tf.policies.gaussian_mlp_policy import GaussianMLPPolicy
    from sandbox.rocky.new_analogy.tf.algos import Trainer
    from sandbox.rocky.s3.resource_manager import resource_manager

    with tf.Session() as sess:
        logger.log("Loading data...")
        file_name = resource_manager.get_file("tower_copter_paths_ab_crippled_100")
        with open(file_name, 'rb') as f:
            paths = pickle.load(f)
        logger.log("Loaded")

        xinits = []
        for path in paths:
            xinits.append(path["env_infos"]["x"][0])

        task_id = tower.get_task_from_text("ab")

        env = TfEnv(
            GprEnv(
                "tower",
                task_id=task_id,
                experiment_args=dict(nboxes=2, horizon=1000),
                xinits=xinits[:1],
            )
        )

        policy = ResidualPolicy(env_spec=env.spec, wrapped_policy=GaussianMLPPolicy(
            env_spec=env.spec,
            hidden_sizes=(256, 256, 256, 256),#128, 128),
            hidden_nonlinearity=tf.nn.tanh,
            name="policy"
        ))

        algo = Trainer(
            env=env,
            policy=policy,
            paths=paths[:1],
            n_epochs=500,
            evaluate_performance=True,
            n_passes_per_epoch=1000,
            train_ratio=1.,#0.9,
            max_path_length=1000,
            n_eval_trajs=1,
            eval_batch_size=1000,
            n_eval_envs=1,
            threshold=4.,
            batch_size=128,
            n_slices=10,
        )

        algo.train(sess=sess)


variants = VG().variants()

print("#Experiments:", len(variants))

for v in variants:

    kwargs = dict(
        use_cloudpickle=True,
        exp_prefix="tower-bc-3",
        mode=MODE,
        use_gpu=True,
        snapshot_mode="last",
        sync_all_data_node_to_s3=False,
        n_parallel=N_PARALLEL,
        env=dict(CUDA_VISIBLE_DEVICES="4", PYTHONPATH="/root/code/rllab:/root/code/rllab/gpr_package"),
        variant=v,
        seed=v["seed"],
    )

    if MODE == "local":
        del kwargs["env"]["PYTHONPATH"]  # =
    else:
        kwargs = dict(
            kwargs,
            docker_image="quay.io/openai/rocky-rllab3-gpr-gpu-pascal:20170112",
            docker_args=" -v /home/rocky/conopt-shared-data:/shared-data",
        )

    run_experiment_lite(
        run_task,
        **kwargs
    )
