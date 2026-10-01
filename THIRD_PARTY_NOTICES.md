# Third-party notices

Market Book Connector installs and uses the following third-party software and
model. They are not bundled in this repository; each is downloaded at install or
first run and remains under its own licence. Versions are the ones the project was
tested with; `requirements.txt` does not pin versions.

## Python packages

| Package | Tested version | Licence | Project page |
| --- | --- | --- | --- |
| PyYAML | 6.0.3 | MIT | [pyyaml.org](https://pyyaml.org/) |
| numpy | 2.2.6 | BSD License (classifier) | [numpy.org](https://numpy.org) |
| torch | 2.12.1+cpu | BSD-3-Clause | [pytorch.org](https://pytorch.org) |
| transformers | 5.12.1 | Apache 2.0 License | [Hugging Face Transformers](https://github.com/huggingface/transformers) |
| sentence-transformers | 5.6.0 | Apache-2.0 | [Sentence Transformers](https://www.SBERT.net) |
| huggingface-hub | 1.21.0 | Apache-2.0 | [Hugging Face Hub](https://github.com/huggingface/huggingface_hub) |
| tokenizers | 0.22.2 | Apache Software License (classifier) | [Tokenizers](https://github.com/huggingface/tokenizers) |
| safetensors | 0.8.0 | Apache Software License (classifier) | [Safetensors](https://github.com/huggingface/safetensors) |

Installed `.dist-info` licence and notice files found:

- PyYAML 6.0.3: `licenses/LICENSE`
- numpy 2.2.6: `LICENSE.txt`
- torch 2.12.1+cpu: `licenses/LICENSE`, `licenses/NOTICE`
- transformers 5.12.1: `licenses/LICENSE`
- sentence-transformers 5.6.0: `licenses/LICENSE`, `licenses/NOTICE.txt`
- huggingface-hub 1.21.0: `licenses/LICENSE`
- tokenizers 0.22.2: none found
- safetensors 0.8.0: `licenses/LICENSE`

Licence metadata came from each installed distribution's `METADATA`. The
`License-Expression` value was `Apache-2.0` for sentence-transformers. The
`License` values were `MIT` for PyYAML, `BSD-3-Clause` for torch, `Apache 2.0
License` for transformers, and `Apache-2.0` for huggingface-hub. NumPy's `License`
field is a long text, so the table uses its `BSD License` classifier; its local
`LICENSE.txt` contains BSD three-clause terms and the metadata also mentions
bundled components. Tokenizers and safetensors use their `Apache Software License`
classifiers because neither has a `License` or `License-Expression` value.
PyYAML's classifier is `License :: OSI Approved :: MIT License`, NumPy's is
`License :: OSI Approved :: BSD License`, and huggingface-hub, tokenizers, and
safetensors each have `License :: OSI Approved :: Apache Software License`.
No `License ::` classifier was present for torch, transformers, or
sentence-transformers.

## Embedding model

| Model | Revision | Licence | Source |
| --- | --- | --- | --- |
| intfloat/e5-small-v2 | ffb93f3bd4047442299a41ebb6fa998a38507c52 | mit | https://huggingface.co/intfloat/e5-small-v2 |

The local model card at
`.hf_cache/hub/models--intfloat--e5-small-v2/snapshots/ffb93f3bd4047442299a41ebb6fa998a38507c52/README.md:2604`
contains `license: mit` in its YAML front matter.

## Command-line model tools

Live runs call a command-line model tool that the user installs separately
(for example Claude Code or Codex CLI). Those tools and the models behind them
are governed by their providers' own terms and are not part of this repository.

If you redistribute any of the packages or the model, you must include their
licence and notice files as their licences require.
