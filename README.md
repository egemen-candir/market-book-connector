# Market Book Connector

Market Book Connector reads a piece of market commentary and checks it against a
curated catalogue of research-derived market states. A *builder* model turns the
commentary into structured observations, a local embedding search picks candidate
states from a frozen index, an *evaluator* model judges each candidate, and
validators check every step. The result is shown in a local dashboard: which states
the commentary supports, which candidates were rejected and why, and what the
research says can go wrong.

Everything runs on your machine. Your choice of command-line model tool decides
whether inference uses a cloud or a local model.

**Everything it produces is research context for analyst review, never a trading
instruction.** No match and pipeline failure are first-class results: an
unsupported or failed run is never dressed up as a finding.

## Why not just paste the research into a prompt?

That is the first question a skeptical reader should ask, so here is a direct answer.

**Partly, you can.** The enriched catalogue is roughly 600 KB, which fits in a current
frontier model's context window. Paste it in with a paragraph of commentary, ask
"which of these states does this support, cite the records", and you will get a
plausible answer, often in better prose than this pipeline's evaluator writes. This
project is not smarter than a frontier model, and it does not claim to be.

What a single prompt does not give you is a way to check the answer. This pipeline
makes a model's reading of commentary against a curated research base constrained,
refusable, reproducible and auditable:

- **Closed-world enforcement.** A prompt *asks* the model to cite only catalogue
  records. Here, validators *check* that every citation belongs to the candidate
  being matched, that labels come from fixed vocabularies, and that no wording
  reads like a trading instruction. Output that fails is rejected: the page reports
  the failure, not a result.
- **Refusal is a real outcome.** A model asked to find a match tends to find one.
  Here, "no supported match" is a first-class, validated result, with a rationale
  saying whether the phenomenon is missing from the catalogue or out of scope
  (for example, a different asset class).
- **Reading and judging are separated.** One model (the *builder*) turns the
  commentary into structured observations. A second model (the *evaluator*) judges
  candidates from those observations; its prompt does not include the original
  prose, which limits how far the commentary's rhetoric carries into the judgement.
  The evaluator's tool can still open files in its working folder; see
  [Evaluator isolation](#evaluator-isolation).
- **Re-derivable candidates.** Candidates come from a local embedding search over a
  frozen, hash-checked index. The same observation packet gives the same shortlist
  on any machine, and every run records its packet, so any shortlist can be
  re-derived and audited later.¹ A prompt takes a different path through the
  context each time and leaves no record of what was considered.
- **What was ruled out is visible.** Each result lists the candidates the evaluator
  rejected, with a reason label, and the similar states that just missed the
  shortlist. You see the alternatives, not only the winner.
- **Every run is a record.** Each run is saved under `connector/runs/<run id>/` with
  a manifest, an input fingerprint, validator results and every intermediate
  artifact. That is the difference between an answer and something you can audit
  later.

¹ The packet is written by the builder, a language model, so rerunning the same
commentary can produce a different packet and therefore different candidates. The
guarantee starts at the packet.

| | Paste research + prompt | This pipeline |
| --- | --- | --- |
| Quality of prose on a single case | Often better | Adequate |
| Citations checked against the catalogue | No (requested, not enforced) | Yes, validated per candidate |
| "Nothing matches" as an answer | Rare; the model tends to stretch a fit | First-class, validated, with a reason |
| Judgement influenced by the commentary's wording | Yes, the model reads the prose | Reduced: the evaluator's prompt carries structured observations, not the prose |
| Shortlist re-derivable from the run record | No | Yes (frozen index, deterministic retrieval from the recorded packet) |
| Compare two models on identical inputs | Hard to control | Per-role model profiles; evaluators compare on identical candidates when the builder output is held fixed |
| Rejected alternatives and near misses | Not recorded | Listed with reasons |
| Audit trail | Chat transcript | Run folder with manifest, fingerprint, checks, artifacts |
| Setup cost | None | Local install; a command-line model for live runs |

### Honest limits

- **The catalogue is small.** It covers a few dozen research-derived market states.
  Most real-world commentary will come back as *no supported match*. That is the
  correct behaviour, but it can look like the tool "does not do much". Treat the
  bundled catalogue as a seed; the method is the product.
- **Passing checks means well-formed, not correct.** The validators confirm
  structure, allowed wording and valid references. They do not confirm that the
  interpretation is right.
- **It reads text, it does not measure markets.** Every result is a model's reading
  of your commentary against research records. It is not market data, not a
  forecast, and not a trading instruction.
- **Known catalogue errors are listed, not silently fixed.** The catalogue and its
  search index are hash-pinned, so corrections to author credits and to some
  figures are recorded in
  [`connector/catalog/CATALOGUE_ERRATA.md`](connector/catalog/CATALOGUE_ERRATA.md).
  Read it before relying on the records it names.

### What you could use it for

These are uses the method supports; whether equivalent tools already exist is worth
checking for your own case.

- **Checking commentary against your own research.** The catalogue and index format
  is the interface. Build a catalogue and index for your own research in the same
  structure, then run incoming notes or internal memos through it: which claims
  have support in *your* research, which do not, and which documented failure
  modes apply. The tools that produced the bundled catalogue are not included;
  adapting your own data is up to you.
- **A grounded devil's advocate for a thesis.** Run an investment memo through it
  and read the matched states' failure patterns: known ways this reading could be
  wrong, each tied to a cited record.
- **Tracking how the narrative shifts.** Run a daily commentary stream and record
  which states match over time. This measures what people are *saying*, not what
  markets are doing, and should be labelled that way.
- **Finding gaps in the catalogue.** No-match results that say "not in the
  catalogue" accumulate into a list of research the book is missing, feedback most
  knowledge bases never get.
- **A test bed for grounded reasoning with refusal.** Frozen retrieval and
  swappable per-role models let you compare evaluators on identical candidates,
  scored on correct matches *and* correct refusals.
- **Beyond markets.** Nothing in the method is market-specific. Any curated corpus
  with typed records (observable signs, failure patterns, implications) fits:
  engineering incident reviews, legal precedent summaries, policy research.
- **Onboarding.** A new analyst pastes their own notes and sees them mapped to the
  research base, including why candidates were rejected.

## Setup

The commands below were verified on Linux x86_64 with Python 3.10.12 and CPU PyTorch. Run them in Bash from this repository's root. Use a Python 3.10 interpreter with `venv` and `pip` available; replace `python3` below if that interpreter has a different name. The frontend is static HTML, CSS, and JavaScript, so there is no Node build. A GPU is not required.

### 1. Install the Python dependencies

Create a new environment. These version arguments select the reference versions used by the working application; `requirements.txt` remains the short, unpinned dependency list.

```bash
python3 --version
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install "torch==2.12.1" \
  --index-url https://download.pytorch.org/whl/cpu
.venv/bin/python -m pip install -r requirements.txt \
  "PyYAML==6.0.3" "numpy==2.2.6" \
  "transformers==5.12.1" "sentence-transformers==5.6.0" \
  "huggingface-hub==1.21.0" "tokenizers==0.22.2" "safetensors==0.8.0"
.venv/bin/python -m pip check
```

The PyTorch install uses the [official PyTorch CPU wheels](https://pytorch.org/get-started/locally/). `pip check` should report `No broken requirements found.` Other operating systems and Python versions have not been verified for this recipe.

### 2. Download the pinned E5 model

The supplied retrieval index uses `intfloat/e5-small-v2` at revision `ffb93f3bd4047442299a41ebb6fa998a38507c52`. This is the local embedding model; the builder and evaluator models are configured separately below.

The cache is excluded from Git, so a fresh clone needs this one-time download before any pipeline run, including the mock example. Internet access is needed for installation and this download; the public model does not require a Hugging Face login. The following command downloads only the nine required files, checks the three hashes recorded in the supplied index manifest, and creates the revision pointer required by the existing loader.

```bash
export HF_HOME="$PWD/.hf_cache"
export HF_HUB_CACHE="$HF_HOME/hub"
unset TRANSFORMERS_CACHE SENTENCE_TRANSFORMERS_HOME
HF_HUB_OFFLINE=0 TRANSFORMERS_OFFLINE=0 .venv/bin/python - <<'PY'
import hashlib
import json
from pathlib import Path

from huggingface_hub import snapshot_download

root = Path.cwd()
revision = "ffb93f3bd4047442299a41ebb6fa998a38507c52"
manifest = json.loads(
    (root / "connector/catalog/glossed/e5/catalog_embedding_index.manifest.json")
    .read_text(encoding="utf-8")
)
files = [
    "config.json", "model.safetensors", "tokenizer.json",
    "tokenizer_config.json", "special_tokens_map.json", "vocab.txt",
    "modules.json", "sentence_bert_config.json", "1_Pooling/config.json",
]
cache = root / ".hf_cache" / "hub"
snapshot = Path(snapshot_download(
    repo_id=manifest["model_name"],
    revision=revision,
    cache_dir=str(cache),
    allow_patterns=files,
    token=False,
))
for name in files:
    if not (snapshot / name).is_file():
        raise RuntimeError(f"Missing model file: {name}")
for record in manifest["model_files"]:
    actual = hashlib.sha256((snapshot / record["name"]).read_bytes()).hexdigest()
    if actual != record["sha256"]:
        raise RuntimeError(f"Model hash mismatch: {record['name']}")

# The existing offline loader resolves the model through refs/main.
ref = cache / "models--intfloat--e5-small-v2" / "refs" / "main"
ref.parent.mkdir(parents=True, exist_ok=True)
ref.write_text(revision, encoding="utf-8")
print(f"Verified E5 cache: {snapshot}")
PY
```

Expected output ends with `Verified E5 cache:` and the snapshot path. Keep that revision: downloading a different model revision can fail the existing index's hash checks. The index is already included and does not need rebuilding. See Hugging Face's [download guide](https://huggingface.co/docs/huggingface_hub/guides/download) for pinned revisions and file selection.

### 3. Set the runtime environment

Run these exports in each terminal used to launch Market Book. They point the model libraries at this repository's cache and keep embedding retrieval offline; they do not control networking by your chosen model CLI.

```bash
export HF_HOME="$PWD/.hf_cache"
export HF_HUB_CACHE="$HF_HOME/hub"
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
unset TRANSFORMERS_CACHE SENTENCE_TRANSFORMERS_HOME
```

The `unset` command removes alternate cache overrides from this shell. The loader expects `.hf_cache/hub/models--intfloat--e5-small-v2/refs/main` to contain the pinned revision and the nine files to be available in its matching `snapshots/<revision>/` directory.

Run the [supplied mock example](#run-the-supplied-mock-example) to check the installation, then start the dashboard or configure your own CLI.

## Dashboard

After setup and the runtime exports above, start the dashboard from this repository root:

```bash
export HF_HOME="$PWD/.hf_cache"
mkdir -p connector/runs
.venv/bin/python dashboard/connector_dashboard.py serve \
  --backend-config connector/configs/backend_profiles.example.yaml \
  --role-config connector/configs/roles.example.yaml \
  --host 127.0.0.1 --port 8765
```

Open http://127.0.0.1:8765/. The page has two modes:

- **Explore saved results** opens runs already saved under `connector/runs/`, by
  recent-run list or by run ID. Nothing runs.
- **Evaluate my commentary** submits new text. Under **Models for this run**, choose
  a backend profile for each step.

The **Guide** panel (the ⓘ buttons, or the `?` key) explains every section of the
page. The dashboard accepts connections from this machine only. Ctrl+C stops the
foreground server; it does not cancel an external CLI that is already running.

## Configure the model CLI

Install and authenticate your own headless model CLI, then configure a backend profile. For example, copy `connector/configs/backend_profiles.example.yaml` to `connector/configs/backend_profiles.local.yaml`, change its command and model settings, and launch with `--backend-config connector/configs/backend_profiles.local.yaml`. The pipeline supplies the role prompt; no separate manual prompt is required.

Profiles specify the command, prompt delivery (`stdin`, `prompt_file_path` or `instruction_arg`), timeout, and output handling. Prefer `stdin` or `prompt_file_path`: a command-line argument is visible to other users of the machine and is length-limited by the operating system, and the builder's prompt contains your full commentary. With `output_from: stdout`, the adapter saves the CLI's stdout. Otherwise, the CLI must write the expected output file itself. The CLI needs access to referenced local context files and must run without interactive permission questions. The included CLI examples are configurations; their flags may not be supported by every installed CLI version.

There are two active roles: `observation_packet_builder` and `market_book_evaluator`. Profile selection follows this precedence: explicit role override, the role file's `backend_profile`, then the `--backend` fallback. The fallback name must exist in the backend profile file. The supplied `roles.example.yaml` sets both roles to `mock`. In the dashboard, choosing a profile for a step overrides the roles file; choosing **Use my roles file** for a step uses the roles file's pin, and the page warns you when a step will run on `mock`. You may use the same profile and model for both roles or choose different ones; no model separation or provider exclusion is enforced.

The supplied live profiles allow up to three CLI attempts per role and 1,800 seconds per attempt. Invalid output, CLI errors or timeouts, and missing or inconsistent runtime data can stop a run. Validators also reject specified trading-directive language in generated fields.

## Run the supplied mock example

This offline example uses the supplied golden outputs:

```bash
.venv/bin/python dashboard/connector_dashboard.py run \
  connector/examples/golden_inputs/risk_on_breadth_weak.md \
  --backend mock \
  --backend-config connector/configs/backend_profiles.example.yaml \
  --role-config connector/configs/roles.example.yaml \
  --builder-backend mock --evaluator-backend mock \
  --out connector/runs/example_mock
```

Run output is written under `connector/runs/<run_id>/`, with builder, retrieval, evaluator, validation, signal context, and dashboard subdirectories. Runs stay local and are untracked.

For a live submission using one configured profile for both roles, use a fresh output directory each time:

```bash
.venv/bin/python dashboard/connector_dashboard.py run my_commentary.md \
  --backend codex_exec \
  --backend-config connector/configs/backend_profiles.example.yaml \
  --role-config connector/configs/roles.example.yaml \
  --builder-backend codex_exec --evaluator-backend codex_exec \
  --out connector/runs/my_first_live_run
```

This command is illustrative; configure the CLI and model first. Substitute your profile names or use different profiles for the two roles.

### Re-derive a run's shortlist

Any run's candidate shortlist can be recomputed from its recorded observation packet, with the same runtime exports as above:

```bash
.venv/bin/python -m connector.tools.retrieve_candidate_entries \
  --packet connector/runs/<run_id>/builder/market_observation_packet.yaml \
  --out-dir /tmp/rederived_<run_id>
```

Compare `/tmp/rederived_<run_id>/candidate_entries.json` with the run's `retrieval/candidate_entries.json`: the candidates and their order should be identical, and scores should agree to about six decimal places.

## Evaluator isolation

The evaluator's *prompt* does not contain your original commentary. Its *command-line tool*, however, runs inside the run folder, where the original text is saved as `raw_input.md`, and a tool that can browse files may open it or other files in this repository. This project does not ship a sandbox, because the right one depends on your operating system and your CLI. If you want the separation enforced rather than only designed in, these are the approaches worth considering:

- **Linux: Landlock.** A kernel feature (5.13 and later) that restricts which paths a process may read and write. The [`landlock`](https://pypi.org/project/landlock/) Python package can apply a ruleset to the CLI process just before it starts. Write the rules as an *allow-list* (system folders, the CLI's own install and login folders, the evaluator's inputs) rather than blocking only this repository; otherwise the CLI can still read everything else on your disk.
- **macOS: Seatbelt (`sandbox-exec`).** Built into macOS. Its profiles can deny reads of specific folders, including this repository, while re-allowing the files the evaluator needs. Apple marks `sandbox-exec` as deprecated, but it still ships and is used by several CLI tools.
- **Windows: WSL2.** There is no simple built-in equivalent that installs with `pip`; running the project inside WSL2 lets you use the Linux approach.
- **A separate working folder.** Give the evaluator a folder that contains only its inputs (candidates, schema and packet, with the raw-input path removed) and run its CLI there. This does not stop a tool from browsing elsewhere, but it removes the original text from where the tool starts.

Two practical points apply to any sandbox:

- **Logins.** A CLI logged in with a subscription account keeps its credentials in your home folder. A sandbox that hides the home folder must give the CLI a copy of its login files and copy refreshed tokens back afterwards, or the CLI will ask you to log in on every run. A CLI authenticated with an API key in an environment variable avoids this.
- **Test it.** Check that the sandboxed CLI can read its inputs and cannot read `raw_input.md` before relying on it.

## Installation verification

On 2026-09-23, the setup above was checked in an isolated copy starting with no Python environment or model cache, on Linux x86_64 with Python 3.10.12. The dependency installation and `pip check` passed. All nine E5 files were downloaded at the pinned revision, and the three manifest hashes matched.

The offline mock run finished with overall status `ok`; both resolved roles used `mock`; observation, evaluation, and signal-context validation were true; and the market-state context and dashboard report parsed as JSON. The dashboard started successfully and returned HTTP 200 for its page, CSS, JavaScript, profile list, examples, run list, and mock-run data. The test server was then stopped.

No live LLM run was part of this installation check; users must configure and check their chosen CLI.

## Sources and errata

The catalogue summarises published research; the papers are credited in [SOURCES.md](SOURCES.md). Known errors in the catalogue text, including author credits, are listed in [connector/catalog/CATALOGUE_ERRATA.md](connector/catalog/CATALOGUE_ERRATA.md).

## Blind-batch study

Fifty commentaries written by GLM-5.3-flash were run with gpt-5.6-sol as both
builder and evaluator. The connector returned 24 matched and 26 no-match
outcomes. Kimi K3 scored the existing answers: 24 cases scored 2.00–2.99 and
26 scored 4.50–5.00, with a mean final score of 3.74. The study's scope and
procedural qualifications apply to these results.

See the [blind-batch study](study/STUDY.md) for the commentaries, rubric,
per-case results, GLM review, repeatability probe, and recorded limitations.

## License

- **Code and documentation:** [MIT](LICENSE), unless noted otherwise.
- **Research catalogue:** [CC BY 4.0](connector/catalog/LICENSE). This covers
  the catalogue JSON, the text in the search index, the retrieval-target
  descriptions in `connector/configs/retrieval_targets.v1.yaml`, and
  `connector/catalog/CATALOGUE_ERRATA.md`. Credit: "Market Book catalogue by
  Egemen Candir, licensed under CC BY 4.0."
- **Not covered:** the research papers the catalogue cites. They belong to their
  authors and publishers and are credited in [SOURCES.md](SOURCES.md).
- **Dependencies and the embedding model** keep their own licences; see
  [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

This is research context, not investment advice.
