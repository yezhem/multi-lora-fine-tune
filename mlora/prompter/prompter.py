import json
from typing import Dict, List


class Prompter:
    template_: Dict[str, str] = None

    def __init__(self, template: str):
        with open(template) as fp:
            self.template_ = json.load(fp)

    def generate_prompt(self, data: Dict[str, str]) -> List[str]:
        ret_val = ""

        try:
            ret_val = self.template_["prompt"].format(**data)
        except:
            ret_val = ""

        if ret_val == "":
            ret_val = self.template_["prompt_no_input"].format(**data)

        return [ret_val]

    def generate_prompt_batch(self, datas: List[Dict[str, str]]) -> List[str]:
        ret_data = []
        for data in datas:
            ret_data.extend(self.generate_prompt(data))
        return ret_data

    def get_response(self, output: str) -> str:
        return output.split(self.template["response"])[-1].strip()
