import collections
import math
import os
import pickle
import pprint
from typing import Union, List, Optional, Iterable, Dict

import numpy as np
import torch
from tqdm import tqdm

from trt.api.utils import (
    download_model,
    get_device_info,
    split,
    char2char_score_fn,
    sliding_windows,
    match_token_ids_ignoring_space_and_unk
)
from trt.model import transformer, tokenizer
from trt.utils import common, config, io, inference, nlp, tokenization_repair, constants

os.environ["TOKENIZERS_PARALLELISM"] = "false"

__all__ = ["ModelInfo", "get_available_models", "TokenizationRepairer"]

ModelInfo = collections.namedtuple("ModelInfo", ["name", "description"])


def get_available_models() -> List[ModelInfo]:
    return [
        ModelInfo(
            name="eo_large_arxiv_with_errors",
            description="best overall model, use this for text that might have OCR or spelling errors (default)"
        ),
        ModelInfo(
            name="eo_medium_arxiv_with_errors",
            description="compromise between eo_arxiv_with_errors and eo_small_arxiv_with_errors, "
                        "faster than eo_arxiv_with_errors but less accurate, "
                        "slower than eo_small_arxiv_with_errors but more accurate"
        ),
        ModelInfo(
            name="eo_small_arxiv_with_errors",
            description="smallest and fastest, but also the least accurate model, "
                        "use this when you want to repair text with few tokenization errors and "
                        "little to no OCR or spelling errors fast"
        ),
        ModelInfo(
            name="nmt_large_arxiv_with_errors",
            description="nmt model, similar in size to eo_large_arxiv_with_errors"
        ),
        ModelInfo(
            name="nmt_medium_arxiv_with_errors",
            description="nmt model, similar in size to eo_medium_arxiv_with_errors"
        ),
        ModelInfo(
            name="nmt_small_arxiv_with_errors",
            description="nmt model, similar in size to eo_small_arxiv_with_errors"
        )
    ]


StringInputOutput = Union[str, List[str]]


class TokenizationRepairer:
    @staticmethod
    def from_pretrained(
            model: str = get_available_models()[0].name,
            device: Union[str, int] = "cuda",
            cache_dir: Optional[str] = None,
            force_download: bool = False
    ) -> "TokenizationRepairer":
        assert any(model == m.name for m in get_available_models()), \
            f"model {model} does not match any of the available models:\n{pprint.pformat(get_available_models())}"

        if cache_dir is None:
            cache_dir = os.environ.get(
                "TOKENIZATION_REPAIR_CACHE_DIR",
                os.path.join(os.path.dirname(__file__), ".cache")
            )

        logger = common.get_logger("DOWNLOAD")
        model_dir = download_model(model, cache_dir, force_download, logger)

        return TokenizationRepairer(model_dir, device)

    @staticmethod
    def from_experiment(
            experiment_dir: str,
            device: Union[str, int] = "cuda"
    ) -> "TokenizationRepairer":
        return TokenizationRepairer(experiment_dir, device)

    @property
    def model_name(self) -> str:
        return self.cfg.model.name

    def __init__(self,
                 model_dir: str,
                 device: Union[str, int]) -> None:
        self.logger = common.get_logger("TOKENIZATION_REPAIR")

        if device != "cpu" and not torch.cuda.is_available():
            self.logger.info(f"could not find a GPU, using CPU as fallback option")
            device = "cpu"

        self.device = torch.device(device)
        self.logger.info(f"running tokenization repair on device {get_device_info(self.device)}")

        self.cfg = config.Config.from_yaml(os.path.join(model_dir, "config.yaml"))
        self.logger.debug(f"loaded model config:\n{self.cfg.model}")

        self.model = transformer.get_model_from_config(self.cfg.model, self.device)
        best_checkpoint_path = io.glob_safe(os.path.join(model_dir, "checkpoints", "*-checkpoint-best.pt"))[0]
        best_checkpoint = io.load_checkpoint(best_checkpoint_path)
        self.model.load_state_dict(best_checkpoint["model_state_dict"])
        self.model.eval()
        for param in self.model.parameters():
            param.requires_grad = False

        if (
                self.cfg.model.type == "encoder_with_head"
                and self.cfg.model.encoder.tokenizer == "char"
                and self.cfg.model.head.type == "sequence_classification"
                and self.cfg.model.head.arguments.get("num_classes", 0) == 3
        ):
            # set default inference kwargs
            self.inference_kwargs = {
                "temperature": 1.0,
                "temperature_no_spaces": 1.0,
                "thresholds_and_default": None,
                "thresholds_and_default_no_spaces": None
            }

            # check if the corresponding inference pickle files are in the model dir, if so, load them
            temperature_path = os.path.join(model_dir, "temperature.pkl")
            temperature_no_spaces_path = os.path.join(model_dir, "temperature_no_spaces.pkl")
            thresholds_and_default_path = os.path.join(model_dir, "thresholds_and_default.pkl")
            thresholds_and_default_no_spaces_path = os.path.join(model_dir, "thresholds_and_default_no_spaces.pkl")
            if os.path.exists(temperature_path):
                with open(temperature_path, "rb") as tf:
                    temp = pickle.load(tf)
                    self.inference_kwargs["temperature"] = temp
                self.logger.debug(f"found temperature file: setting temperature to {temp}")
            if os.path.exists(temperature_no_spaces_path):
                with open(temperature_no_spaces_path, "rb") as tf:
                    temp_no_spaces = pickle.load(tf)
                    self.inference_kwargs["temperature_no_spaces"] = temp_no_spaces
                self.logger.debug(f"found temperature (no spaces) file: setting temperature to {temp_no_spaces}")
            if os.path.exists(thresholds_and_default_path):
                with open(thresholds_and_default_path, "rb") as tf:
                    thresholds_and_default = pickle.load(tf)
                    self.inference_kwargs["thresholds_and_default"] = thresholds_and_default
                self.logger.debug(f"found thresholds_and_default file: setting thresholds and default to "
                                  f"{thresholds_and_default}")
            if os.path.exists(thresholds_and_default_no_spaces_path):
                with open(thresholds_and_default_no_spaces_path, "rb") as tf:
                    thresholds_and_default_no_spaces = pickle.load(tf)
                    self.inference_kwargs["thresholds_and_default_no_spaces"] = thresholds_and_default_no_spaces
                self.logger.debug(f"found thresholds_and_default (no spaces) file: setting thresholds and default to "
                                  f"{thresholds_and_default_no_spaces}")
        elif (
                self.cfg.model.type == "transformer"
                and self.cfg.model.encoder.tokenizer == "char"
                and self.cfg.model.decoder.tokenizer == "char"
        ):
            self.char_tokenizer = tokenizer.load_tokenizer("char")
            self.unk_token_id = self.char_tokenizer.token_to_id(constants.UNK)
            self.ws_token_id = self.char_tokenizer.token_to_id(" ")
            self.inference_kwargs = {
                "score_fn": char2char_score_fn(self.char_tokenizer)
            }
        else:
            raise RuntimeError(f"model should either be of type encoder_with_head with a char encoder tokenizer and 3 "
                               f"output classes or of type transformer with both a char encoder and decoder tokenizer")

        self.max_length = self.model.encoder.config.max_num_embeddings - 2  # - 2 because of bos and eos tokens
        self.window_size = math.ceil(0.75 * self.max_length)
        self.context_size = math.floor((self.max_length - self.window_size) / 2)

    def _merge_inference_results(
            self,
            inference_results: List[
                Union[
                    inference.SequenceGenerationInferenceResult,
                    List[inference.SequenceGenerationInferenceResult],
                    inference.SequenceClassificationInferenceResult
                ]
            ],
            input_str: str
    ) -> inference.InferenceResult:
        assert (
                len(inference_results) > 0
        )

        input_length = len(input_str)
        if isinstance(inference_results[0], inference.SequenceClassificationInferenceResult):
            if len(inference_results) == 1:
                return inference_results[0]

            windows = sliding_windows(input_length, self.window_size)
            assert len(inference_results) == len(windows)

            merged_predictions = np.full(input_length, fill_value=-1, dtype=int)
            merged_logits = np.zeros((input_length, len(inference_results[0].logits[0])), dtype=float)
            for i, (ir, window) in enumerate(zip(inference_results, windows)):
                start_idx = 0 if i == 0 else self.context_size
                predictions = ir.predictions[1:-1][start_idx:start_idx + self.window_size]
                logits = ir.logits[1:-1][start_idx:start_idx + self.window_size]
                merged_predictions[window: window + self.window_size] = predictions
                merged_logits[window: window + self.window_size] = logits

            assert np.all(merged_predictions >= 0)  # make sure everything was successful
            # add bos eos predictions and logits again (all zeros because they are not used anyway)
            merged_predictions = list(np.pad(merged_predictions, (1, 1)))
            merged_logits = list(np.pad(merged_logits, ((1, 1), (0, 0))))
            return inference.SequenceClassificationInferenceResult(merged_predictions, merged_logits)

        else:
            # we have a list of lists when beam search is used, only take the top beam (first) in this case
            if isinstance(inference_results[0], list):
                inference_results = [irs[0] for irs in inference_results]
            if len(inference_results) == 1:
                return inference_results[0]

            windows = sliding_windows(input_length, self.window_size)
            assert len(inference_results) == len(windows)

            merged_token_ids = []
            merged_log_probabilities = []
            for i, (ir, window) in enumerate(zip(inference_results, windows)):
                input_str_left_context = input_str[max(0, window - self.context_size):window]
                input_str_window = input_str[window: window + self.window_size]
                input_str_right_context = \
                    input_str[window + self.window_size:
                              window + self.window_size + self.context_size]
                assert (
                               ir.token_ids[0] == self.char_tokenizer.token_to_id(constants.BOS)
                               and ir.token_ids[-1] == self.char_tokenizer.token_to_id(constants.EOS)
                )
                from_idx, to_idx = match_token_ids_ignoring_space_and_unk(
                    ir.token_ids[1:-1],
                    self.char_tokenizer,
                    input_str_left_context,
                    input_str_window,
                    input_str_right_context
                )
                merged_token_ids.extend(ir.token_ids[1:-1][from_idx: to_idx])
                merged_log_probabilities.extend(ir.token_log_probabilities[1:-1][from_idx: to_idx])

            merged_token_ids = (
                    [self.char_tokenizer.token_to_id(constants.BOS)]
                    + merged_token_ids
                    + [self.char_tokenizer.token_to_id(constants.EOS)]
            )
            merged_log_probabilities = [0.] + merged_log_probabilities + [0.]
            return inference.SequenceGenerationInferenceResult(merged_token_ids, merged_log_probabilities)

    def _inference_result_to_str(
            self,
            inference_result: Union[
                inference.SequenceGenerationInferenceResult,
                inference.SequenceClassificationInferenceResult
            ],
            input_str: str
    ) -> str:
        if isinstance(inference_result, inference.SequenceClassificationInferenceResult):
            return tokenization_repair.repair_whitespace(
                input_str,
                inference_result.predictions[1:-1]
            )
        else:
            output_chars = []
            input_str_no_spaces = input_str.replace(" ", "")
            input_str_no_spaces_ptr = 0
            for token_id in inference_result.token_ids[1:-1]:
                if token_id == self.ws_token_id:
                    output_chars.append(" ")
                else:
                    char = (
                        self.char_tokenizer.id_to_token(token_id) if token_id != self.unk_token_id
                        else input_str_no_spaces[input_str_no_spaces_ptr]
                    )
                    output_chars.append(char)
                    input_str_no_spaces_ptr += 1

            output_str = "".join(output_chars)
            assert input_str_no_spaces_ptr == len(input_str_no_spaces)
            output_str_no_spaces = output_str.replace(" ", "")
            assert output_str_no_spaces == input_str_no_spaces, \
                f"{input_str} --> {output_str}\n{input_str_no_spaces}\n{output_str_no_spaces}"
            return output_str

    @torch.inference_mode()
    def _repair_text_raw(
            self,
            inputs: List[str],
            batch_size: int = 16,
            sort_by_length: bool = True,
            show_progress: bool = False
    ) -> List[inference.InferenceResult]:
        # create batches, if an input sequence is too long, split it into multiple sequence using a sliding window
        # approach

        all_inference_results: Dict[int, List[inference.InferenceResult]] = {}
        batches = []
        for input_idx, ipt in enumerate(inputs):
            length = len(ipt)
            if length <= self.max_length:
                batches.append((input_idx, 0, length, 0))
                all_inference_results[input_idx] = [None]
            else:
                windows = sliding_windows(length, self.window_size)
                for i, window in enumerate(windows):
                    batches.append((
                        input_idx,
                        max(0, window - self.context_size),
                        min(length, window + self.window_size + self.context_size),
                        i
                    ))
                all_inference_results[input_idx] = [None] * len(windows)

        if sort_by_length:
            batches = sorted(batches, key=lambda e: e[2] - e[1], reverse=True)

        sum_lengths = sum(to_ - from_ for _, from_, to_, _ in batches)
        pbar = tqdm(
            list(range(0, len(batches), batch_size)),
            total=sum_lengths,
            ascii=True,
            leave=False,
            disable=not show_progress,
            unit="char"
        )

        for i, batch_idx in enumerate(pbar):
            batch = batches[batch_idx: batch_idx + batch_size]
            batch_sequences = [
                inputs[input_idx][from_:to_]
                for input_idx, from_, to_, _ in batch
            ]
            batch_length = sum(len(s) for s in batch_sequences)

            pbar.set_description(
                f"[Batch {i + 1}] Repairing tokenization of {len(batch):,} sequences "
                f"with {batch_length:,} characters in total"
            )

            kwargs = {}
            if self.cfg.model.type == "encoder_with_head":
                kwargs["no_spaces"] = [" " not in ipt for ipt in batch]
            else:
                kwargs["input_strings"] = [seq.replace(" ", "") for seq in batch_sequences]
            # add inference keyword arguments to the model
            kwargs.update(self.inference_kwargs)
            batch_inference_results = self.model.inference(
                batch_sequences,
                **kwargs
            )
            for (input_idx, _, _, position), inference_result in zip(
                    batch,
                    batch_inference_results
            ):
                all_inference_results[input_idx][position] = inference_result

            pbar.update(batch_length)

        pbar.close()
        return [self._merge_inference_results(all_inference_results[i], inputs[i]) for i in range(len(inputs))]

    def repair_text(
            self,
            inputs: StringInputOutput,
            batch_size: int = 16,
            sort_by_length: bool = True,
            show_progress: bool = False
    ) -> StringInputOutput:
        input_is_string = isinstance(inputs, str)
        assert (
                input_is_string
                or (isinstance(inputs, list) and len(inputs) > 0 and isinstance(inputs[0], str))
        ), f"input needs to be a string or a non empty list of strings"

        if input_is_string:
            inputs = [inputs]

        # clean inputs from leading, trailing or multiple whitespaces
        inputs = [nlp.clean_sequence(ipt) for ipt in inputs]

        inference_results = self._repair_text_raw(inputs, batch_size, sort_by_length, show_progress)

        outputs = [self._inference_result_to_str(ir, ipt) for ipt, ir in zip(inputs, inference_results)]
        return outputs[0] if input_is_string else outputs

    def repair_text_iter(
            self,
            input_iterator: Iterable[StringInputOutput],
            batch_size: int = 16,
            sort_by_length: bool = True
    ) -> Iterable[str]:
        for inputs in input_iterator:
            yield self.repair_text(inputs, batch_size, sort_by_length)

    def repair_file(
            self,
            input_file_path: str,
            output_file_path: Optional[str] = None,
            batch_size: int = 16,
            sort_by_length: bool = True,
            show_progress: bool = True
    ) -> Optional[List[str]]:
        inputs = []
        with open(input_file_path, "r", encoding="utf8") as in_file:
            for line in in_file:
                inputs.append(line.strip())

        outputs = self.repair_text(inputs, batch_size, sort_by_length, show_progress)

        if output_file_path is not None:
            with open(output_file_path, "w", encoding="utf8") as out_file:
                for output in outputs:
                    out_file.write(output)
                    out_file.write("\n")
            return None
        else:
            return outputs

    def to(self, device: Union[str, int]) -> "TokenizationRepairer":
        self.device = torch.device(device)
        self.model = self.model.to(self.device)
        return self
