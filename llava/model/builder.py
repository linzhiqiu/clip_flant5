#    Copyright 2023 Haotian Liu
#
#    Licensed under the Apache License, Version 2.0 (the "License");
#    you may not use this file except in compliance with the License.
#    You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS,
#    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#    See the License for the specific language governing permissions and
#    limitations under the License.


import os
import warnings
import shutil

from transformers import AutoTokenizer, AutoModelForCausalLM, AutoConfig, BitsAndBytesConfig
from transformers import T5TokenizerFast
import torch
from llava.model import *
from llava.constants import DEFAULT_IMAGE_PATCH_TOKEN, DEFAULT_IM_START_TOKEN, DEFAULT_IM_END_TOKEN

CLIP_T5_BASE_MODELS = {
    'clip-flant5-xxl': {'base': 'google/flan-t5-xxl', 'model': 'zhiqiulin/clip-flant5-xxl', 'load_projector_only': False},
    'clip-flant5-xxl-stage-1': {'base': 'google/flan-t5-xxl', 'model': 'zhiqiulin/clip-flant5-xxl-stage-1', 'load_projector_only': True},
    'clip-flant5-xxl-no-split-text': {'base': 'google/flan-t5-xxl', 'model': 'zhiqiulin/clip-flant5-xxl-no-split-text', 'load_projector_only': False},
    'clip-flant5-xxl-stage-1-no-split-text': {'base': 'google/flan-t5-xxl', 'model': 'zhiqiulin/clip-flant5-xxl-stage-1-no-split-text', 'load_projector_only': True},
    'clip-flant5-xl': {'base': 'google/flan-t5-xl', 'model': 'zhiqiulin/clip-flant5-xl', 'load_projector_only': False},
    'clip-flant5-xl-stage-1': {'base': 'google/flan-t5-xl', 'model': 'zhiqiulin/clip-flant5-xl-stage-1', 'load_projector_only': True},
    'clip-t5-xxl': {'base': 't5-11b', 'model': 'zhiqiulin/clip-t5-xxl', 'load_projector_only': False},
    'clip-t5-xxl-stage-1': {'base': 't5-11b', 'model': 'zhiqiulin/clip-t5-xxl-stage-1', 'load_projector_only': True},
}

def load_pretrained_model(model_path, model_base, model_name, load_8bit=False, load_4bit=False, device_map="auto", device="cuda"):
    kwargs = {"device_map": device_map}
    use_t5 = 'clip-t5' in model_name.lower() or 'clip-flant5' in model_name.lower()
    dtype = torch.bfloat16 if use_t5 else torch.float16
    
    if load_8bit:
        kwargs['load_in_8bit'] = True
    elif load_4bit:
        kwargs['load_in_4bit'] = True
        kwargs['quantization_config'] = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_compute_dtype=dtype,
            bnb_4bit_use_double_quant=True,
            bnb_4bit_quant_type='nf4'
        )
    else:
        kwargs['torch_dtype'] = dtype

    if 'llava' in model_name.lower():
        # Load LLaVA model
        if 'lora' in model_name.lower() and model_base is None:
            warnings.warn('There is `lora` in model name but no `model_base` is provided. If you are loading a LoRA model, please provide the `model_base` argument. Detailed instruction: https://github.com/haotian-liu/LLaVA#launch-a-model-worker-lora-weights-unmerged.')
        if 'lora' in model_name.lower() and model_base is not None:
            lora_cfg_pretrained = AutoConfig.from_pretrained(model_path)
            tokenizer = AutoTokenizer.from_pretrained(model_base, use_fast=False)
            print('Loading LLaVA from base model...')
            model = LlavaLlamaForCausalLM.from_pretrained(model_base, low_cpu_mem_usage=True, config=lora_cfg_pretrained, **kwargs)
            token_num, tokem_dim = model.lm_head.out_features, model.lm_head.in_features
            if model.lm_head.weight.shape[0] != token_num:
                model.lm_head.weight = torch.nn.Parameter(torch.empty(token_num, tokem_dim, device=model.device, dtype=model.dtype))
                model.model.embed_tokens.weight = torch.nn.Parameter(torch.empty(token_num, tokem_dim, device=model.device, dtype=model.dtype))

            print('Loading additional LLaVA weights...')
            if os.path.exists(os.path.join(model_path, 'non_lora_trainables.bin')):
                non_lora_trainables = torch.load(os.path.join(model_path, 'non_lora_trainables.bin'), map_location='cpu')
            else:
                # this is probably from HF Hub
                from huggingface_hub import hf_hub_download
                def load_from_hf(repo_id, filename, subfolder=None):
                    cache_file = hf_hub_download(
                        repo_id=repo_id,
                        filename=filename,
                        subfolder=subfolder)
                    return torch.load(cache_file, map_location='cpu')
                non_lora_trainables = load_from_hf(model_path, 'non_lora_trainables.bin')
            non_lora_trainables = {(k[11:] if k.startswith('base_model.') else k): v for k, v in non_lora_trainables.items()}
            if any(k.startswith('model.model.') for k in non_lora_trainables):
                non_lora_trainables = {(k[6:] if k.startswith('model.') else k): v for k, v in non_lora_trainables.items()}
            model.load_state_dict(non_lora_trainables, strict=False)

            from peft import PeftModel
            print('Loading LoRA weights...')
            model = PeftModel.from_pretrained(model, model_path)
            print('Merging LoRA weights...')
            model = model.merge_and_unload()
            print('Model is loaded...')
        elif model_base is not None:
            # this may be mm projector only
            print('Loading LLaVA from base model...')
            if 'mpt' in model_name.lower():
                if not os.path.isfile(os.path.join(model_path, 'configuration_mpt.py')):
                    shutil.copyfile(os.path.join(model_base, 'configuration_mpt.py'), os.path.join(model_path, 'configuration_mpt.py'))
                tokenizer = AutoTokenizer.from_pretrained(model_base, use_fast=True)
                cfg_pretrained = AutoConfig.from_pretrained(model_path, trust_remote_code=True)
                model = LlavaMPTForCausalLM.from_pretrained(model_base, low_cpu_mem_usage=True, config=cfg_pretrained, **kwargs)
            else:
                tokenizer = AutoTokenizer.from_pretrained(model_base, use_fast=False)
                cfg_pretrained = AutoConfig.from_pretrained(model_path)
                model = LlavaLlamaForCausalLM.from_pretrained(model_base, low_cpu_mem_usage=True, config=cfg_pretrained, **kwargs)

            mm_projector_weights = torch.load(os.path.join(model_path, 'mm_projector.bin'), map_location='cpu')
            mm_projector_weights = {k: v.to(dtype) for k, v in mm_projector_weights.items()}
            model.load_state_dict(mm_projector_weights, strict=False)
        else:
            if 'mpt' in model_name.lower():
                tokenizer = AutoTokenizer.from_pretrained(model_path, use_fast=True)
                model = LlavaMPTForCausalLM.from_pretrained(model_path, low_cpu_mem_usage=True, **kwargs)
            else:
                tokenizer = AutoTokenizer.from_pretrained(model_path, use_fast=False)
                model = LlavaLlamaForCausalLM.from_pretrained(model_path, low_cpu_mem_usage=True, **kwargs)
    elif use_t5:
        # Load CLIP-FlanT5 or CLIP-T5 model
        if 'lora' in model_name.lower() and model_base is None:
            warnings.warn('There is `lora` in model name but no `model_base` is provided. If you are loading a LoRA model, please provide the `model_base` argument. Detailed instruction: https://github.com/haotian-liu/LLaVA#launch-a-model-worker-lora-weights-unmerged.')
        if 'lora' in model_name.lower() and model_base is not None:
            raise NotImplementedError()
        elif model_base is not None:
            raise NotImplementedError()
        else:
            load_projector_only = CLIP_T5_BASE_MODELS[model_name]['load_projector_only']
            if load_projector_only:
                load_model_path = CLIP_T5_BASE_MODELS[model_name]['base'] # use the oirginal language model
            else:
                load_model_path = CLIP_T5_BASE_MODELS[model_name]['model'] # use the pretrained multimodal language model
            # this is mm projector only (stage-1 training)
            print(f'Loading CLIP-FlanT5 from base model {model_name}: path is {load_model_path}...')
            cfg_pretrained = AutoConfig.from_pretrained(model_path)
            tokenizer = T5TokenizerFast.from_pretrained(
                CLIP_T5_BASE_MODELS[model_name]['base'],
                truncation_side='right',
                padding_side="right",
            )
            model = CLIPT5ForConditionalGeneration.from_pretrained(
                load_model_path,
                # config=cfg_pretrained,
                delay_load=False,
                torch_dtype=torch.bfloat16
            )
            model.to(dtype=torch.bfloat16, device=device)

            if load_projector_only:
                # Not tested
                if os.environ.get('HF_HOME') is not None:
                    local_dir = os.path.join(os.environ.get('HF_HOME'), model_name)
                else:
                    local_dir = os.path.join(os.path.expanduser("~"), model_name)
                print(f"Downloading projector weights to {local_dir}")
                hf_hub_download(
                    repo_id=CLIP_T5_BASE_MODELS[model_name]['model'],
                    filename='mm_projector.bin',
                    local_dir=local_dir,
                )
                mm_projector_weights = torch.load(os.path.join(local_dir, 'mm_projector.bin'), map_location='cpu')
                mm_projector_weights = {k: v.to(torch.bfloat16) for k, v in mm_projector_weights.items()}
                model.load_state_dict(mm_projector_weights, strict=False)
    else:
        raise NotImplementedError()

    image_processor = None

    # if 'llava' in model_name.lower():
        # mm_use_im_start_end = getattr(model.config, "mm_use_im_start_end", False)
        # mm_use_im_patch_token = getattr(model.config, "mm_use_im_patch_token", True)
        # if mm_use_im_patch_token:
        #     tokenizer.add_tokens([DEFAULT_IMAGE_PATCH_TOKEN], special_tokens=True)
        # if mm_use_im_start_end:
        #     tokenizer.add_tokens([DEFAULT_IM_START_TOKEN, DEFAULT_IM_END_TOKEN], special_tokens=True)
    model.resize_token_embeddings(len(tokenizer))
    vision_tower = model.get_vision_tower()
    if not vision_tower.is_loaded:
        vision_tower.load_model()
    vision_tower.to(device=device, dtype=dtype)
    mm_projector = model.get_model().mm_projector
    mm_projector.to(dtype=dtype, device=device)
    image_processor = vision_tower.image_processor

    if hasattr(model.config, "max_sequence_length"):
        context_len = model.config.max_sequence_length
    else:
        context_len = 2048

    return tokenizer, model, image_processor, context_len, use_t5
