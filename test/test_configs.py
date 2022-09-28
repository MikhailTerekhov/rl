import argparse

import pytest
import torch.cuda

try:
    import hydra
    from hydra.core.global_hydra import GlobalHydra
    from hydra.utils import instantiate

    _has_hydra = True
except ImportError:
    _has_hydra = False
from mocking_classes import ContinuousActionVecMockEnv
from torch import nn
from torchrl.envs.libs.dm_control import _has_dmc
from torchrl.envs.libs.gym import _has_gym
from torchrl.modules import TensorDictModule


def make_env():
    def fun():
        return ContinuousActionVecMockEnv()

    return fun


@pytest.mark.skipif(not _has_hydra, reason="No hydra found")
class TestConfigs:
    @pytest.fixture(scope="class", autouse=True)
    def init_hydra(self, request):
        GlobalHydra.instance().clear()
        hydra.initialize("../examples/configs/")
        request.addfinalizer(GlobalHydra.instance().clear)

    @pytest.mark.parametrize(
        "file,num_workers",
        [
            ("async_sync", 2),
            ("sync_single", 0),
            ("sync_sync", 2),
        ],
    )
    def test_collector_configs(self, file, num_workers):
        create_env = make_env()
        policy = TensorDictModule(
            nn.Linear(7, 7), in_keys=["observation"], out_keys=["action"]
        )

        cfg = hydra.compose(
            "config", overrides=[f"collector={file}", f"num_workers={num_workers}"]
        )

        if cfg.num_workers == 0:
            create_env_fn = create_env
        else:
            create_env_fn = [
                create_env,
            ] * cfg.num_workers
        collector = instantiate(
            cfg.collector, policy=policy, create_env_fn=create_env_fn
        )
        for data in collector:
            assert data.numel() == 200
            break
        collector.shutdown()

    @pytest.mark.skipif(not _has_gym, reason="No gym found")
    @pytest.mark.skipif(not _has_dmc, reason="No gym found")
    @pytest.mark.parametrize(
        "file,from_pixels",
        [
            ("cartpole", True),
            ("cartpole", False),
            ("halfcheetah", True),
            ("halfcheetah", False),
            ("cheetah", True),
            # ("cheetah",False), # processes fail -- to be investigated
        ],
    )
    def test_env_configs(self, file, from_pixels):
        if from_pixels and torch.cuda.device_count() == 0:
            return pytest.skip("not testing pixel rendering without gpu")

        cfg = hydra.compose(
            "config", overrides=[f"env={file}", f"++env.env.from_pixels={from_pixels}"]
        )

        env = instantiate(cfg.env)

        tensordict = env.rollout(3)
        if from_pixels:
            assert "next_pixels" in tensordict.keys()
            assert tensordict["next_pixels"].shape[-1] == 3
        env.rollout(3)
        env.close()
        del env

    @pytest.mark.skipif(not _has_gym, reason="No gym found")
    @pytest.mark.skipif(not _has_dmc, reason="No gym found")
    @pytest.mark.parametrize(
        "env_file,transform_file",
        [
            ["cartpole", "pixels"],
            ["halfcheetah", "pixels"],
            # ["cheetah", "pixels"],
            ["cartpole", "state"],
            ["halfcheetah", "state"],
            ["cheetah", "state"],
        ],
    )
    def test_transforms_configs(self, env_file, transform_file):
        if transform_file == "state":
            from_pixels = False
        else:
            if torch.cuda.device_count() == 0:
                return pytest.skip("not testing pixel rendering without gpu")
            from_pixels = True
        cfg = hydra.compose(
            "config",
            overrides=[
                f"env={env_file}",
                f"++env.env.from_pixels={from_pixels}",
                f"transforms={transform_file}",
            ],
        )

        env = instantiate(cfg.env)
        transforms = [instantiate(transform) for transform in cfg.transforms]
        for t in transforms:
            env.append_transform(t)
        env.rollout(3)
        env.close()
        del env

    @pytest.mark.parametrize(
        "file",
        [
            "circular",
            "prioritized",
        ],
    )
    @pytest.mark.parametrize(
        "size",
        [
            "10",
            None,
        ],
    )
    def test_replaybuffer(self, file, size):
        args = [f"replay_buffer={file}"]
        if size is not None:
            args += [f"replay_buffer.size={size}"]
        cfg = hydra.compose("config", overrides=args)
        replay_buffer = instantiate(cfg.replay_buffer)
        assert replay_buffer._capacity == replay_buffer._storage.size


def make_actor_dqn(net_partial, actor_partial, env, out_features=None):
    if out_features is not None:
        out_features = [out_features] + list(env.action_spec.shape)
    else:
        out_features = list(env.action_spec.shape)
    network = net_partial.network(out_features=out_features)
    actor = actor_partial.actor(module=network, in_keys=net_partial.in_keys)
    return actor


def make_model_ppo(net_partial, model_partial, env):
    out_features = env.action_spec.shape[-1] * model_partial.out_features_multiplier
    # build the module
    policy_network = net_partial.policy_network(out_features=out_features)
    # Let's check that this module does not need to be instantiated further
    if hasattr(model_partial, "module_wrapper"):
        # e.g. for NormalParamWrapper
        policy_network = model_partial.module_wrapper(policy_network)
    # we need to wrap our policy in a TensorDictModule
    policy_operator = model_partial.actor_tensordict(
        policy_network, in_keys=net_partial.in_keys_policy_module
    )
    actor_critic = net_partial.actor_critic(policy_operator=policy_operator)
    # actor = actor_critic.get_policy_operator()
    # critic = actor_critic.get_value_operator()
    return actor_critic


@pytest.mark.skipif(not _has_hydra, reason="No hydra found")
class TestModelConfigs:
    @pytest.fixture(scope="class", autouse=True)
    def init_hydra(self, request):
        GlobalHydra.instance().clear()
        hydra.initialize("../examples/configs/")
        request.addfinalizer(GlobalHydra.instance().clear)

    @pytest.mark.parametrize("pixels", [True, False])
    @pytest.mark.parametrize("distributional", [True, False])
    def test_dqn(self, pixels, distributional):
        env_config = ["env=cartpole"]
        if pixels:
            net_conf = "network=dqn/pixels"
            env_config += ["transforms=pixels", "++env.env.from_pixels=True"]
        else:
            net_conf = "network=dqn/state"
            env_config += ["transforms=state"]
        if distributional:
            model_conf = "model=dqn/distributional"
        else:
            model_conf = "model=dqn/regular"

        cfg = hydra.compose("config", overrides=env_config + [net_conf] + [model_conf])
        env = instantiate(cfg.env)
        transforms = [instantiate(transform) for transform in cfg.transforms]
        for t in transforms:
            env.append_transform(t)

        actor_partial = instantiate(cfg.model)
        net_partial = instantiate(cfg.network)
        out_features = cfg.model.out_features
        make_actor_dqn(net_partial, actor_partial, env, out_features)

    @pytest.mark.parametrize("pixels", [True, False])
    @pytest.mark.parametrize("independent", [True, False])
    @pytest.mark.parametrize("continuous", [True, False])
    def test_ppo(self, pixels, independent, continuous):
        env_config = ["env=cartpole"]
        if independent:
            prefix = "independent"
        else:
            prefix = "shared"
        if pixels:
            suffix = "pixels"
            env_config += ["transforms=pixels", "++env.env.from_pixels=True"]
        else:
            suffix = "state"
            env_config += ["transforms=state"]
        net_conf = f"network=ppo/{prefix}_{suffix}"

        if continuous:
            model_conf = "model=ppo/continuous"
        else:
            model_conf = "model=ppo/discrete"

        cfg = hydra.compose("config", overrides=env_config + [net_conf] + [model_conf])
        env = instantiate(cfg.env)
        transforms = [instantiate(transform) for transform in cfg.transforms]
        for t in transforms:
            env.append_transform(t)

        actor_partial = instantiate(cfg.model)
        net_partial = instantiate(cfg.network)
        actorcritic = make_model_ppo(net_partial, actor_partial, env)
        rollout = env.rollout(3)
        assert all(key in rollout.keys() for key in actorcritic.in_keys), (
            actorcritic.in_keys,
            rollout.keys(),
        )
        tensordict = actorcritic(rollout)
        assert env.action_spec.is_in(tensordict["action"])


if __name__ == "__main__":
    args, unknown = argparse.ArgumentParser().parse_known_args()
    pytest.main([__file__, "--capture", "no", "--exitfirst"] + unknown)
