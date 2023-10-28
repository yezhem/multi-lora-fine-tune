# ASPEN: Efficient Multi-LoRA Fine Tuning with Shared-Based Model
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Copyright (C) 2023 All Rights Reserved.
#
# Github:  https://github.com/TUDB-Labs/multi-lora-fine-tune

import json
import torch
import aspen
import random
import datetime
import argparse
from typing import Dict, Tuple, List

# Command Line Arguments
parser = argparse.ArgumentParser(description='ASPEN main program')
parser.add_argument('--base_model', type=str,
                    help='Path to or name of base model')
parser.add_argument('--model_type', type=str, default="llama",
                    help='The model type, support: llama, chatglm')
parser.add_argument('--inference', action="store_true",
                    help='The inference mode (just for test)')
parser.add_argument('--load_lora', action="store_true",
                    help="Load lora from file instead of init randomly")
parser.add_argument('--disable_lora', action="store_true",
                    help="Disable the lora modules")
parser.add_argument('--tokenizer', type=str,
                    help='Path to or name of tokenizer')
parser.add_argument('--load_8bit', action="store_true",
                    help='Load model in 8bit mode')
parser.add_argument('--load_4bit', action="store_true",
                    help='Load model in 4bit mode')
parser.add_argument('--device', type=str, default='cuda:0',
                    help='Specify which GPU to be used, default is cuda:0')
parser.add_argument('--config', type=str,
                    help='Path to finetune configuration')
parser.add_argument('--seed', type=int, default=42,
                    help='Random seed in integer, default is 42')
parser.add_argument('--log', type=bool, default=True,
                    help='Turn on or off log, default is true')

args = parser.parse_args()


def log(msg: str):
    if args.log:
        print('[%s] ASPEN: %s' %
              (datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'), msg))


if torch.cuda.is_available():
    log('NVIDIA CUDA initialized successfully.')
    log('Total %i GPU(s) detected.' % torch.cuda.device_count())
else:
    print('ASPEN requires NVIDIA CUDA computing capacity. Please check your PyTorch installation.')
    exit(-1)


if args.base_model is None:
    print('error: Argument --base_model are required.')
    parser.print_help()
    exit(-1)


if args.config is None:
    print('error: Argument --config are required.')
    parser.print_help()
    exit(-1)


# Functions
def setup_seed(seed):
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    random.seed(seed)


def load_base_model(config: Dict[str, any]) -> Tuple[aspen.Tokenizer, aspen.LLMModel]:
    if args.model_type == "llama":
        model = aspen.LlamaModel.from_pretrained(
            path=args.base_model,
            device=args.device,
            bits=(8 if args.load_8bit else (4 if args.load_4bit else None)),
            log_fn=log
        )
    elif args.model_type == "chatglm":
        model = aspen.ChatGLMModel.from_pretrained(
            path=args.base_model,
            device=args.device,
            bits=(8 if args.load_8bit else (4 if args.load_4bit else None)),
            log_fn=log
        )
    else:
        raise f"unkown model type {args.model_type}"

    if args.tokenizer:
        tokenizer = aspen.Tokenizer(args.tokenizer, from_file=True)
    else:
        tokenizer = aspen.Tokenizer(args.base_model)

    if config["pad_token_id"] == -1:
        if config["expand_right"]:
            model.pad_token_id_ = tokenizer.eos_id_
        else:
            model.pad_token_id_ = tokenizer.pad_id_
    else:
        model.pad_token_id_ = config["pad_token_id"]

    model.eos_token_id_ = tokenizer.eos_id_

    tokenizer.pad_id_ = model.pad_token_id_

    return tokenizer, model


def init_lora_model(config: Dict[str, any], llama_model: aspen.LLMModel):
    if args.disable_lora:
        return

    for lora_config in config["lora"]:
        lora_weight = None
        if args.load_lora:
            adapter_file_path = lora_config["output"] + "/adapter_model.bin"
            print(f"load {adapter_file_path}")
            lora_weight = torch.load(adapter_file_path)

        llama_model.init_lora_weight(lora_config["name"],
                                     lora_config["r"],
                                     lora_config["alpha"],
                                     lora_config["dropout"],
                                     lora_config["target_modules"],
                                     lora_weight)


def get_optimizer(config: Dict[str, any], train_paramas: Dict[str, torch.Tensor]) -> Dict[str, torch.optim.Optimizer]:
    # get optimizer per lora model
    optimizer: Dict[str, torch.optim.Optimizer] = {}

    for lora_config in config["lora"]:
        adapter_name = lora_config["name"]
        optim_name = lora_config["optim"]
        lr = lora_config["lr"]
        if optim_name == "sgd":
            momentum = 0
            if "momentum" in lora_config:
                momentum = lora_config["momentum"]
            optimizer[adapter_name] = (torch.optim.SGD(
                train_paramas[adapter_name], lr=lr, momentum=momentum))
        elif optim_name == "adamw":
            optimizer[adapter_name] = (torch.optim.AdamW(
                train_paramas[adapter_name], lr=lr))
        else:
            raise f"unkown optimizer {optim_name}"

    return optimizer


def get_accumulation_steps(config: Dict[str, any]) -> Dict[str, int]:
    ret_accumulation_step = {}
    for lora_config in config["lora"]:
        batch_size = lora_config["batch_size"]
        micro_batch_size = lora_config["micro_batch_size"]
        if batch_size < micro_batch_size or batch_size % micro_batch_size != 0:
            raise f"error batch_size {batch_size} and micro batch size {micro_batch_size}"
        ret_accumulation_step[lora_config["name"]
                              ] = batch_size / micro_batch_size
    return ret_accumulation_step


# to get test result and want early stop it
def train(config: Dict[str, any], llm_model: aspen.LLMModel, dispatcher: aspen.Dispatcher):
    # the train paramas per lora model
    all_train_paramas: Dict[str, List[torch.Tensor]
                            ] = llm_model.get_train_paramas(config)
    all_optimizer: Dict[str, torch.optim.Optimizer] = get_optimizer(
        config, all_train_paramas)
    accumulation_step: Dict[str, int] = get_accumulation_steps(config)

    loss_fn = torch.nn.CrossEntropyLoss()

    step_cnt = 0
    while not dispatcher.check_task_done():
        input: aspen.MultiLoraBatchData = dispatcher.get_train_data()
        for lora in input.lora_batch_data_config_:
            all_optimizer[lora.adapter_name_].zero_grad()

        step_cnt += 1

        output = llm_model.forward(input)
        labels = torch.tensor(input.batch_tokens_,
                              dtype=torch.long).to(args.device)

        total_loss = None
        for lora_config in input.lora_batch_data_config_:
            start_idx = lora_config.batch_start_idx_
            end_idx = lora_config.batch_end_idx_
            loss_input = output[start_idx:end_idx][..., :-1,
                                                   :].contiguous().view(-1, llm_model.vocab_size_)
            loss_target = labels[start_idx:end_idx][...,
                                                    1:].contiguous().view(-1)
            loss = loss_fn(loss_input, loss_target) / \
                accumulation_step[lora_config.adapter_name_]
            print(
                f"    adapter: {lora_config.adapter_name_} loss: {loss}")
            if total_loss is None:
                total_loss = loss
            else:
                total_loss += loss

        total_loss.backward()
        for lora in input.lora_batch_data_config_:
            if step_cnt % accumulation_step[lora.adapter_name_] == 0:
                all_optimizer[lora.adapter_name_].step()

        if step_cnt % config["save_step"] == 0:
            aspen.save_lora_model(llm_model, config, f"{step_cnt}")

    aspen.save_lora_model(llm_model, config)


def inference(config: Dict[str, any],
              llm_model: aspen.LLMModel,
              tokenizer: aspen.Tokenizer):
    lora_adapter_num = len(config["lora"])
    batch_data_config: List[aspen.LoraBatchDataConfig] = []

    for idx, lora_config in enumerate(config["lora"]):
        adapter_name = lora_config["name"]
        batch_data_config.append(aspen.LoraBatchDataConfig(
            adapter_name, idx, idx + 1))

    max_len = 128

    while True:
        input_raw = input("INPUT WITHOUT PROMPT: ")
        if input_raw == "QUIT":
            return

        tokens = tokenizer.encode(input_raw, True, False)
        token_len = len(tokens)
        while len(tokens) < max_len:
            tokens.append(tokenizer.pad_id_)

        input_data = aspen.MultiLoraBatchData(
            prompts_=[input_raw] * lora_adapter_num,
            lora_batch_data_config_=batch_data_config,
            batch_tokens_=[tokens] * lora_adapter_num,
            tokens_len_without_pad_=[token_len] * lora_adapter_num,
            batch_seq_len_=max_len,
            inference_model_=True)

        eos_flag: List[bool] = [False] * lora_adapter_num
        for pos in range(token_len, max_len):
            with torch.no_grad():
                # batch_size, seq_len, voc_logs
                outputs = llm_model.forward(input_data)
                next_token = outputs[:, pos - 1, :]
                next_token = torch.argmax(next_token, dim=-1)
                for idx in range(len(input_data.batch_tokens_)):
                    input_data.batch_tokens_[idx][pos] = next_token[idx].item()
                    # end of the sentence
                    if next_token[idx].item() == tokenizer.eos_id_:
                        eos_flag[idx] = True
                    input_data.tokens_len_without_pad_[
                        idx] = input_data.tokens_len_without_pad_[idx] + 1
            hava_not_done_sentenct = False
            for flag in eos_flag:
                if not flag:
                    hava_not_done_sentenct = True
                    break
            if not hava_not_done_sentenct:
                break

        for idx, output in enumerate(input_data.batch_tokens_):
            print(f"#LORA{idx} OUTPUT IS:")
            print(tokenizer.decode(output))


# Main Function
if __name__ == "__main__":
    setup_seed(args.seed)

    with open(args.config, 'r', encoding='utf8') as fp:
        config = json.load(fp)

    tokenizer, model = load_base_model(config)
    init_lora_model(config, model)

    torch.cuda.empty_cache()

    if args.inference:
        inference(config, model, tokenizer)
    else:
        dispatcher = aspen.Dispatcher(config, tokenizer)
        train(config, model, dispatcher)
