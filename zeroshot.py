import re
import random
from base import Agent
from colorama import Fore, Style
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
import warnings
from transformers import logging as transformers_logging

from utils import strip_all_lines

# Ignore warning messages from transformers
warnings.filterwarnings("ignore")
transformers_logging.set_verbosity_error()

class LocalModelAgent(Agent):
    """
    A base agent that uses a local model for text generation tasks.
    """
    def __init__(self, config: dict) -> None:
        """
        Initialize the local model
        """
        super().__init__(config)
        self.llm_config = config
        if config['use_8bit']:
            quantization_config = BitsAndBytesConfig(
                load_in_8bit=True,
                llm_int8_has_fp16_weight=False
            )
            self.model = AutoModelForCausalLM.from_pretrained(
                config["model_name"],
                quantization_config=quantization_config,
                device_map=config["device"]
            )
        else:
            self.model = AutoModelForCausalLM.from_pretrained(
                config["model_name"],
                torch_dtype=torch.float16,
                device_map=config["device"]
            )
        self.tokenizer = AutoTokenizer.from_pretrained(config["model_name"])
        self.model.eval()

    def generate_response(self, messages: list) -> str:
        """
        Generate a response using the local model.
        """
        text_chat = self.tokenizer.apply_chat_template(
            messages,
            tokenize=False,
            add_generation_prompt=True
        )
        model_inputs = self.tokenizer([text_chat], return_tensors="pt").to(self.model.device)

        generated_ids = self.model.generate(
            **model_inputs,
            max_new_tokens=self.llm_config["max_tokens"],
            do_sample=False
        )
        generated_ids = [
            output_ids[len(input_ids):] for input_ids, output_ids in zip(model_inputs.input_ids, generated_ids)
        ]

        return self.tokenizer.batch_decode(generated_ids, skip_special_tokens=True)[0]

    def update(self, correctness: bool) -> bool:
        """
        Update the agent based on the correctness of its output.
        """
        pass  # No update for the non-streaming zeroshot baseline

class ClassificationAgent(LocalModelAgent):
    """
    An agent that classifies text into one of the labels in the given label set.
    """
    @staticmethod
    def get_system_prompt() -> str:
        system_prompt = """\
        Act as a professional medical doctor that can diagnose the patient based on the patient profile.
        Provide your diagnosis in the following format: <number>. <diagnosis>""".strip()
        return strip_all_lines(system_prompt)

    @staticmethod
    def get_zeroshot_prompt(
        option_text: str,
        text: str
    ) -> str:
        prompt = f"""\
        Act as a medical doctor and diagnose the patient based on the following patient profile:

        {text}

        All possible diagnoses for you to choose from are as follows (one diagnosis per line, in the format of <number>. <diagnosis>):
        {option_text}

        Now, directly provide the diagnosis for the patient in the following format: <number>. <diagnosis>""".strip()
        return strip_all_lines(prompt)

    def __call__(
        self,
        label2desc: dict[str, str],
        text: str
    ) -> str:
        self.reset_log_info()
        option_text = '\n'.join([f"{str(k)}. {v}" for k, v in label2desc.items()])
        prompt = self.get_zeroshot_prompt(option_text, text)
        messages = [
            {"role": "system", "content": self.get_system_prompt()},
            {"role": "user", "content": prompt}
        ]
        response = self.generate_response(messages)
        prediction = self.extract_label(response, label2desc)
        self.update_log_info(log_data={
            "input_pred": prompt,
            "output_pred": response,
            "num_input_tokens": len(self.tokenizer.encode(self.get_system_prompt() + prompt)),
            "num_output_tokens": len(self.tokenizer.encode(response))
        })
        return prediction

    @staticmethod
    def extract_label(pred_text: str, label2desc: dict[str, str]) -> str:
        numbers = re.findall(pattern=r"(\d+)\.", string=pred_text)
        if len(numbers) == 1:
            number = numbers[0]
            if int(number) in label2desc:
                prediction = number
            else:
                print(Fore.RED + f"Prediction {pred_text} not found in the label set. Randomly select one." + Style.RESET_ALL)
                prediction = random.choice(list(label2desc.keys()))
        else:
            if len(numbers) > 1:
                print(Fore.YELLOW + f"Extracted numbers {numbers} is not exactly one. Select the first one." + Style.RESET_ALL)
                prediction = numbers[0]
            else:
                print(Fore.RED + f"Prediction {pred_text} has no extracted numbers. Randomly select one." + Style.RESET_ALL)
                prediction = random.choice(list(label2desc.keys()))
        return str(prediction)

class SQLGenerationAgent(LocalModelAgent):
    """
    An agent that generates SQL code based on the given table schema and the user query.
    """
    @staticmethod
    def get_system_prompt() -> str:
        system_prompt = """\
        Act as a professional programmer.
        You will be given a table schema and a user query, and you need to generate the correct SQL code to answer the user query in the following format:
        ```sql\n<your_SQL_code>\n```"""
        return strip_all_lines(system_prompt)

    @staticmethod
    def get_zeroshot_prompt(table_schema: str, user_query: str) -> str:
        prompt = f"""\
        {table_schema}
        
        -- Using valid SQLite, answer the following question for the tables provided above.
        -- Question: {user_query}
        
        Now, generate the correct SQL code directly in the following format:
        ```sql\n<your_SQL_code>\n```"""
        return strip_all_lines(prompt)

    def __call__(
        self,
        table_schema: str,
        user_query: str
    ) -> str:
        self.reset_log_info()
        prompt = self.get_zeroshot_prompt(table_schema, user_query)
        messages = [
            {"role": "system", "content": self.get_system_prompt()},
            {"role": "user", "content": prompt}
        ]
        response = self.generate_response(messages)
        sql_code = self.parse_sql(response)
        self.update_log_info(log_data={
            "input_pred": prompt,
            "output_pred": response,
            "num_input_tokens": len(self.tokenizer.encode(self.get_system_prompt() + prompt)),
            "num_output_tokens": len(self.tokenizer.encode(response))
        })
        return sql_code

    @staticmethod
    def parse_sql(pred_text: str) -> str:
        """
        Parse the SQL code from the LLM's response.
        """
        pattern = r"```sql([\s\S]*?)```"
        match = re.search(pattern, pred_text)
        if match:
            sql_code = match.group(1)
            sql_code = sql_code.strip()
            return sql_code
        else:
            print(Fore.RED + "No SQL code found in the response" + Style.RESET_ALL)
            sql_code = pred_text
        return sql_code

if __name__ == "__main__":
    from argparse import ArgumentParser
    from execution_pipeline import main

    parser = ArgumentParser()
    parser.add_argument('--bench_name', type=str, required=True)
    parser.add_argument('--model_name', type=str, default="Qwen/Qwen2.5-7B-Instruct")
    parser.add_argument('--device', type=str, default="cuda:0")
    parser.add_argument('--debug', action='store_true')
    parser.add_argument('--use_8bit', action='store_true')
    parser.add_argument('--output_path', type=str, default=None, help='path to save csv file for kaggle submission')
    parser.add_argument('--use_wandb', action='store_true')
    args = parser.parse_args()

    if args.bench_name.startswith("classification"):
        max_tokens = 32
        agent_name = ClassificationAgent
    elif args.bench_name.startswith("sql_generation"):
        max_tokens = 512
        agent_name = SQLGenerationAgent
    else:
        raise ValueError(f"Invalid benchmark name: {args.bench_name}")
    # Classification: Medical diagnosis; SQL generation: Text-to-SQL
    bench_cfg = {
        'bench_name': args.bench_name,
        'output_path': args.output_path
    }
    llm_config = {
        'model_name': args.model_name,
        'exp_name': f'zeroshot_{args.bench_name}_{args.model_name}_8bit-{args.use_8bit}',
        'bench_name': bench_cfg['bench_name'],
        'max_tokens': max_tokens,
        'do_sample': False,
        'device': args.device,
        'use_8bit': args.use_8bit
    }
    agent = agent_name(llm_config)
    main(agent, bench_cfg, debug=args.debug, use_wandb=args.use_wandb, wandb_name=llm_config["exp_name"], wandb_config=llm_config)
