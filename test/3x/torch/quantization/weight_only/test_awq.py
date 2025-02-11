import copy
import shutil

import pytest
import torch
import transformers

from neural_compressor.common import Logger

logger = Logger().get_logger()
from neural_compressor.torch.algorithms.weight_only.modules import WeightOnlyLinear
from neural_compressor.torch.quantization import AWQConfig, convert, get_default_awq_config, prepare, quantize


def get_gpt_j():
    tiny_gptj = transformers.AutoModelForCausalLM.from_pretrained(
        "hf-internal-testing/tiny-random-GPTJForCausalLM",
        torchscript=True,
    )
    return tiny_gptj


class TestAWQQuant:
    @classmethod
    def setup_class(self):
        self.tiny_gptj = transformers.AutoModelForCausalLM.from_pretrained(
            "hf-internal-testing/tiny-random-GPTJForCausalLM",
        )
        self.example_inputs = torch.ones([1, 10], dtype=torch.long)
        self.label = self.tiny_gptj(self.example_inputs)[0]

    def teardown_class(self):
        shutil.rmtree("saved_results", ignore_errors=True)

    @pytest.mark.parametrize(
        "bits, use_sym, group_size",
        [
            (8, True, -1),
            (4, True, 128),
            (4, False, 32),
            (4, False, -1),
            (2, True, 8),
        ],
    )
    def test_awq(self, bits, use_sym, group_size):
        model = copy.deepcopy(self.tiny_gptj)

        @torch.no_grad()
        def calib_func(model):
            for i in range(2):
                model(self.example_inputs)

        quant_config = AWQConfig(bits=8, group_size=-1)
        logger.info(f"Test AWQ with config {quant_config}")
        model = prepare(
            model=model,
            quant_config=quant_config,
            example_inputs=self.example_inputs,
        )
        calib_func(model)
        qdq_model = convert(model)
        out = qdq_model(self.example_inputs)[0]

        # default awq_quantize is 4 bits, 32 group size, use big atol=1e-1
        if (bits, use_sym, group_size) == (8, True, -1):
            assert torch.allclose(out, self.label, atol=1e-2), "Accuracy gap atol > 0.01 is unexpected."
        elif (bits, use_sym, group_size) == (2, True, 8):
            assert torch.allclose(out, self.label, atol=0.5), "Accuracy gap atol > 0.5 is unexpected."
        else:
            assert torch.allclose(out, self.label, atol=1e-1), "Accuracy gap atol > 0.01 is unexpected."

    def test_awq_with_quantize_API(self):
        @torch.no_grad()
        def calib_func(model):
            for i in range(2):
                model(self.example_inputs)

        quant_config = get_default_awq_config()
        logger.info(f"Test AWQ with config {quant_config}")

        # prepare + convert API
        model = prepare(
            model=copy.deepcopy(self.tiny_gptj),
            quant_config=quant_config,
            example_inputs=self.example_inputs,
        )
        calib_func(model)
        qdq_model = convert(model)
        out1 = qdq_model(self.example_inputs)

        # quantize API
        qdq_model = quantize(
            model=copy.deepcopy(self.tiny_gptj),
            quant_config=quant_config,
            example_inputs=self.example_inputs,
            run_fn=calib_func,
        )
        out2 = qdq_model(self.example_inputs)

        # compare the results of calling `convert` + `prepare` and calling `quantize`
        assert torch.all(
            out1[0].eq(out2[0])
        ), "The results of calling `convert` + `prepare` and calling `quantize` should be equal."

    def test_save_and_load(self):
        @torch.no_grad()
        def calib_func(model):
            for i in range(2):
                model(self.example_inputs)

        fp32_model = copy.deepcopy(self.tiny_gptj)
        quant_config = get_default_awq_config()

        # prepare + convert API
        model = prepare(
            model=fp32_model,
            quant_config=quant_config,
            example_inputs=self.example_inputs,
        )
        calib_func(model)
        q_model = convert(model)
        assert q_model is not None, "Quantization failed!"
        q_model.save("saved_results")
        inc_out = q_model(self.example_inputs)[0]

        from neural_compressor.torch.quantization import load

        # loading compressed model
        loaded_model = load("saved_results")
        loaded_out = loaded_model(self.example_inputs)[0]
        assert torch.allclose(inc_out, loaded_out), "Unexpected result. Please double check."
        assert isinstance(loaded_model.lm_head, WeightOnlyLinear), "loading compressed model failed."
