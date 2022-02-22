import copy
from collections import defaultdict
from typing import Any, Dict, List, no_type_check

import numpy as np

import torch
from torch import nn


class SaveHook:
    def __init__(self) -> None:
        self.saved: List[torch.Tensor] = []

    @no_type_check
    def __call__(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError()

    def clear(self) -> None:
        self.saved = []


class SaveInputHook(SaveHook):
    def __call__(self, module: nn.Module, inputs: torch.Tensor) -> None:
        self.saved.append(inputs[0].cpu().detach())


class SaveOutputHook(SaveHook):
    def __call__(self, module: nn.Module, inputs: torch.Tensor, outputs: torch.Tensor) -> None:
        self.saved.append(outputs.cpu().detach())


class AttentionWeightsHook(SaveOutputHook):
    def __call__(self, module: nn.Module, inputs: torch.Tensor, outputs: torch.Tensor) -> None:
        self.saved.append(outputs[1].cpu().detach())


class ModelHook:
    def __init__(self) -> None:
        self.attached_hooks: Dict[str, Dict[str, SaveOutputHook]] = defaultdict(dict)

    def clear(self, name: str = None) -> None:
        for k, v in self.attached_hooks.items():
            if name is not None and k != name:
                continue
            for name, hook in v.items():
                hook.clear()

    def attach(self,
               name: str,
               model: nn.Module,
               hook: SaveOutputHook,
               attach_to_cls: nn.Module,
               layer_name: str = None,
               pre_hook: bool = False) -> None:
        for module_name, module in model.named_modules():
            if isinstance(module, attach_to_cls):
                if layer_name is not None:
                    if layer_name != module_name:
                        continue
                hook_cp = copy.deepcopy(hook)
                self.attached_hooks[name][module_name] = hook_cp
                if pre_hook:
                    module.register_forward_pre_hook(hook_cp)
                else:
                    module.register_forward_hook(hook_cp)

    def __getitem__(self, name: str) -> Dict[str, List[np.ndarray]]:
        if name not in self.attached_hooks:
            raise ValueError(f"Invalid key {name}")
        tensor_dict = self.attached_hooks[name]
        return {n: [t.numpy() for t in v.saved] for n, v in tensor_dict.items()}
