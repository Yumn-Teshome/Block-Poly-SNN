import torch
from torch import Tensor, dtype
import torch.nn as nn
import numpy as np
from brainbox.models import BBModel

from block.nn.surrogate import FastSigmoid
import block.nn.methods as methods


METHOD_STANDARD = "standard"
METHOD_FAST_NAIVE = "fast_naive"
METHOD_FAST_OPTIMISED = "fast_optimised"


class BaseNeurons(BBModel):

    def __init__(self, method, t_len, beta_init=[0.9], beta_requires_grad=False, spike_func=FastSigmoid.apply, scale=100, **kwargs):
        super().__init__()
        self._method = method
        self._t_len = t_len
        self._beta_init = beta_init
        self._beta_requires_grad = beta_requires_grad
        self._spike_func = spike_func
        self._scale = scale

        self._beta = nn.Parameter(data=torch.Tensor(beta_init), requires_grad=beta_requires_grad)
        self._method_func = self._get_method_func(t_len, **kwargs)

    @property
    def hyperparams(self):
        return {**super().hyperparams, "method": self._method, "t_len": self._t_len, "beta_init": self._beta_init, "beta_requires_grad": self._beta_requires_grad, "spike_func": self._spike_func.__class__.__name__}

    @property
    def beta(self):
        return torch.clamp(self._beta, min=0.00, max=1)

    def forward(self, x, v_init=None, return_type=methods.RETURN_SPIKES):
        # current: b x n x t
        # beta: n
        # v_init: b x n
        return self._method_func(x, self.beta, v_init, return_type)

    def get_recurrent_current(self, spikes):
        raise NotImplementedError

    def _get_method_func(self, t_len, **kwargs):
        if self._method == METHOD_STANDARD:
            recurrent_source = self.get_recurrent_current if kwargs.get("recurrent", False) else None
            return methods.MethodStandard(t_len, self._spike_func, self._scale, kwargs.get("single_spike", False), kwargs.get("integrator", False), recurrent_source)
        elif self._method == METHOD_FAST_NAIVE:
            return methods.MethodFastNaive(t_len, self._spike_func, self._scale, self.beta)
        elif self._method == METHOD_FAST_OPTIMISED:
            raise NotImplementedError


class LinearNeurons(BaseNeurons):

    def __init__(self, n_in, n_out, method, t_len, beta_init=[0.9], beta_requires_grad=False, spike_func=FastSigmoid.apply, scale=10, **kwargs):
        super().__init__(method, t_len, beta_init, beta_requires_grad, spike_func, scale, **kwargs)
        self._n_in = n_in
        self._n_out = n_out

        self._to_current = nn.Linear(n_in, n_out)
        #self._to_recurrent_current = nn.Linear(n_out, n_out)
        self.init_weight(self._to_current.weight, "uniform", a=-np.sqrt(1 / n_in), b=np.sqrt(1 / n_in))
        self.init_weight(self._to_current.bias, "constant", c=0)

    @property
    def hyperparams(self):
        return {**super().hyperparams, "n_in": self._n_in, "n_out": self._n_out}

    # def get_recurrent_current(self, spikes):
    #     return self._to_recurrent_current(spikes)

    def forward(self, x, v_init=None, return_type=methods.RETURN_SPIKES):
        x = x.permute(0, 2, 1)
        current = self._to_current(x)
        current = current.permute(0, 2, 1)
        spikes = super().forward(current, v_init, return_type)

        return spikes


class ConvNeurons(BaseNeurons):

    def __init__(self, n_in, n_out, kernel, stride, method, t_len, beta_init=[0.9], beta_requires_grad=False, spike_func=FastSigmoid.apply, scale=10, **kwargs):
        super().__init__(method, t_len, beta_init, beta_requires_grad, spike_func, scale, **kwargs)
        self._n_in = n_in
        self._n_out = n_out
        self._kernel = kernel
        self._stride = stride
        self._flatten = kwargs.get("flatten", False)

        self._to_current = nn.Conv3d(n_in, n_out, (1, kernel, kernel), (1, stride, stride))

        sc = kwargs.get("sc", 1)
        if sc is not None:
            n_in = kernel * kernel * n_in
            self.init_weight(self._to_current.weight, "uniform", a=-sc*np.sqrt(1 / n_in), b=sc*np.sqrt(1 / n_in))
        else:
            self.init_weight(self._to_current.weight, "glorot_normal")
        self.init_weight(self._to_current.bias, "constant", c=0)

    @property
    def hyperparams(self):
        return {**super().hyperparams, "n_in": self._n_in, "n_out": self._n_out, "kernel": self._kernel, "stride": self._stride}

    def forward(self, x, v_init=None, return_type=methods.RETURN_SPIKES):
        current = self._to_current(x)
        b, n, t, h, w = current.shape

        current = current.permute(0, 1, 3, 4, 2)
        current = current.flatten(start_dim=1, end_dim=3)
        spikes = super().forward(current, v_init, return_type)

        if not self._flatten:
            spikes = spikes.view(b, n, h, w, t)
            spikes = spikes.permute(0, 1, 4, 2, 3)

        return spikes

# Beginning of Yumn's edits
class PolyNeurons(BaseNeurons):
    def __init__(self, n_in, n_out, method, t_len, beta_init=[0.9], beta_requires_grad=False, spike_func=FastSigmoid.apply, scale=10, **kwargs):
        super().__init__(method, t_len, beta_init, beta_requires_grad, spike_func, scale, **kwargs)
        self._n_in = n_in
        self._n_out = n_out

        self.fc1 = nn.Linear(n_in, n_out)
        self.fc2 = nn.Linear(n_in, n_out)
        self.init_weight(self.fc1.weight, "uniform", a=-np.sqrt(1 / n_in), b=np.sqrt(1 / n_in))
        self.init_weight(self.fc1.bias, "constant", c=0)
        self.init_weight(self.fc2.weight, "uniform", a=-np.sqrt(1 / n_in), b=np.sqrt(1 / n_in))
        self.init_weight(self.fc2.bias, "constant", c=0)


    @property
    def hyperparams(self):
        return {**super().hyperparams, "n_in": self._n_in, "n_out": self._n_out}

    # def get_recurrent_current(self, spikes):
    #     return self._to_recurrent_current(spikes)
    
    def _to_current(self, x):
        # "Polynomial Neural Networks" formulation:
        return (self.fc2(x)+1) * self.fc1(x)
        # Alternative formulation:
        # return self.fc1(x) + (self.fc2(x))**2

    def forward(self, x, v_init=None, return_type=methods.RETURN_SPIKES):

        x = x.permute(0, 2, 1)
        current = self._to_current(x)
        current = current.permute(0, 2, 1)
        spikes = super().forward(current, v_init, return_type)

        return spikes

class PolyConvNeurons(BaseNeurons):

    def __init__(self, n_in, n_out, kernel, stride, method, t_len, beta_init=[0.9], beta_requires_grad=False, spike_func=FastSigmoid.apply, scale=10, **kwargs):
        super().__init__(method, t_len, beta_init, beta_requires_grad, spike_func, scale, **kwargs)
        self._n_in = n_in
        self._n_out = n_out
        self._kernel = kernel
        self._stride = stride
        self._flatten = kwargs.get("flatten", False)

        self.conv_1 = nn.Conv3d(n_in, n_out, (1, kernel, kernel), (1, stride, stride))
        self.conv_2 = nn.Conv3d(n_in, n_out, (1, kernel, kernel), (1, stride, stride))

        #self._to_current = QuadConv(n_in, n_out, (1, kernel, kernel), (1, stride, stride))
        self._to_current = lambda x: self.conv_1(x) * (1 + self.conv_2(x))

        sc = kwargs.get("sc", 1)
        if sc is not None:
            n_in = kernel * kernel * n_in
            self.init_weight(self.conv_1.weight, "uniform", a=-sc*np.sqrt(1 / n_in), b=sc*np.sqrt(1 / n_in))
            self.init_weight(self.conv_2.weight, "uniform", a=-sc*np.sqrt(1 / n_in), b=sc*np.sqrt(1 / n_in))
        else:
            self.init_weight(self.conv_1.weight, "glorot_normal")
            self.init_weight(self.conv_2.weight, "glorot_normal")
        self.init_weight(self.conv_1.bias, "constant", c=0)
        self.init_weight(self.conv_2.bias, "constant", c=0)

    @property
    def hyperparams(self):
        return {**super().hyperparams, "n_in": self._n_in, "n_out": self._n_out, "kernel": self._kernel, "stride": self._stride}

    def forward(self, x, v_init=None, return_type=methods.RETURN_SPIKES):
        current = self._to_current(x)
        b, n, t, h, w = current.shape

        current = current.permute(0, 1, 3, 4, 2)
        current = current.flatten(start_dim=1, end_dim=3)
        spikes = super().forward(current, v_init, return_type)

        if not self._flatten:
            spikes = spikes.view(b, n, h, w, t)
            spikes = spikes.permute(0, 1, 4, 2, 3)

        return spikes

"""class QuadConv(nn.Module):

    __constants__ = ['in_features', 'out_features']
    in_features: int
    out_features: int
    conv_1: nn.Conv3d
    conv_2: nn.Conv3d

    def __init__(self, in_features: int, out_features: int,
                 kernel, stride,
                 #bias_1: bool = True, bias_2: bool = True,
                 device=None, dtype=None) -> None:
        factory_kwargs = {'device': device, 'dtype': dtype}
        super().__init__()
        self.in_features = in_features
        self.out_features = out_features
        self.conv_1 = nn.Conv3d(in_features, out_features, (1, kernel, kernel), (1, stride, stride))
        self.conv_2 = nn.Conv3d(in_features, out_features, (1, kernel, kernel), (1, stride, stride))

    def reset_parameters(self) -> None:
        self.conv_1.reset_parameters()
        self.conv_2.reset_parameters()

    def forward(self, input: Tensor) -> Tensor:
        return self.conv_1(input) * (1 + self.conv_2(input))
"""
