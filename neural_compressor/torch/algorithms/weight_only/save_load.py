# Copyright (c) 2024 Intel Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# pylint:disable=import-error

import json
import os

import torch

from neural_compressor.common.utils import load_config_mapping, save_config_mapping
from neural_compressor.torch.utils import QCONFIG_NAME, WEIGHT_NAME, logger


def save(model, output_dir="./saved_results"):
    if not os.path.exists(output_dir):
        os.mkdir(output_dir)
    qmodel_file_path = os.path.join(os.path.abspath(os.path.expanduser(output_dir)), WEIGHT_NAME)
    qconfig_file_path = os.path.join(os.path.abspath(os.path.expanduser(output_dir)), QCONFIG_NAME)
    # saving process
    save_config_mapping(model.qconfig, qconfig_file_path)

    if hasattr(model, "gptq_config") and model.gptq_config:
        gptq_config_path = os.path.join(os.path.abspath(os.path.expanduser(output_dir)), "gptq_config.json")
        with open(gptq_config_path, "w") as f:
            json.dump(model.gptq_config, f, indent=4)

    # MethodType 'save' not in state_dict
    del model.save
    torch.save(model, qmodel_file_path)

    logger.info("Save quantized model to {}.".format(qmodel_file_path))
    logger.info("Save configuration of quantized model to {}.".format(qconfig_file_path))


def load(output_dir="./saved_results"):
    qmodel_file_path = os.path.join(os.path.abspath(os.path.expanduser(output_dir)), WEIGHT_NAME)
    model = torch.load(qmodel_file_path)
    logger.info("Quantized model loading successful.")
    return model
