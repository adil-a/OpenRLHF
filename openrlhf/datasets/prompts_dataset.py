from torch.utils.data import Dataset
from tqdm import tqdm
from .utils import exist_and_not_none


def preprocess_data(data, input_template=None, input_key="input", apply_chat_template=None) -> str:
    if apply_chat_template:
        prompt = apply_chat_template(data[input_key], tokenize=False, add_generation_prompt=True)
    else:
        prompt = data[input_key]
        if input_template:
            prompt = input_template.format(prompt)
    return prompt


class PromptDataset(Dataset):
    """
    Dataset for PPO model

    Args:
        dataset: dataset for PPO model
        tokenizer: tokenizer for PPO model
        max_length: max length of input
    """

    def __init__(
        self,
        dataset,
        tokenizer,
        strategy,
        input_template=None,
    ) -> None:
        super().__init__()
        self.strategy = strategy
        self.tokenizer = tokenizer
        self.n_samples_per_prompt = getattr(self.strategy.args, "n_samples_per_prompt", 1)

        # chat_template
        self.input_template = input_template
        input_key = getattr(self.strategy.args, "input_key", None)
        apply_chat_template = getattr(self.strategy.args, "apply_chat_template", False)

        if apply_chat_template:
            if tokenizer.chat_template is None:
                print("[Warning]: no chat template specified, defaulting to the one from OpenRLHF/Llama-3-8b-sft-mixture")
                from transformers import AutoTokenizer
                tokenizerchat = AutoTokenizer.from_pretrained("OpenRLHF/Llama-3-8b-sft-mixture")
                tokenizer.chat_template = tokenizerchat.chat_template

            apply_chat_template = self.tokenizer.apply_chat_template

        self.prompts = []
        for data in tqdm(dataset, desc="Preprocessing data", disable=not self.strategy.is_rank_0()):
            prompt = preprocess_data(data, input_template, input_key, apply_chat_template)
            self.prompts.append(prompt)

    def __len__(self):
        length = len(self.prompts)
        return length * self.n_samples_per_prompt

    def __getitem__(self, idx):
        # The way this currently works is the dataset itself is extended by n_samples_per_prompt
        # This is fine after a whole pass over the dataset in making experiences... but not what I want for batched CTL/SIXO etc.
        # So for CTL SIXO etc. just use n_samples_per_prompt 1 and use a separate argument
        return self.prompts[idx // self.n_samples_per_prompt]
