import re
from typing import Dict, Optional

from loguru import logger

from data_juicer.ops.base_op import OPERATORS, UNFORKABLE, Mapper
from data_juicer.utils.lazy_loader import LazyLoader
from data_juicer.utils.model_utils import get_model, prepare_model

torch = LazyLoader('torch', 'torch')
vllm = LazyLoader('vllm', 'vllm')

OP_NAME = 'optimize_qa_mapper'


# TODO: Extend LLM-based OPs into API-based implementation.
@UNFORKABLE.register_module(OP_NAME)
@OPERATORS.register_module(OP_NAME)
class OptimizeQAMapper(Mapper):
    """
    Mapper to optimize question-answer pairs.
    """

    # avoid leading whitespace
    DEFAULT_SYSTEM_PROMPT = ('请优化输入的问答对，使【问题】和【回答】都更加详细、准确。'
                             '必须按照以下标记格式，直接输出优化后的问答对：\n'
                             '【问题】\n'
                             '优化后的问题\n'
                             '【回答】\n'
                             '优化后的回答')
    DEFAULT_INPUT_TEMPLATE = '以下是原始问答对：\n{}'
    DEFAULT_QA_PAIR_TEMPLATE = '【问题】\n{}\n【回答】\n{}'
    DEFAULT_OUTPUT_PATTERN = r'.*?【问题】\s*(.*?)\s*【回答】\s*(.*)'

    _accelerator = 'cuda'

    def __init__(self,
                 hf_model: str = 'Qwen/Qwen2.5-7B-Instruct',
                 *,
                 system_prompt: Optional[str] = None,
                 input_template: Optional[str] = None,
                 qa_pair_template: Optional[str] = None,
                 output_pattern: Optional[str] = None,
                 enable_vllm: bool = False,
                 model_params: Optional[Dict] = None,
                 sampling_params: Optional[Dict] = None,
                 **kwargs):
        """
        Initialization method.

        :param hf_model: Hugging Face model ID.
        :param system_prompt: System prompt for guiding the optimization task.
        :param input_template: Template for building the input for the model.
            Please make sure the template contains one placeholder '{}', which
            corresponds to the question and answer pair generated by
            param `qa_pair_template`.
        :param qa_pair_template: Template for formatting the question and
            answer pair. Please make sure the template contains two
            '{}' to format question and answer.
        :param output_pattern: Regular expression pattern to extract question
            and answer from model response.
        :param enable_vllm: Whether to use VLLM for inference acceleration.
        :param model_params: Parameters for initializing the model.
        :param sampling_params: Sampling parameters for text generation (e.g.,
            {'temperature': 0.9, 'top_p': 0.95}).
        :param kwargs: Extra keyword arguments.
        """
        super().__init__(**kwargs)

        self.system_prompt = system_prompt or self.DEFAULT_SYSTEM_PROMPT
        self.input_template = input_template or self.DEFAULT_INPUT_TEMPLATE
        self.qa_pair_template = qa_pair_template or \
            self.DEFAULT_QA_PAIR_TEMPLATE
        self.output_pattern = output_pattern or self.DEFAULT_OUTPUT_PATTERN

        self.enable_vllm = enable_vllm
        model_params = model_params or {}
        sampling_params = sampling_params or {}

        if enable_vllm:
            assert torch.cuda.device_count() >= 1, 'must be executed in CUDA'
            # cannot initialize vllm replicas on different GPUs
            self.num_proc = 1
            if model_params.get('tensor_parallel_size') is None:
                tensor_parallel_size = torch.cuda.device_count()
                logger.info(f'Set tensor_parallel_size to \
                    {tensor_parallel_size} for vllm.')
                model_params['tensor_parallel_size'] = tensor_parallel_size
            self.model_key = prepare_model(
                model_type='vllm',
                pretrained_model_name_or_path=hf_model,
                **model_params)
            self.sampling_params = vllm.SamplingParams(**sampling_params)
        else:
            self.model_key = prepare_model(
                model_type='huggingface',
                pretrained_model_name_or_path=hf_model,
                return_pipe=True,
                **model_params)
            self.sampling_params = sampling_params

    def build_input(self, sample):
        qa_pair = self.qa_pair_template.format(sample[self.query_key],
                                               sample[self.response_key])
        input_prompt = self.input_template.format(qa_pair)
        return input_prompt

    def parse_output(self, raw_output):
        logger.debug(raw_output)
        matches = re.findall(self.output_pattern, raw_output, re.DOTALL)
        if matches:
            match = matches[0]
            return match.group(1).strip(), match.group(2).strip()
        else:
            return None, None

    def process_single(self, sample=None, rank=None):
        model, _ = get_model(self.model_key, rank, self.use_cuda())

        input_prompt = self.build_input(sample)
        messages = [{
            'role': 'system',
            'content': self.system_prompt
        }, {
            'role': 'user',
            'content': input_prompt
        }]

        if self.enable_vllm:
            response = model.chat(messages, self.sampling_params)
            output = response[0].outputs[0].text
        else:
            # model is pipe
            response = model(messages,
                             return_full_text=False,
                             **self.sampling_params)
            output = response[0]['generated_text']

        parsed_q, parsed_a = self.parse_output(output)
        if parsed_q:
            sample[self.query_key] = parsed_q
        if parsed_a:
            sample[self.response_key] = parsed_a

        return sample