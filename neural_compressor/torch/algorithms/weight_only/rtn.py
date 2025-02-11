#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright (c) 2024 MIT HAN Lab
# This source code is licensed under the MIT license
#
# Copyright (c) 2024 Intel Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


from collections import OrderedDict

import torch

from neural_compressor.torch.algorithms import Quantizer
from neural_compressor.torch.utils import get_device, is_transformers_imported, logger, set_module

from .utility import cast_fp8, quant_tensor, search_clip

if is_transformers_imported():
    import transformers


class RTNQuantizer(Quantizer):
    def __init__(self, quant_config: OrderedDict = {}):
        """Init a RTNQuantizer object.

        Args:
            quant_config (OrderedDict, optional): quantization config for ops. Defaults to {}.
        """
        super().__init__(quant_config)

    @torch.no_grad()
    def prepare(self, model, *args, **kwargs):
        """Prepares a given model for quantization.

        Will return model directly in RTN algorithm.

        Args:
            model (torch.nn.Module): The model to be prepared.
        """
        return model

    @torch.no_grad()
    def convert(
        self,
        model,
        dtype="int",
        bits=4,
        scheme="sym",
        group_size=32,
        group_dim=1,
        quantile=1.0,
        export_compressed_model=True,
        use_full_range=False,
        use_mse_search=False,
        *args,
        **kwargs,
    ):
        """Quant the model with round to nearest method and inplace is True.

        Args:
            model: torch module
            dtype (str, optional): select from int, nf4, fp4. Defaults to int.
            bits: num bits. Defaults to 4.
            scheme (str, optional): sym or asym. Defaults to "sym".
            group_size (int, optional): how many elements share one scale/zp. Defaults to 32.
            group_dim (int, optional):  0 means splitting output channel,
                                        1 means splitting input channel. Defaults to 1.
            quantile (float, optional): percentile of clip. Defaults to 1.0.
            export_compressed_model (bool, optional): Choose return fp32 or int32 model.
                                        Defaults to False.
            use_full_range (bool, optional): Choose sym range whether use -2**(bits-1).
                                        Defaults to False.
            use_mse_search (bool, optional):  Whether search clip range.
                                        Defaults to True.

        Returns:
            model: fake quantized torch module
        """
        weight_config = self.quant_config
        device = get_device(kwargs.pop("device", "auto"))

        # Put model on device explicitly
        # TODO: refine it later, Put module on device one by one instead of the whole model
        model.to(device)

        assert isinstance(model, torch.nn.Module), "only support torch module"
        if is_transformers_imported():
            supported_layers = (torch.nn.Linear, transformers.Conv1D)
        else:
            supported_layers = (torch.nn.Linear,)
        # initialize global configuration
        double_quant_config = {
            "double_quant": kwargs.get("use_double_quant", False),
            "double_quant_dtype": kwargs.get("double_quant_dtype", "int"),
            "double_quant_bits": kwargs.get("double_quant_bits", 8),
            "double_quant_scheme": kwargs.get("double_quant_scheme", "sym"),
            "double_quant_group_size": kwargs.get("double_quant_group_size", 256),
        }
        if export_compressed_model:
            use_optimum_format = kwargs.get("use_optimum_format", True)
        for name, m in model.named_modules():
            if not isinstance(m, supported_layers):
                continue
            if name in weight_config:  # pragma: no cover
                # initialize op configuration
                dtype = weight_config[name].get("dtype", "int")
                if dtype == "fp32":
                    continue
                ### FP8 cast part
                if dtype in ["fp8_e5m2", "fp8_e5m2fnuz", "fp8_e4m3fn", "fp8_e4m3fnuz"]:
                    logger.debug("Cast module {} to FP8 using qdq mode, no scaling".format(name))
                    m.weight = cast_fp8(m.weight, dtype, use_qdq=True)
                    continue
                ####
                logger.debug("Apply RTN on module %s.", name)
                bits = weight_config[name].get("bits", 4)
                group_size = weight_config[name]["group_size"]
                scheme = weight_config[name]["scheme"]
                quantile = weight_config[name].get("quantile", 1.0)
                group_dim = weight_config[name]["group_dim"]
                use_full_range = weight_config[name]["use_full_range"]
                use_mse_search = weight_config[name]["use_mse_search"]
                use_layer_wise = weight_config[name]["use_layer_wise"]
                if export_compressed_model:
                    use_optimum_format = kwargs.get("use_optimum_format", True)
                # double quant config
                double_quant_config = {
                    "double_quant": weight_config[name]["use_double_quant"],
                    "double_quant_dtype": weight_config[name]["double_quant_dtype"],
                    "double_quant_bits": weight_config[name]["double_quant_bits"],
                    "double_quant_scheme": weight_config[name]["double_quant_scheme"],
                    "double_quant_group_size": weight_config[name]["double_quant_group_size"],
                }
                if dtype != "int" and "int" in dtype:
                    bits = int(dtype.lstrip("int"))
                    dtype = "int"
            log_msg = (
                f"RTN quantization config: bits={bits}, group_size={group_size}, "
                + f"scheme={scheme}, quantile={quantile}"
            )
            if dtype != "int":
                log_msg += f", dtype={dtype}"
            elif scheme == "sym":  # nf4/fp4 is always [-7,7]
                log_msg += f", use_full_range={use_full_range}"
            if dtype == "fp32":
                continue
            logger.debug(f"RTN quantized module:{name, m}")
            logger.debug(log_msg)
            # for only group_dim is 0 or only `transformers.Conv1D`, we need transpose weight.
            if is_transformers_imported():
                transpose = (group_dim == 0) ^ (isinstance(m, transformers.Conv1D))
            else:
                transpose = group_dim == 0
            if transpose:
                weight = m.weight.t_().contiguous()
            else:
                weight = m.weight
            if use_mse_search:
                quantile = search_clip(m, bits, group_size, scheme, dtype, use_full_range)
            if export_compressed_model:
                int_weight, scale, zp = quant_tensor(
                    weight,
                    dtype=dtype,
                    bits=bits,
                    group_size=group_size,
                    scheme=scheme,
                    quantile=quantile,
                    return_int=True,
                    full_range=use_full_range,
                    **double_quant_config,
                )
                int_weight = int_weight.t_().contiguous() if transpose else int_weight
                scale = scale.t_().contiguous() if transpose else scale
                zp = zp.t_().contiguous() if transpose and zp is not None else zp
                if isinstance(m, torch.nn.Linear):
                    in_features = m.in_features
                    out_features = m.out_features
                elif is_transformers_imported() and isinstance(m, transformers.Conv1D):
                    in_features = m.weight.shape[1]
                    out_features = m.weight.shape[0]
                    int_weight = int_weight.t_().contiguous()
                    scale = scale.t_().contiguous()
                    zp = zp.t_().contiguous() if zp is not None else zp
                from .modules import WeightOnlyLinear

                new_module = WeightOnlyLinear(
                    in_features,
                    out_features,
                    dtype=dtype,
                    bits=bits,
                    group_size=group_size,
                    zp=zp is not None,
                    bias=m.bias is not None,
                    use_optimum_format=use_optimum_format,
                    device=device,
                )
                new_module.pack(int_weight, scale, zp, m.bias)
                if name == "":
                    return new_module
                else:
                    set_module(model, name, new_module)
            else:
                weight = quant_tensor(
                    weight,
                    dtype=dtype,
                    bits=bits,
                    group_size=group_size,
                    scheme=scheme,
                    quantile=quantile,
                    full_range=use_full_range,
                    **double_quant_config,
                )
                if transpose:
                    # for only group_dim is 0 or only `transformers.Conv1D`,
                    # we need to transpose the quantized tensor and module's weight back
                    weight = weight.t_().contiguous()
                    m.weight.t_().contiguous()
                m.weight.data.copy_(weight)
        return model
