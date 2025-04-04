import importlib
import platform
from typing import Sequence

import numpy as np
from tqdm import tqdm

from modules import shared
from modules.cache_utils import process_llamacpp_cache

imported_module = None
not_available_modules = set()


def llama_cpp_lib():
    global imported_module, not_available_modules

    # Determine the platform
    is_macos = platform.system() == 'Darwin'

    # Define the library names based on the platform
    if is_macos:
        lib_names = [
            (None, 'llama_cpp')
        ]
    else:
        lib_names = [
            ('cpu', 'llama_cpp'),
            ('tensorcores', 'llama_cpp_cuda_tensorcores'),
            (None, 'llama_cpp_cuda'),
            (None, 'llama_cpp')
        ]

    for arg, lib_name in lib_names:
        if lib_name in not_available_modules:
            continue

        should_import = (arg is None or getattr(shared.args, arg))

        if should_import:
            if imported_module and imported_module != lib_name:
                # Conflict detected, raise an exception
                raise Exception(f"Cannot import `{lib_name}` because `{imported_module}` is already imported. Switching to a different version of llama-cpp-python currently requires a server restart.")

            try:
                return_lib = importlib.import_module(lib_name)
                imported_module = lib_name
                monkey_patch_llama_cpp_python(return_lib)
                return return_lib
            except ImportError:
                not_available_modules.add(lib_name)
                continue

    return None


def eval_with_progress(self, tokens: Sequence[int]):
    """
    A copy of

    https://github.com/abetlen/llama-cpp-python/blob/main/llama_cpp/llama.py

    with tqdm to show prompt processing progress.
    """
    self._ctx.kv_cache_seq_rm(-1, self.n_tokens, -1)

    if len(tokens) > self.n_batch:
        progress_bar = tqdm(range(0, len(tokens), self.n_batch), desc="Prompt evaluation", leave=False)
    else:
        progress_bar = range(0, len(tokens), self.n_batch)

    for i in progress_bar:
        batch = tokens[i : min(len(tokens), i + self.n_batch)]
        n_past = self.n_tokens
        n_tokens = len(batch)
        self._batch.set_batch(
            batch=batch, n_past=n_past, logits_all=self.context_params.logits_all
        )
        self._ctx.decode(self._batch)
        # Save tokens
        self.input_ids[n_past : n_past + n_tokens] = batch
        # Save logits
        if self.context_params.logits_all:
            rows = n_tokens
            cols = self._n_vocab
            logits = np.ctypeslib.as_array(
                self._ctx.get_logits(), shape=(rows * cols,)
            )
            self.scores[n_past : n_past + n_tokens, :].reshape(-1)[::] = logits
            self.last_updated_index = n_past + n_tokens - 1
        else:
            rows = 1
            cols = self._n_vocab
            logits = np.ctypeslib.as_array(
                self._ctx.get_logits(), shape=(rows * cols,)
            )
            last_token_index = min(n_past + n_tokens - 1, self.scores.shape[0] - 1)
            self.scores[last_token_index, :] = logits.reshape(-1)
            self.last_updated_index = last_token_index
        # Update n_tokens
        self.n_tokens += n_tokens


def monkey_patch_llama_cpp_python(lib):
    if getattr(lib.Llama, '_is_patched', False):
        # If the patch is already applied, do nothing
        return

    def my_generate(self, *args, **kwargs):
        if shared.args.streaming_llm:
            new_sequence = args[0]
            past_sequence = self._input_ids

            # Do the cache trimming for StreamingLLM
            process_llamacpp_cache(self, new_sequence, past_sequence)

        for output in self.original_generate(*args, **kwargs):
            yield output

    lib.Llama.eval = eval_with_progress
    lib.Llama.original_generate = lib.Llama.generate
    lib.Llama.generate = my_generate

    # Also patch Jinja2ChatFormatter to handle loop controls
    if hasattr(lib, 'llama_chat_format') and hasattr(lib.llama_chat_format, 'Jinja2ChatFormatter'):
        Formatter = lib.llama_chat_format.Jinja2ChatFormatter

        if not getattr(Formatter, '_is_patched', False):
            def patched_init(self, *args, **kwargs):
                # Extract parameters from args or kwargs
                if args:
                    self.template = args[0]
                    self.eos_token = args[1] if len(args) > 1 else kwargs.get('eos_token')
                    self.bos_token = args[2] if len(args) > 2 else kwargs.get('bos_token')
                    self.add_generation_prompt = args[3] if len(args) > 3 else kwargs.get('add_generation_prompt', True)
                    self.stop_token_ids = args[4] if len(args) > 4 else kwargs.get('stop_token_ids')
                else:
                    self.template = kwargs.get('template')
                    self.eos_token = kwargs.get('eos_token')
                    self.bos_token = kwargs.get('bos_token')
                    self.add_generation_prompt = kwargs.get('add_generation_prompt', True)
                    self.stop_token_ids = kwargs.get('stop_token_ids')

                # Process stop tokens as in the original
                self.stop_token_ids = (
                    set(self.stop_token_ids) if self.stop_token_ids is not None else None
                )

                # Create environment with loopcontrols extension
                import jinja2
                from jinja2.ext import loopcontrols

                self._environment = jinja2.sandbox.ImmutableSandboxedEnvironment(
                    loader=jinja2.BaseLoader(),
                    trim_blocks=True,
                    lstrip_blocks=True,
                    extensions=[loopcontrols]
                ).from_string(self.template)

            # Replace the original __init__ with our patched version
            Formatter.__init__ = patched_init
            Formatter._is_patched = True

    # Set the flag to indicate that the patch has been applied
    lib.Llama._is_patched = True
