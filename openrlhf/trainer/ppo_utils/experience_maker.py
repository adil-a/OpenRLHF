import logging
import time
from abc import ABC
from copy import deepcopy
from dataclasses import dataclass
from typing import List, Optional, Tuple, Union
from torch.profiler import profile, record_function, ProfilerActivity

import ray
import torch
import torch.nn as nn
from tqdm import tqdm

from openrlhf.models.actor import Actor
from openrlhf.models.utils import compute_reward, masked_mean
from openrlhf.utils.logging_utils import init_logger
from openrlhf.utils.remote_rm_utils import remote_rm_fn, remote_rm_fn_ray
from openrlhf.utils.utils import tile_prompts

logger = init_logger(__name__)


@dataclass
class Experience:
    """Experience is a batch of data.
    These data should have the the sequence length and number of actions.
    Left padding for sequences is applied.

    Shapes of each tensor:
    sequences: (B, S)
    action_log_probs: (B, A)
    values: (B, A)
    returns: (B, A)
    advatanges: (B, A)
    attention_mask: (B, S)
    action_mask: (B, A)

    "A" is the number of actions.
    """

    sequences: torch.Tensor
    action_log_probs: torch.Tensor
    values: torch.Tensor
    returns: torch.Tensor
    advantages: torch.Tensor
    attention_mask: Optional[torch.LongTensor]
    action_mask: Optional[torch.BoolTensor]
    info: Optional[dict]

    @torch.no_grad()
    def to_device(self, device: torch.device) -> None:
        self.sequences = self.sequences.to(device)
        self.action_log_probs = self.action_log_probs.to(device)
        self.values = self.values.to(device)
        self.returns = self.returns.to(device)
        if self.advantages is not None:
            self.advantages = self.advantages.to(device)
        if self.attention_mask is not None:
            self.attention_mask = self.attention_mask.to(device)
        if self.action_mask is not None:
            self.action_mask = self.action_mask.to(device)

    def pin_memory(self):
        self.sequences = self.sequences.pin_memory()
        self.action_log_probs = self.action_log_probs.pin_memory()
        self.values = self.values.pin_memory()
        self.returns = self.returns.pin_memory()
        self.advantages = self.advantages.pin_memory()
        if self.attention_mask is not None:
            self.attention_mask = self.attention_mask.pin_memory()
        if self.action_mask is not None:
            self.action_mask = self.action_mask.pin_memory()
        return self


class NaiveExperienceMaker(ABC):
    """
    Naive experience maker.
    """

    def __init__(
        self,
        actor: Actor,
        critic: nn.Module,
        reward_model: nn.Module,
        initial_model: Actor,
        tokenizer,
        prompt_max_len: int,
        kl_controller,
        strategy=None,
        remote_rm_url: str = None,
        reward_fn=None,
        shared_actorcritic=False,
        threshold=-5.,
        reward_cap=4.5,
        target_dist_beta=1.,
        rm_type=None,
        actor_loss_type=None,
        max_new_tokens=None,
        save_negdata=False,
        save_negdata_threshold=-10000,
    ) -> None:
        super().__init__()
        self.actor = actor
        self.critic = critic
        self.reward_model = reward_model
        self.remote_rm_url = remote_rm_url
        self.initial_model = initial_model
        self.tokenizer = tokenizer
        self.prompt_max_len = prompt_max_len
        self.kl_ctl = kl_controller
        self.strategy = strategy
        self.reward_fn = reward_fn
        self.shared_actorcritic = shared_actorcritic
        self.threshold = threshold
        self.reward_cap = reward_cap
        self.target_dist_beta = target_dist_beta
        self.rm_type = rm_type
        self.actor_loss_type = actor_loss_type
        self.max_new_tokens = max_new_tokens

        assert actor_loss_type is not None

        if self.actor_loss_type == "ppo":
            self.multiply_by_beta = False
        else:
            self.multiply_by_beta = True

        self.save_negdata = save_negdata
        self.save_negdata_threshold = save_negdata_threshold
        if self.save_negdata:
            self.neg_data = set()

    # tokenizer
    def tokenize_fn(self, texts, max_length, device):
        batch = self.tokenizer(
            texts,
            return_tensors="pt",
            add_special_tokens=False,
            max_length=max_length,
            padding=True,
            truncation=True,
        )
        return {k: v.to(device) for k, v in batch.items()}



    @torch.no_grad()
    def make_experience(self, prompts: Union[str, List[str]], samples_per_prompt: int = 1, **generate_kwargs) -> Experience:
        expanded_prompts = tile_prompts(prompts, samples_per_prompt)


        if self.shared_actorcritic:
            action_log_probs, action_mask, attention_mask, num_actions, sequences, value = self.generate_seqs_and_get_logprobs(
                expanded_prompts, **generate_kwargs)
        else:
            action_log_probs, action_mask, attention_mask, num_actions, sequences = self.generate_seqs_and_get_logprobs(
                expanded_prompts, **generate_kwargs)

            if self.critic is not None:
                # values
                value = self.critic(sequences, action_mask, attention_mask)

            else:
                value = None

        print("MAKE EXPERIENCE INSPECTION")
        print(sequences)
        print(attention_mask)
        print(action_mask)



        # print("action log probs check")
        # print(action_log_probs)
        #
        # action_log_probs2 = self.actor(sequences, num_actions, attention_mask)
        #
        # print(action_log_probs2)

        # print("base action inspection")
        # print(sequences)
        # print(sequences[0])
        # print(sequences[1])
        # print(sequences[10])
        # print(sequences[11])

        # output = self.tokenizer.batch_decode(
        #     sequences,
        #     skip_special_tokens=True)

        # print("text output")
        # print(output)
        #
        # print(num_actions)
        # print(attention_mask)
        # print(attention_mask[0])
        # print(attention_mask[1])
        # print(attention_mask[10])
        # print(attention_mask[11])

        # print("device check")
        # print(sequences.device)
        # print(attention_mask.device)
        # print(self.initial_model.model.device)

        # init log probs
        base_action_log_probs = self.initial_model(sequences, num_actions,
                                                   attention_mask)
        # print("--BASE ACTION LOG PROBS--")
        # print(base_action_log_probs.mean())
        # print(base_action_log_probs)

        r = self.compute_reward_no_kl(sequences, attention_mask, multiply_by_beta=self.multiply_by_beta)

        # print("--Rewards--")
        # print(r)
        # print(r.mean())
        # print("--End Rewards--")

        rewards, kl = compute_reward(
            r,
            self.kl_ctl.value,
            action_log_probs,
            base_action_log_probs,
            action_mask=action_mask,
        )
        print("--Rewards--")
        print(rewards)
        print(rewards.sum(-1))
        # print("--End Rewards--")

        if value is None:
            advantages = torch.zeros_like(rewards)
            returns = action_mask * rewards
            value = torch.zeros_like(rewards)
        else:
            advantages, returns = self.get_advantages_and_returns(
                value,
                rewards,
                action_mask,
                generate_kwargs["gamma"],
                generate_kwargs["lambd"],
            )

        # print("INSPECTION")
        # print(returns)
        # print(advantages)
        # print(returns.shape)
        # print(advantages.shape)
        #
        # returns2 = action_mask * rewards
        # print("INSPECTION2")
        # print(returns2)
        # print(returns2.shape)
        # returns3 = returns2.sum(dim=-1)
        # print("INSPECTION3")
        # print(returns3.shape)
        # print(returns3)
        # print("COMPARISON")
        # print(returns2 - returns)
        # print(torch.abs(returns2 - returns).sum())
        # 1/0



        info = {
            "kl": masked_mean(kl, action_mask, dim=-1),
            "reward": r,
            "return": rewards.sum(dim=-1),
            "response_length": action_mask.float().sum(dim=-1),
            "total_length": attention_mask.float().sum(dim=-1),
        }
        # reset model state
        self.actor.train()
        if self.critic is not None:
            self.critic.train()

        return Experience(
            sequences,
            action_log_probs,
            value,
            returns,
            advantages,
            attention_mask,
            action_mask,
            info,
        )



    def compute_reward_no_kl(self, sequences, attention_mask, class_num=0, multiply_by_beta=False):
        # rewards
        if self.remote_rm_url is not None:

            # TODO not yet supported/checked with custom_single_prompt

            # remote RM
            queries = self.tokenizer.batch_decode(sequences.cpu(),
                                                  skip_special_tokens=False)
            r = remote_rm_fn(self.remote_rm_url, queries=queries).to(
                device=attention_mask.device)
        else:

            # local RM
            r = self.reward_model(sequences, attention_mask)

        if self.save_negdata:

            print("savenegdata")
            print(len(self.neg_data))
            print(sequences.shape)
            # print(sequences)
            # print(r < self.save_negdata_threshold)
            # print(sequences[r < self.save_negdata_threshold])
            seqs_below_threshold = sequences[r < self.save_negdata_threshold]
            print(seqs_below_threshold.shape)

            queries = self.tokenizer.batch_decode(seqs_below_threshold.cpu(),
                                                  skip_special_tokens=False)
            # self.neg_data.extend(sequences[r < self.save_negdata_threshold])
            self.neg_data.update(queries) # keep only unique samples
            # print(self.neg_data)
            print(len(self.neg_data))



        # if self.rm_type is None:
        #     return r

        if self.rm_type == "exp_beta_toxicity_class_logprob":
            print("WARNING: only set up for toxicity so far") # TODO later make more flexible, for different reward models. Also, be careful that this is the only place the reward model is used
            score = r
            nontoxic_class_logprob = torch.nn.functional.logsigmoid(score)

            if class_num == 1:
                log_prob_of_class = nontoxic_class_logprob
            else:
                assert class_num == 0
                # toxic_class_prob = 1 - torch.exp(nontoxic_class_logprob)
                # log_prob_of_class = torch.log(toxic_class_prob)

                # NUMERICAL STABILITY
                # Note that we want to calculate: log prob of class 0
                # but we have a logit for class 1, call it c
                # So naively, we would take log(1 - sigmoid(c))
                # This is equivalent to log(1 - 1/(1+e^-c))
                # = log(e^-c/(1+e^-c))
                # = -c - log(1+e^-c)
                # Softplus function is log(1 + e^x)
                # So -score - softplus(-score) is one way of doing this
                # Alternatively, you can write out:
                # log(1 - sigmoid(c)) = log(1 - e^c/(1+e^c))
                # = log(1/(1+e^c))
                # = - log(1 + e^c)
                # = - softplus(score)

                # print("INSPECTING REWARDS: score")
                # print(score)
                # print("INSPECTING REWARDS: nontoxic_class_logprob")
                # print(nontoxic_class_logprob)
                # print("INSPECTING REWARDS: toxic_class_prob")
                # print(toxic_class_prob)
                # print("INSPECTING REWARDS: log_prob_of_class")
                # print(log_prob_of_class)

                log_prob_of_class = -torch.nn.functional.softplus(score)
                # print("INSPECTING REWARDS: log_prob_of_class (softplus)")
                # print(log_prob_of_class)

            final_reward = log_prob_of_class
            # Because remember r_u = 1/beta log phi is the right way to set up the unregularized reward for equivalence between standard RL formulation and our setup
            # BUT remember that phi = p(class | s)^\beta right? So log phi is beta * p(class | s). But anyway, my experiments just use beta = 1 here...
        elif self.rm_type == "indicator_below_threshold": # works for any arbitrary indicator function on checking if score is less than threshold
            eps = 1e-16
            score = r
            # print("score")
            # print(score)
            final_reward = torch.log((score < self.threshold) + eps)
        elif self.rm_type == "toy_rlhf":
            score = r
            capped_reward = torch.minimum(score, self.reward_cap * torch.ones_like(score))

            # print(score)
            # print(capped_reward)
            # print(self.target_dist_beta) # Debug only

            final_reward = capped_reward # Here, 1/beta log phi = 1/beta log e^beta (capped r) = capped r.

        else:
            raise NotImplementedError

        if multiply_by_beta: # Use for twist formulation
            return final_reward * self.target_dist_beta
        else: # Use for PPO formulation
            if self.target_dist_beta < 0:
                return -final_reward # For PPO, if we have target e^{beta r} where beta is negative, say beta = -b, then e^{-br} = e^{b(-r)} which is equivalent to using PPO on a new reward model r' = -r. So what we'll instead do is keep the KL penalty as 1/beta but now have the reward -r. Twist formulations directly handle multiplying by beta
            else:
                return final_reward

    def set_all_eval(self):
        self.actor.eval()
        if self.critic is not None:
            self.critic.eval()
        self.initial_model.eval()
        if self.reward_model is not None:
            self.reward_model.eval()

    def generate_seqs_and_get_logprobs(self, prompts, **generate_kwargs):
        self.set_all_eval()
        # generate seq
        inputs = self.tokenize_fn(prompts, self.prompt_max_len, device="cuda")

        # print(inputs)
        # print(generate_kwargs)

        # with profile(activities=[ProfilerActivity.CPU, ProfilerActivity.CUDA],
        #              profile_memory=True, record_shapes=True) as prof:

        sequences, attention_mask, action_mask = self.actor.generate(**inputs,
                                                                     **generate_kwargs)
        # print("PROFILE GENERATE")
        # print(prof.key_averages().table(sort_by="self_cuda_memory_usage"))


        print("generate_inspection")
        print(sequences)
        print(attention_mask)
        print(action_mask)
        # print(prompts)
        # print(prompts[0])
        # # print(prompts[1])
        # # print(prompts[10])
        # # print(prompts[11])
        #
        # print(inputs)
        # print(inputs['input_ids'][0])
        # # print(inputs['input_ids'][1])
        # # print(inputs['input_ids'][10])
        # # print(inputs['input_ids'][11])
        # print(sequences)
        # print(sequences[0])
        # # print(sequences[1])
        # # print(sequences[10])
        # # print(sequences[11])

        # if self.shared_actorcritic:
        #     sequences = torch.tensor([[7454, 2402, 257, 640, 11, 612, 373, 257, 8966, 326,
        #              561, 5858, 790, 3329, 13, 21326, 340, 28077, 11, 262,
        #              8966, 373, 9314, 13, 4380, 422, 1978, 8288],
        #             [7454, 2402, 257, 640, 11, 612, 373, 257, 1263, 8848,
        #              351, 257, 1263, 18021, 13, 383, 8848, 373, 845, 1593,
        #              284, 262, 661, 319, 340, 11, 523, 484]],
        #            device='cuda:0')
        #     # TODO REMOVE LATER DEBUG ONLY

        num_actions = action_mask.size(1)
        # print("--NUM ACTIONS--")
        # print(num_actions)
        # print("--ACTION MASK--")
        # print(action_mask.size()) # check this matches the output_len
        # print(action_mask)
        # print("--Sequences--")
        # print(sequences)
        # print(self.tokenizer.batch_decode(sequences))
        # print("--End Sequences--")
        # log probs
        if self.shared_actorcritic:

            # print("attention_mask")
            # print(attention_mask.shape)
            # print(sequences.shape)

            action_log_probs, values = self.actor(sequences, num_actions, attention_mask)
            return action_log_probs, action_mask, attention_mask, num_actions, sequences, values
        else:
            action_log_probs = self.actor(sequences, num_actions, attention_mask)
            # print("--ACTION LOG PROBS--")
            # print(action_log_probs.mean())
            # print(action_log_probs)
            return action_log_probs, action_mask, attention_mask, num_actions, sequences

    @torch.no_grad()
    def get_advantages_and_returns(
        self,
        values: torch.Tensor,
        rewards: torch.Tensor,
        action_mask: torch.Tensor,
        gamma: float,
        lambd: float,
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        """Function that computes advantages and returns from rewards and values.
        Calculated as in the original PPO paper: https://arxiv.org/abs/1707.06347
        Note that rewards may include a KL divergence loss term.

        Advantages looks like this:
        Adv1 =  R1 + γ * λ * R2     + γ^2 * λ^2 * R3       + ...
              - V1 + γ * (1 - λ) V2 + γ^2 * λ * (1 - λ) V3 + ...

        Returns looks like this:
        Ret1 =  R1 + γ * λ * R2     + γ^2 * λ^2 * R3       + ...
                   + γ * (1 - λ) V2 + γ^2 * λ * (1 - λ) V3 + ...

        Input:
        - values: Tensor of shape (batch_size, response_size)
        - rewards: Tensor of shape (batch_size, response_size)

        Output:
        - advantages: Tensor of shape (batch_size, response_size)
        - returns: Tensor of shape (batch_size, response_size)
        """
        lastgaelam = 0
        advantages_reversed = []
        response_length = rewards.size(1)

        # Mask invalid responses
        values = action_mask * values
        rewards = action_mask * rewards

        for t in reversed(range(response_length)):
            nextvalues = values[:, t + 1] if t < response_length - 1 else 0.0
            delta = rewards[:, t] + gamma * nextvalues - values[:, t]
            lastgaelam = delta + gamma * lambd * lastgaelam
            advantages_reversed.append(lastgaelam)
        advantages = torch.stack(advantages_reversed[::-1], dim=1)
        returns = advantages + values
        # print("ADV-RETURNS")
        # print(returns)
        # print("ADV-VALUES")
        # print(values)
        # print("ADV-ADV")
        # print(advantages)

        return advantages.detach(), returns


class RemoteExperienceMaker(NaiveExperienceMaker):
    def __init__(self, *args, vllm_engines: List = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.vllm_engines = vllm_engines

    @torch.no_grad()
    def make_experience(self, prompts: Union[str, List[str]], **generate_kwargs) -> Experience:
        self.actor.eval()
        device = torch.cuda.current_device()

        # generate sequence
        start = time.time()
        sequences, attention_mask, action_mask = (
            self._generate_local(prompts, **generate_kwargs)
            if self.vllm_engines is None
            else self._generate_vllm(prompts, **generate_kwargs)
        )
        generate_time = time.time() - start

        num_actions = action_mask.size(1)
        sequences_cpu, attention_mask_cpu, action_mask_cpu = (
            sequences.to("cpu"),
            attention_mask.to("cpu"),
            action_mask.to("cpu"),
        )

        # init log probs
        base_action_log_probs_ref = self.initial_model.forward.remote(sequences_cpu, num_actions, attention_mask_cpu)

        if self.shared_actorcritic:
            raise NotImplementedError # Below stuff not implemented for this yet
        # values
        value_ref = self.critic.forward.remote(sequences_cpu, action_mask_cpu, attention_mask_cpu)

        # avoid CUDA OOM when colocate models
        if self.strategy.args.colocate_critic_reward:
            ray.get([value_ref])
            ray.get([self.critic.empty_cache.remote()])

        if self.strategy.args.colocate_actor_ref:
            ray.get([base_action_log_probs_ref])
            ray.get([self.initial_model.empty_cache.remote()])

        # rewards
        r_refs = []
        # support remote RM API with ray
        if not self.remote_rm_url:
            for rm in self.reward_model:
                r_refs.append(rm.forward.remote(sequences_cpu, attention_mask_cpu))
        else:
            # remote RM
            for rm in self.remote_rm_url:
                queries = self.tokenizer.batch_decode(sequences.cpu(), skip_special_tokens=False)
                r = remote_rm_fn_ray.remote(rm, queries=queries)
                r_refs.append(r)

        # log probs
        start = time.time()
        action_log_probs = self.actor(sequences, num_actions, attention_mask)
        actor_time = time.time() - start

        # wait initial/critic/reward model done
        start = time.time()
        ref_values = ray.get([base_action_log_probs_ref, value_ref] + r_refs)
        wait_time = time.time() - start

        base_action_log_probs, value, rewards = ref_values[0], ref_values[1], ref_values[2:]
        base_action_log_probs, value = base_action_log_probs.to(device), value.to(device)
        rewards = [r.to(device) for r in rewards]
        r = self.reward_fn(rewards) if len(rewards) > 0 else rewards[0]

        # avoid CUDA OOM when colocate models
        if self.strategy.args.colocate_critic_reward and not self.remote_rm_url:
            ray.get([self.reward_model[0].empty_cache.remote()])

        if self.strategy.args.colocate_actor_ref:
            torch.cuda.empty_cache()

        # TODO check whether you want clamping in the below
        reward, kl = compute_reward(
            r,
            self.kl_ctl.value,
            action_log_probs,
            base_action_log_probs,
            action_mask=action_mask,
        )
        advantage, returns = self.get_advantages_and_returns(
            value,
            reward,
            action_mask,
            generate_kwargs["gamma"],
            generate_kwargs["lambd"],
        )

        info = {
            "kl": masked_mean(kl, action_mask, dim=-1),
            "reward": r,
            "return": reward.sum(dim=-1),
            "response_length": action_mask.float().sum(dim=-1),
            "total_length": attention_mask.float().sum(dim=-1),
        }

        if self.strategy.args.perf:
            batch_size = 1 if isinstance(prompts, str) else len(prompts)
            info["generate_time"] = torch.full((batch_size,), generate_time, device=device)
            info["actor_time"] = torch.full((batch_size,), actor_time, device=device)
            info["wait_time"] = torch.full((batch_size,), wait_time, device=device)

        experience = Experience(
            sequences,
            action_log_probs,
            value,
            returns,
            advantage,
            attention_mask,
            action_mask,
            info,
        )

        # send experience to critic
        experience_cpu = deepcopy(experience)
        experience_cpu.to_device("cpu")
        self._ref = self.critic.append.remote(experience_cpu)

        self.actor.train()  # reset model state
        return experience

    def _generate_local(self, prompts: List[str], **kwargs) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        inputs = self.tokenize_fn(prompts, self.prompt_max_len, device="cuda")
        return self.actor.generate(**inputs, **kwargs)

    def _generate_vllm(self, prompts: List[str], **kwargs) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        from vllm import SamplingParams

        # round-robin load balance
        rank = torch.distributed.get_rank()
        llm = self.vllm_engines[rank % len(self.vllm_engines)]

        sampling_params = SamplingParams(
            temperature=kwargs.get("temperature", 1.0),
            top_p=kwargs.get("top_p", 1.0),
            top_k=kwargs.get("top_k", -1),
            max_tokens=kwargs.get("max_new_tokens", 1024),
            min_tokens=kwargs.get("min_new_tokens", 1),
            skip_special_tokens=kwargs.get("skip_special_tokens", False),
        )

        # TODO: can't pass `max_length` to vLLM's tokenizer for input truncation, remove this once it is supported.
        input_ids = self.tokenize_fn(prompts, self.prompt_max_len, device="cpu")["input_ids"]
        assert self.tokenizer.padding_side == "left", f"tokenizer padding_size should be left"
        pad_indices = (input_ids != self.tokenizer.pad_token_id).to(dtype=torch.int).argmax(dim=-1)
        prompt_token_ids = []
        for i, pad_index in enumerate(pad_indices.numpy()):
            prompt_token_ids.append(input_ids[i][pad_index:].tolist())
        outputs = ray.get(llm.generate.remote(sampling_params=sampling_params, prompt_token_ids=prompt_token_ids))

        # NOTE: concat all outputs to following format:
        #
        # | [PAD] [PAD] token token token | token token [EOS] [PAD] |
        # | token token token token token | token token [EOS] [PAD] |
        # | [PAD] [PAD] [PAD] token token | token token token [EOS] |
        # |<---------- prompt ----------->|<-------- answer ------->|
        max_input_len, max_output_len = 0, 0
        for output in outputs:
            max_input_len = max(max_input_len, len(output.prompt_token_ids))
            max_output_len = max(max_output_len, len(output.outputs[0].token_ids))

        pad_token_id, eos_token_id = self.tokenizer.pad_token_id, self.tokenizer.eos_token_id
        sequences = []
        for output in outputs:
            # left padding input
            input_len = len(output.prompt_token_ids)
            input_ids = [pad_token_id] * (max_input_len - input_len) + list(output.prompt_token_ids)

            # right padding output
            output_len = len(output.outputs[0].token_ids)
            output_ids = list(output.outputs[0].token_ids) + [pad_token_id] * (max_output_len - output_len)

            if output_ids[output_len - 1] != eos_token_id:
                raise Exception # This is likely doing the wrong thing, e.g. see https://github.com/OpenRLHF/OpenRLHF/issues/238
                output_ids[min(output_len, len(output_ids) - 1)] = eos_token_id

            # concat input and output
            sequences.append(input_ids + output_ids)

        sequences = torch.tensor(sequences)
        sequences, attention_mask, action_mask = self.actor.process_sequences(
            sequences, max_input_len, eos_token_id, pad_token_id
        )
        return sequences.to("cuda"), attention_mask.to("cuda"), action_mask.to("cuda")

    def flush(self):
        "Ensure all experience has been send to critic"
        ray.get(self._ref)
        self._ref = None
