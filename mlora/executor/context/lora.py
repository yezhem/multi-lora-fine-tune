import logging
from collections import OrderedDict
from typing import Dict, override

import torch

from mlora.config import LoRAConfig
from mlora.model.args import LinearInfo
from mlora.model.modules import LoRA

from .context import TaskContext
from .inference import InferenceTaskContext
from .train import TrainTaskContext


def _load_lora_weight(
    context: TaskContext,
    config: LoRAConfig,
    linears_info: OrderedDict[str, LinearInfo],
):
    # init the weight
    for linear_name, linear_info in linears_info.items():
        target_name = linear_name.split(".")[3]
        if target_name not in config.target_:
            continue
        if config.target_[target_name] is not True:
            continue

        context.adapter_model_[linear_name] = LoRA(
            config.name_,
            linear_info.in_dim_,
            linear_info.out_dim_,
            config.r_,
            config.alpha_,
            config.dropout_,
        )
    weight_dict = None
    prefix_name = "base_model.model.model."

    for name, module in context.adapter_model_.items():
        lora_a = (
            None
            if weight_dict is None
            else weight_dict[prefix_name + name + ".lora_A.weight"]
        )
        lora_b = (
            None
            if weight_dict is None
            else weight_dict[prefix_name + name + ".lora_B.weight"]
        )
        module.init_weight(lora_a, lora_b)


class InferenceLoRAContext(InferenceTaskContext):
    config_: LoRAConfig

    def __init__(
        self, config: LoRAConfig, linears_info: OrderedDict[str, LinearInfo]
    ) -> None:
        super().__init__(config, linears_info)

    @override
    def load_weight(self, linears_info: OrderedDict[str, LinearInfo]):
        _load_lora_weight(self, self.config_, linears_info)


class TrainLoRAContext(TrainTaskContext):
    def __init__(
        self,
        config: LoRAConfig,
        linears_info: OrderedDict[str, LinearInfo],
    ) -> None:
        super().__init__(config, linears_info)

        self.loss_fn_ = torch.nn.CrossEntropyLoss()

    @override
    def load_weight(self, linears_info: OrderedDict[str, LinearInfo]):
        _load_lora_weight(self, self.config_, linears_info)

    @override
    def weight_dict(self) -> Dict[str, torch.Tensor]:
        # base_model.model.model.layers.{0}.self_attn.{q_proj}.{lora_A}.weight
        # base_model.model.model.layers.{0}.mlp.{gate_proj}.{lora_A}.weight
        ret_val = {}
        for name, adapter in self.adapter_model_.items():
            prefix_name = "base_model.model.model." + name
            ret_val[prefix_name + ".lora_A.weight"] = adapter.lora_a_
            ret_val[prefix_name + ".lora_B.weight"] = adapter.lora_b_

        return ret_val

    @override
    def state_dict(self) -> Dict[str, torch.Tensor]: ...

    @override
    def recover_optimizer(self, state_dict: Dict[str, torch.Tensor]): ...

    @override
    def recover_weight(self, weight_dict: Dict[str, torch.Tensor]): ...
