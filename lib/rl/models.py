import numpy as np
import torch.nn as nn
import torch
from copy import deepcopy
from gym import spaces
import rl_games.common.divergence as divergence
from rl_games.common.extensions.distributions import CategoricalMasked

# from rl_games.algos_torch.running_mean_std import RunningMeanStd

from lib.rl.moving_avg import RunningMeanStd, RunningMeanStdObs


class BaseModel:
    def __init__(self, model_class):
        self.model_class = model_class

    def is_rnn(self):
        return False

    def is_separate_critic(self):
        return False

    def get_value_layer(self):
        return None

    def build(self, config):
        obs_shape = config["input_shape"]
        normalize_value = config.get("normalize_value", False)
        normalize_input = config.get("normalize_input", False)
        normalize_input_excluded_keys = config.get("normalize_input_excluded_keys", None)
        value_size = config.get("value_size", 1)
        return self.Network(
            self.network_builder.build(self.model_class, **config),
            obs_shape=obs_shape,
            normalize_value=normalize_value,
            normalize_input=normalize_input,
            value_size=value_size,
            normalize_input_excluded_keys=normalize_input_excluded_keys,
            **(config.get("model", {})),
        )


class BaseModelNetwork(nn.Module):
    def __init__(
        self,
        obs_shape,
        normalize_value,
        normalize_input,
        value_size,
        normalize_input_excluded_keys=None,
        **kwargs,
    ):
        nn.Module.__init__(self)
        self.obs_shape = obs_shape
        self.normalize_value = normalize_value
        self.normalize_input = normalize_input
        self.value_size = value_size

        if normalize_value:
            self.value_mean_std = RunningMeanStd((self.value_size,))  # GeneralizedMovingStats((self.value_size,)) #
        if normalize_input:
            if isinstance(obs_shape, spaces.Dict):
                self.running_mean_std = RunningMeanStdObs(obs_shape, exclude_keys=normalize_input_excluded_keys)
            else:
                self.running_mean_std = RunningMeanStd(obs_shape)

    def norm_obs(self, observation):
        with torch.no_grad():
            return self.running_mean_std(observation) if self.normalize_input else observation

    def denorm_value(self, value):
        with torch.no_grad():
            return self.value_mean_std(value, denorm=True) if self.normalize_value else value


class ModelA2C(BaseModel):
    def __init__(self, network):
        BaseModel.__init__(self, "a2c")
        self.network_builder = network

    class Network(BaseModelNetwork):
        def __init__(self, a2c_network, **kwargs):
            BaseModelNetwork.__init__(self, **kwargs)
            self.a2c_network = a2c_network

        def is_rnn(self):
            return self.a2c_network.is_rnn()

        def get_default_rnn_state(self):
            return self.a2c_network.get_default_rnn_state()

        def get_value_layer(self):
            return self.a2c_network.get_value_layer()

        def kl(self, p_dict, q_dict):
            p = p_dict["logits"]
            q = q_dict["logits"]
            return divergence.d_kl_discrete(p, q)

        def forward(self, input_dict):
            is_train = input_dict.get("is_train", True)
            action_masks = input_dict.get("action_masks", None)
            prev_actions = input_dict.get("prev_actions", None)
            input_dict["obs"] = self.norm_obs(input_dict["obs"])
            logits, value, states = self.a2c_network(input_dict)

            if is_train:
                categorical = CategoricalMasked(logits=logits, masks=action_masks)
                prev_neglogp = -categorical.log_prob(prev_actions)
                entropy = categorical.entropy()
                result = {
                    "prev_neglogp": torch.squeeze(prev_neglogp),
                    "logits": categorical.logits,
                    "values": value,
                    "entropy": entropy,
                    "rnn_states": states,
                }
                return result
            else:
                categorical = CategoricalMasked(logits=logits, masks=action_masks)
                selected_action = categorical.sample().long()
                neglogp = -categorical.log_prob(selected_action)
                result = {
                    "neglogpacs": torch.squeeze(neglogp),
                    "values": self.denorm_value(value),
                    "actions": selected_action,
                    "logits": categorical.logits,
                    "rnn_states": states,
                }
                return result


class ModelA2CContinuousLogStd(BaseModel):
    def __init__(self, network):
        BaseModel.__init__(self, "a2c")
        self.network_builder = network

    class Network(BaseModelNetwork):
        def __init__(self, a2c_network, **kwargs):
            BaseModelNetwork.__init__(self, **kwargs)
            self.a2c_network = a2c_network

        def is_rnn(self):
            return self.a2c_network.is_rnn()

        def get_value_layer(self):
            return self.a2c_network.get_value_layer()

        def get_default_rnn_state(self):
            return self.a2c_network.get_default_rnn_state()

        def forward(self, input_dict):
            is_train = input_dict.get("is_train", True)
            prev_actions = input_dict.get("prev_actions", None)
            input_dict["obs"] = self.norm_obs(input_dict["obs"])
            mu, logstd, value, states = self.a2c_network(input_dict)
            sigma = torch.exp(logstd)
            distr = torch.distributions.Normal(mu, sigma, validate_args=False)
            if is_train:
                entropy = distr.entropy().sum(dim=-1)
                prev_neglogp = self.neglogp(prev_actions, mu, sigma, logstd)
                result = {
                    "prev_neglogp": torch.squeeze(prev_neglogp),
                    "values": value,
                    "entropy": entropy,
                    "rnn_states": states,
                    "mus": mu,
                    "sigmas": sigma,
                }
                return result
            else:
                selected_action = distr.sample()
                neglogp = self.neglogp(selected_action, mu, sigma, logstd)
                result = {
                    "neglogpacs": torch.squeeze(neglogp),
                    "values": self.denorm_value(value),
                    "actions": selected_action,
                    "rnn_states": states,
                    "mus": mu,
                    "sigmas": sigma,
                }
                return result

        def neglogp(self, x, mean, std, logstd):
            return (
                0.5 * (((x - mean) / std) ** 2).sum(dim=-1)
                + 0.5 * np.log(2.0 * np.pi) * x.size()[-1]
                + logstd.sum(dim=-1)
            )


class SepModelA2CContinuousLogStd(BaseModel):
    def __init__(self, network):
        BaseModel.__init__(self, "a2c")
        self.network_builder = network

    class Network(nn.Module):
        def __init__(self, a2c_network, **kwargs):
            super().__init__()
            input_kwargs = deepcopy(kwargs)

            def sep_shape(x):
                return spaces.Box(
                    low=-np.inf,
                    high=np.inf,
                    shape=(x.shape[0] // 2,),
                )

            input_kwargs["obs_shape"] = spaces.Dict({k: sep_shape(v) for k, v in input_kwargs["obs_shape"].items()})

            self.left_hand_base = BaseModelNetwork(**input_kwargs)
            self.right_hand_base = BaseModelNetwork(**input_kwargs)
            self.a2c_network = a2c_network

        def is_rnn(self):
            return self.a2c_network.is_rnn()

        def get_value_layer(self):
            return self.a2c_network.get_value_layer()

        def get_default_rnn_state(self):
            return self.a2c_network.get_default_rnn_state()

        def load_state_dict(self, state_dict):
            assert type(state_dict) is list
            self.right_hand_base.load_state_dict({k: v for k, v in state_dict[0].items() if "a2c_network" not in k})
            self.left_hand_base.load_state_dict({k: v for k, v in state_dict[1].items() if "a2c_network" not in k})
            self.a2c_network.right_hand_net.load_state_dict(
                {k[12:]: v for k, v in state_dict[0].items() if "a2c_network" in k}
            )
            self.a2c_network.left_hand_net.load_state_dict(
                {k[12:]: v for k, v in state_dict[1].items() if "a2c_network" in k}
            )

        def forward(self, input_dict):
            is_train = input_dict.get("is_train", True)
            prev_actions = input_dict.get("prev_actions", None)
            lh_input_dict = deepcopy(input_dict)
            rh_input_dict = deepcopy(input_dict)
            for k, v in input_dict["obs"].items():
                rh_input_dict["obs"][k] = v[:, : v.shape[1] // 2]
                lh_input_dict["obs"][k] = v[:, v.shape[1] // 2 :]
            lh_input_dict["obs"] = self.left_hand_base.norm_obs(lh_input_dict["obs"])
            rh_input_dict["obs"] = self.right_hand_base.norm_obs(rh_input_dict["obs"])
            lh_mu, lh_logstd, lh_value, lh_states = self.a2c_network.left_hand_net(lh_input_dict)  # ! TODO
            lh_sigma = torch.exp(lh_logstd)
            lh_distr = torch.distributions.Normal(lh_mu, lh_sigma, validate_args=False)
            rh_mu, rh_logstd, rh_value, rh_states = self.a2c_network.right_hand_net(rh_input_dict)
            rh_sigma = torch.exp(rh_logstd)
            rh_distr = torch.distributions.Normal(rh_mu, rh_sigma, validate_args=False)
            if is_train:
                assert False, "Not implemented"
                entropy = distr.entropy().sum(dim=-1)
                prev_neglogp = self.neglogp(prev_actions, mu, sigma, logstd)
                result = {
                    "prev_neglogp": torch.squeeze(prev_neglogp),
                    "values": value,
                    "entropy": entropy,
                    "rnn_states": states,
                    "mus": mu,
                    "sigmas": sigma,
                }
                return result
            else:
                rh_selected_action = rh_distr.sample()
                rh_neglogp = self.neglogp(rh_selected_action, rh_mu, rh_sigma, rh_logstd)
                rh_result = {
                    "neglogpacs": torch.squeeze(rh_neglogp),
                    "values": self.right_hand_base.denorm_value(rh_value),  # ! TODO
                    "actions": rh_selected_action,
                    "rnn_states": rh_states,
                    "mus": rh_mu,
                    "sigmas": rh_sigma,
                }
                lh_selected_action = lh_distr.sample()
                lh_neglogp = self.neglogp(lh_selected_action, lh_mu, lh_sigma, lh_logstd)
                lh_result = {
                    "neglogpacs": torch.squeeze(lh_neglogp),
                    "values": self.left_hand_base.denorm_value(lh_value),  # ! TODO
                    "actions": lh_selected_action,
                    "rnn_states": lh_states,
                    "mus": lh_mu,
                    "sigmas": lh_sigma,
                }
                result = {}
                for k in rh_result.keys():
                    if rh_result[k] is None:
                        result[k] = None
                        continue
                    result[k] = torch.cat([rh_result[k], lh_result[k]], dim=-1)
                return result

        def neglogp(self, x, mean, std, logstd):
            return (
                0.5 * (((x - mean) / std) ** 2).sum(dim=-1)
                + 0.5 * np.log(2.0 * np.pi) * x.size()[-1]
                + logstd.sum(dim=-1)
            )
