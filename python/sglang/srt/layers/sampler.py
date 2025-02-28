import logging
from typing import List

import torch
from torch import nn

from sglang.srt.layers.logits_processor import LogitsProcessorOutput
from sglang.srt.managers.schedule_batch import global_server_args_dict
from sglang.srt.sampling.sampling_batch_info import SamplingBatchInfo
from sglang.srt.utils import crash_on_warnings, is_flashinfer_available

if is_flashinfer_available():
    from flashinfer.sampling import (
        min_p_sampling_from_probs,
        top_k_renorm_prob,
        top_k_top_p_sampling_from_probs,
        top_p_renorm_prob,
    )


logger = logging.getLogger(__name__)


class Sampler(nn.Module):
    def __init__(self):
        super().__init__()
        self.use_nan_detectioin = global_server_args_dict["enable_nan_detection"]

    def forward(
        self,
        logits_output: LogitsProcessorOutput,
        sampling_info: SamplingBatchInfo,
        return_logprob: bool,
        top_logprobs_nums: List[int],
    ):
        logits = logits_output.next_token_logits

        if self.use_nan_detectioin and torch.any(torch.isnan(logits)):
            logger.warning("Detected errors during sampling! NaN in the logits.")
            logits = torch.where(
                torch.isnan(logits), torch.full_like(logits, -1e5), logits
            )
            if crash_on_warnings():
                raise ValueError("Detected errors during sampling! NaN in the logits.")

        if sampling_info.is_all_greedy:
            # Use torch.argmax if all requests use greedy sampling
            batch_next_token_ids = torch.argmax(logits, -1)
            if return_logprob:
                probs = torch.nn.functional.log_softmax(logits, dim=-1)
                logprobs = probs.clamp(min=torch.finfo(probs.dtype).min)
        else:
            # Post process logits
            logits.div_(sampling_info.temperatures)
            probs = torch.softmax(logits, dim=-1)
            del logits

            if global_server_args_dict["sampling_backend"] == "flashinfer":
                if return_logprob:
                    # NOTE: the top_p_renorm_prob from flashinfer has numerical problems,
                    # https://github.com/flashinfer-ai/flashinfer/issues/708
                    # so we use the torch implementation.

                    # clamp to avoid -inf
                    logprobs = torch.log(
                        top_p_normalize_probs_torch(probs, sampling_info.top_ps)
                    ).clamp(min=torch.finfo(probs.dtype).min)

                max_top_k_round, batch_size = 32, probs.shape[0]
                uniform_samples = torch.rand(
                    (max_top_k_round, batch_size), device=probs.device
                )
                if sampling_info.need_min_p_sampling:
                    probs = top_k_renorm_prob(probs, sampling_info.top_ks)
                    probs = top_p_renorm_prob(probs, sampling_info.top_ps)
                    batch_next_token_ids, success = min_p_sampling_from_probs(
                        probs, uniform_samples, sampling_info.min_ps
                    )
                else:
                    batch_next_token_ids, success = top_k_top_p_sampling_from_probs(
                        probs,
                        uniform_samples,
                        sampling_info.top_ks,
                        sampling_info.top_ps,
                        filter_apply_order="joint",
                    )

                if self.use_nan_detectioin and not torch.all(success):
                    logger.warning("Detected errors during sampling!")
                    batch_next_token_ids = torch.zeros_like(batch_next_token_ids)

            elif global_server_args_dict["sampling_backend"] == "pytorch":
                # A slower fallback implementation with torch native operations.
                batch_next_token_ids = top_k_top_p_min_p_sampling_from_probs_torch(
                    probs,
                    sampling_info.top_ks,
                    sampling_info.top_ps,
                    sampling_info.min_ps,
                    sampling_info.need_min_p_sampling,
                )
                if return_logprob:
                    # clamp to avoid -inf
                    logprobs = torch.log(
                        top_p_normalize_probs_torch(probs, sampling_info.top_ps)
                    ).clamp(min=torch.finfo(probs.dtype).min)
            else:
                raise ValueError(
                    f"Invalid sampling backend: {global_server_args_dict['sampling_backend']}"
                )

        batch_next_token_ids = batch_next_token_ids.to(torch.int32)

        # Attach logprobs to logits_output (in-place modification)
        if return_logprob:
            if any(x > 0 for x in top_logprobs_nums):
                (
                    logits_output.next_token_top_logprobs_val,
                    logits_output.next_token_top_logprobs_idx,
                ) = get_top_logprobs(logprobs, top_logprobs_nums)

            logits_output.next_token_logprobs = logprobs[
                torch.arange(len(batch_next_token_ids), device=sampling_info.device),
                batch_next_token_ids,
            ]

        return batch_next_token_ids


def top_k_top_p_min_p_sampling_from_probs_torch(
    probs: torch.Tensor,
    top_ks: torch.Tensor,
    top_ps: torch.Tensor,
    min_ps: torch.Tensor,
    need_min_p_sampling: bool,
):
    """A top-k, top-p and min-p sampling implementation with native pytorch operations."""
    probs_sort, probs_idx = probs.sort(dim=-1, descending=True)
    probs_sum = torch.cumsum(probs_sort, dim=-1)
    probs_sort[
        torch.arange(0, probs.shape[-1], device=probs.device).view(1, -1)
        >= top_ks.view(-1, 1)
    ] = 0.0
    probs_sort[(probs_sum - probs_sort) > top_ps.view(-1, 1)] = 0.0

    if need_min_p_sampling:
        min_p_thresholds = probs_sort[:, 0] * min_ps
        probs_sort[probs_sort < min_p_thresholds.view(-1, 1)] = 0.0

    sampled_index = torch.multinomial(probs_sort, num_samples=1)
    # int32 range is enough to represent the token ids
    probs_idx = probs_idx.to(torch.int32)
    batch_next_token_ids = torch.gather(probs_idx, dim=1, index=sampled_index).view(-1)
    return batch_next_token_ids


def top_p_normalize_probs_torch(
    probs: torch.Tensor,
    top_ps: torch.Tensor,
):
    # See also top_k_top_p_min_p_sampling_from_probs_torch
    probs_sort, probs_idx = probs.sort(dim=-1, descending=True)
    probs_sum = torch.cumsum(probs_sort, dim=-1)
    probs_sort[(probs_sum - probs_sort) > top_ps.view(-1, 1)] = 0.0
    probs_sort.div_(probs_sort.sum(dim=-1, keepdim=True))
    return torch.zeros_like(probs_sort).scatter_(-1, probs_idx, probs_sort)


def get_top_logprobs(logprobs: torch.Tensor, top_logprobs_nums: List[int]):
    max_k = max(top_logprobs_nums)
    ret = logprobs.topk(max_k, dim=1)
    values = ret.values.tolist()
    indices = ret.indices.tolist()

    output_top_logprobs_val = []
    output_top_logprobs_idx = []
    for i, k in enumerate(top_logprobs_nums):
        output_top_logprobs_val.append(values[i][:k])
        output_top_logprobs_idx.append(indices[i][:k])
    return output_top_logprobs_val, output_top_logprobs_idx
