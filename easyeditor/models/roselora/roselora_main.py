from copy import deepcopy
from typing import Any, Dict, List, Tuple
from tqdm import tqdm 
import numpy as np

from peft import get_peft_model, AdaLoraConfig, TaskType, get_peft_model_state_dict, set_peft_model_state_dict, LoraConfig
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
from torch.nn import CrossEntropyLoss
from .roselora_hparams import RoseLoRAHyperParams


def apply_roselora_to_model(
        model: AutoModelForCausalLM,
        tok: AutoTokenizer,
        requests: List[Dict],
        hparams: RoseLoRAHyperParams,
        copy=False,
        keep_original_weight=False,
        **kwargs: Any,
) -> Tuple[AutoModelForCausalLM, Dict[str, Any]]:
    """
    Returns a model with the desired changes.
    :param copy: If true, will preserve the original model while creating a new one to edit.
        Note that you are responsible for deallocating the new model's memory to avoid leaks.
    :return: (1) the updated model, (2) the weights that changed
    """
    weights_copy = {}
    if copy:
        weights_copy = deepcopy(model)

    edited_model = execute_roselora(model, tok, requests, hparams, keep_original_weight)
    return edited_model, weights_copy


def execute_roselora(
        model: AutoModelForCausalLM,
        tok: AutoTokenizer,
        requests: List[Dict],
        hparams: RoseLoRAHyperParams,
        keep_original_weight=False,
        **kwargs: Any,
) -> Dict[str, Tuple[torch.Tensor]]:
    """
    Executes the RoseLora update algorithm for the specified update at the specified layer
    Invariant: model at beginning of function == model at end of function
    """

    sparsity = 0.05
    full_iter = 3
    burnin_iter = 20

    model.config.use_cache = False
    model.supports_gradient_checkpointing = True  #
    model.gradient_checkpointing_enable()
    model.enable_input_require_grads()
    if hparams.lora_type == "lora":
        Config = LoraConfig
    else:
        raise NotImplementedError
    
    if not keep_original_weight and hasattr(model,'peft_config'):
        peft_model = model
    else:
        peft_config = Config(
            task_type=TaskType.CAUSAL_LM,
            inference_mode=False,
            r=hparams.rank,
            lora_alpha=hparams.lora_alpha, 
            lora_dropout=hparams.lora_dropout,
            layers_to_transform=hparams.layers if len(hparams.layers) > 0 else None,
            target_modules=hparams.target_modules
        )
        peft_model = get_peft_model(model, peft_config)

    peft_model.is_parallelizable = True
    peft_model.model_parallel = True

    requests = deepcopy(requests)
    for request in requests:
        if request["target_new"] != " ":
            # Space required for correct tokenization
            request["target_new"] = " " + request["target_new"]

    device = torch.device(f'cuda:{hparams.device}')

    # Define inputs
    texts = [r["prompt"] for r in requests]
    targets = [r["target_new"] for r in requests]

    progress_bar = tqdm(
        range(hparams.num_steps),
        total=hparams.num_steps,
        desc=f'RoseLoRA Training: ',
        leave=True
    )

    # Configure optimizer / gradients    
    opt = torch.optim.Adam(
        peft_model.parameters(),
        lr=hparams.lr,
        weight_decay=hparams.weight_decay,
    )

    loss_meter = AverageMeter()
    imp_A = {}
    imp_B = {}

    for it in progress_bar:
        loss_meter.reset()

        for txt, tgt in zip(
                chunks(texts, hparams.batch_size), 
                chunks(targets, hparams.batch_size),
        ):

            # Prepare RoseLoRA Rate
            if it < full_iter:
                rate = 1.0

            elif full_iter <= it < burnin_iter:
                rate = sparsity + (1 - sparsity) * (1 - (it - full_iter) / (burnin_iter - full_iter)) ** 3
            
            else:
                rate = sparsity

            inputs = tok(txt, return_tensors="pt", padding=True).to(device)

            inputs_targets = [txt_ + tgt_ for txt_, tgt_ in zip(txt, tgt)]
            inputs_targets = tok(inputs_targets, return_tensors="pt", padding=True).to(device)

            num_prompt_toks = [int((i != tok.pad_token_id).sum()) for i in inputs['input_ids'].cpu()]
            num_pad_toks = [int((i == tok.pad_token_id).sum()) for i in inputs_targets['input_ids'].cpu()]
            prompt_len = [x + y for x, y in zip(num_pad_toks, num_prompt_toks)]
            prompt_target_len = inputs_targets['input_ids'].size(1)
            label_mask = torch.tensor([[False] * length + [True] * (prompt_target_len - length) for length in prompt_len]).to(device)
            bs = inputs["input_ids"].shape[0]

            # Compute Loss
            opt.zero_grad()

            logits = model(**inputs_targets).logits
            shift_logits = logits[..., :-1, :].contiguous()
            shift_labels = inputs_targets['input_ids'][..., 1:].contiguous()

            loss_fct = CrossEntropyLoss(reduction='none')
            loss = loss_fct(shift_logits.view(-1, shift_logits.size(-1)), shift_labels.view(-1))
            loss = loss.view(bs, -1)
            loss = (loss * label_mask[:,1:]).sum(1) / label_mask[:,1:].sum(1)
            loss = loss.mean()

            loss_meter.update(loss.item(), n=bs)

            loss.backward()
                        
            for n, p in peft_model.named_parameters():
                if "lora_A" in n:
                    if n not in imp_A:
                        imp_A[n] = torch.abs(p.grad*p)
                    else:
                        imp_A[n] = imp_A[n] * 0.8+torch.abs(p.grad*p).detach() * 0.2
                if "lora_B" in n:
                    if n not in imp_B:
                        imp_B[n] = torch.abs(p.grad*p)
                    else:
                        imp_B[n] = imp_B[n] * 0.8+torch.abs(p.grad*p).detach() * 0.2

            opt.step()

            if rate < 1.0:
                for n, p in peft_model.named_parameters():
                    if "lora_B" in n:
                        mask_threshold = torch.kthvalue(imp_B[n], int(imp_B[n].shape[0] * (1 - rate)), 0, True)[0]
                        p.data.masked_fill_(imp_B[n] < mask_threshold, 0.0)
                        p.data.clamp_(-3e-3, 3e-3)

                    if "lora_A" in n:
                        mask_threshold = torch.kthvalue(imp_A[n], int(imp_A[n].shape[1] * (1 - rate)), 1, True)[0]
                        p.data.masked_fill_(imp_A[n] < mask_threshold, 0.0) 
                        p.data.clamp_(-3e-3, 3e-3)

            
        progress_bar.set_description(
            f"rate: {rate:.3f} "
            f"loss: {loss_meter.avg:.3f} "
        )
        progress_bar.update()

        if it > burnin_iter and loss_meter.avg < 0.1:
            break

    progress_bar.close()

    return peft_model


class AverageMeter:#
    """Computes and stores the average and current value"""

    def __init__(self):
        self.reset()

    def reset(self):
        self.val = 0
        self.avg = 0
        self.sum = 0
        self.count = 0

    def update(self, val, n=1):
        self.val = val
        self.sum += val * n
        self.count += n
        self.avg = self.sum / self.count


def chunks(arr, n):
    """Yield successive n-sized chunks from arr."""
    chunk = []
    for a in arr:
        chunk.append(a)
        if len(chunk) == n:
            yield chunk
            chunk = []
    if len(chunk) > 0:
        yield chunk
