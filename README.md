# Ion

Ion is a local terminal coding agent. Give it a text task to inspect a repository, apply guarded edits, run checks, and report changes with verification evidence. It supports repository questions, public GitHub issue import, symbol search, web research, infrastructure inspection, and installed project linters.

The implementation uses Python and Textual with text-only, OpenAI-compatible model connections. See [architecture.md](architecture.md) for the runtime design, decisions, and implementation limits.




https://github.com/user-attachments/assets/915bf13b-d68b-4228-877a-b5a339c753fe



## Why use Ion?

- Work in your existing repository from the terminal, with task steering and visible tool activity.
- Apply edits against observed source and hashes to catch stale writes.
- Review changed files alongside retained command output and verification status.
- Bound model requests and token allowances for small coding tasks.
- Choose an explicit provider connection and keep run history and repository memory locally.

## Getting started

Use an interactive terminal on macOS or Linux, with Git, `make`, and `python3` available. The application requires Python 3.12 or 3.13; setup uses uv to resolve the runtime and locked dependencies. Setup needs network access and bootstraps a project-local uv if necessary.

Clone the repository, then install and launch:

```sh
git clone https://github.com/divyanshu-114/ion.git
cd ion
export AI_API_KEY="<PROVIDED_API_KEY>"
make setup
make run
```

This is the evaluation launch flow required by [RULE.md](RULE.md). The evaluator needs no additional routing variables, login, or configuration edits. Ion starts with DeepSeek Flash. For a Qwen key, open `/model` and choose Qwen; the same exported key is used without re-entry.

Normal product launches start with **No model selected**. Use `/models` to choose a configured model, or `/connect` to connect a provider. Until you choose, Ion keeps drafted tasks and blocks task execution, GitHub import, and provider diagnostics.

Evaluation offers only the submitted profiles: `deepseek-direct` (`deepseek-flash`) and `qwen-direct` (`qwen-plus`). `/model`, `/models`, `/providers`, and `/connect` open the interactive evaluation picker. Model selection is blocked while a task runs. Use `/doctor` to check the selected connection. To start product mode, leave `AI_API_KEY` unset in the shell, launch Ion, and use `/connect` to select a provider and enter its key.

### Run from any repository

Install the `ion` command once from the Ion checkout:

```sh
make install
```

Then launch it from the repository you want to work on:

```sh
cd /path/to/your/repository
ion
```

`make install` runs setup and installs Ion as an editable uv tool in a separate environment. Keep the Ion checkout in place: source and configuration changes take effect there; rerun `make install` after dependency changes or moving the checkout. If uv reports that its executable directory is missing from `PATH`, run `uv tool update-shell` and restart your terminal. If setup bootstrapped uv locally, use `.uv-tools/bootstrap/bin/uv tool update-shell` from the Ion checkout instead.

Regular tasks use the launch directory. GitHub issue tasks use a separate cloned checkout, shown by `/repo` and in the sidebar. `ION_REPO` does not change the launch directory. Plain `make run` still launches Ion from its own checkout for evaluation. In another repository, `make run` belongs to that repository's Makefile. Without installing the global command, you can still use `make -f /absolute/path/to/ion/Makefile run` from the target repository.

## Using Ion

Enter a specific task, such as `Fix the date parser in src/parser.py and run its tests`, or ask `How does authentication work in this repo?`. Recognized repository questions use read-only tools. Messages submitted during a run steer the next model turn. Each new submission starts a foreground task.

| Command | Purpose |
| --- | --- |
| `/help` | Browse commands |
| `/model`, `/models` | Select an evaluation profile, or a configured/discovered product model |
| `/providers`, `/connect` | Choose an evaluation provider; in product mode, inspect providers or enter a key |
| `/doctor` | Diagnose credentials, endpoint, model, and tool support |
| `/github URL` | Start a task from a public GitHub issue |
| `/steer TEXT` | Add instructions to the running task |
| `/stop` | Cancel work while keeping Ion open |
| `/history`, `/sessions` | Browse saved tasks |
| `/inspect TASK_ID` | Inspect a saved result |
| `/resume TASK_ID` | Check unresolved operations and prepare the task for re-submission |
| `/logs` | View recent requests, tool activity, and failures |
| `/new`, `/sidebar` | Clear the task view or toggle details |
| `/quit` | Exit; Ctrl+C also stops work and exits |

Enter submits; Shift+Enter or Ctrl+J inserts a newline. `/resume` does not replay edits or restore the previous model conversation.

Paste a public GitHub issue URL directly, include it in your task, or use `/github https://github.com/OWNER/REPO/issues/NUMBER`. Ion acknowledges the submission immediately, shows clone progress and elapsed time, then fetches the issue and first 100 comments. The fresh shallow checkout is stored under its data directory (`workspaces/`). Press Esc or use `/stop` to cancel preparation; text drafted while waiting is preserved. The agent investigates, edits, and runs relevant checks in that checkout. The full issue context is retained as a searchable artifact, with a bounded excerpt sent to the model. The checkout path appears in the transcript and sidebar; edits and patches remain available after completion. Each new issue URL submission creates a new checkout. Short follow-ups such as `solve`, `solve the issue`, `continue`, or `retry` reuse the current issue checkout and retained evidence. `/new` or a standalone regular task clears that issue context. After restarting Ion, paste the issue URL again; a bare issue reference without context prompts for details without calling the model. Regular tasks continue to use the launch repository. Issue solving does not post comments, create pull requests, or push changes.

The agent chooses tools as needed. `trace_symbol` finds case-sensitive occurrences using syntax patterns; `infra_scan` inspects manifests and declared ports; `web_search` returns bounded DuckDuckGo snippets. `run_linter` uses an installed project linter and retains diagnostics without installing dependencies. You can select a lint command in the active Ion configuration:

```toml
[tools]
lint_command = ["make", "check-style"]
```

Edits require observed source and current hashes. Whole-file rewrites require complete reads. Relevant passing checks can produce a `verified` result; an applied diff or clean lint alone does not establish behavioral correctness. Other outcomes include `unverified`, `blocked`, `budget_exhausted`, `failed`, and `cancelled`. If a provider rate limit interrupts a run, Ion returns an unverified incomplete result, preserves the current edits and patch artifact, and gives resume guidance instead of discarding the work.

## Configuration

Ion loads [ion.toml](ion.toml) from its checkout by default. Set `ION_CONFIG` to select another configuration. Ion does not load `.env` files or support `ION_ENV_FILE`; no `.env.example` is needed.

**Hackathon:** export `AI_API_KEY` in the same terminal that runs `make run`. This activates evaluation and reads only that credential. DeepSeek Flash is selected initially. For a Qwen key, choose Qwen through `/model`; no extra environment variables or key entry are needed. The variable name is `AI_API_KEY`, not `AI_APIA_KEY`.

**Product use:** start without an exported `AI_API_KEY`, open `/connect`, choose the provider that issued your key, and enter the key in the masked dialog. Ion keeps it in the current process and validates the provider connection. It never writes the key to `.env` or another credential file. Use `/models` to change models and `/doctor` to inspect the selected connection.

Profiles specify the HTTPS endpoint, model, context and output limits, and either native tools or structured JSON actions. Evaluation ignores `AI_PROVIDER`, `AI_MODEL`, and `AI_BASE_URL` overrides and preserves the configured endpoint, model, and limits. Ion does not infer a provider from a key or try keys against other providers. The Qwen profile currently targets DashScope Beijing, so its key must belong to that region. Both submitted endpoints and model IDs are defined in `ion.toml`; they must match the committee's prescribed services.

Product launches use the task-progress workflow. Normal reads use a controller-selected 12,000-character page and advance by the returned `next_offset`; explicit smaller limits remain authoritative. Request, deadline, context, and exact-loop ceilings remain hard safety controls, but normal runs do not enter finalization merely because a usage percentage was reached. A repair prompt such as “find all bugs and solve it” remains an edit task while it investigates, so discovery and guarded mutation tools stay available until the model finishes or a concrete safety stop occurs. Economy mode is disabled by default and in the committed `ion.toml`; its bounded settings remain for compatibility and targeted tests. When explicitly enabled, it applies 12 model requests and 24,000 accounted tokens per task, with preferred output allowances of 512 tokens for inspection, 1,024 for edits, 4,096 for rewrites, and 512 each for verification and finalization. Token planning protects 256 tokens each for verification and finalization, while request admission remains task-driven instead of reserving the final 20% of turns. Profiles default to an estimated 6,000 input tokens per request, including tool schemas. These are admission estimates, not provider billing guarantees.

## Data and execution boundaries

Use Ion on repositories you trust. Commands execute on the host under your user account. Path guards, command checks, filtered environments, timeouts, and process cleanup reduce accidental damage; they do not isolate hostile repository code. There is no container or VM sandbox.

Tasks, selected source excerpts, instructions, and tool output can reach the selected model provider. Web search and GitHub import also make external requests. Ion retains local task history, operation records, output artifacts, patches, and repository memory without encryption at rest. Redaction cannot identify every secret.

Data lives under `ION_DATA_DIR`, or `$XDG_DATA_HOME/ion`, defaulting to `~/.local/share/ion`. Diagnostics are in `logs/ion.jsonl`; `/logs` shows the file location. Diagnostic events exclude prompts, source bodies, and tool arguments, while task artifacts and history can contain repository content. `make clean` removes known disposable caches, not saved runs and evidence.

## Development and evaluation

```sh
make test
make clean
```

`make test` runs the offline pytest suite. The [tests](tests) cover configuration, guarded tools, context and budgets, recovery, verification, storage, and the TUI. [evals](evals) contains a case manifest and grading helpers; [evals/swebench.py](evals/swebench.py) validates the official three-field prediction shape and runs trusted local argv-based smoke checks. This is SWE-bench-compatible plumbing, not the official Docker evaluator: use the [SWE-bench evaluation guide](https://github.com/SWE-bench/SWE-bench/blob/main/docs/guides/evaluation.md) and its [harness reference](https://github.com/SWE-bench/SWE-bench/blob/main/docs/reference/harness.md) for dataset evaluation. `scripts/smoke_economy.py` provides an optional live check against a temporary README copy and consumes provider quota.

The hackathon launch contract in [RULE.md](RULE.md) is:

```sh
export AI_API_KEY="<provided key>"
make setup
make run
```

The submission team owns provider and model configuration in `ion.toml`; the evaluator only supplies the credential. `RULE.md` does not name a provider or model. If the committee prescribes one, the team must configure that exact text-only model and endpoint before submission. The current `evaluation_profiles` allowlist contains DeepSeek Flash and Qwen Plus, with DeepSeek selected initially. The evaluator explicitly chooses Qwen through `/model` when using a Qwen key. A bare key cannot reliably identify its issuer, so Ion never probes other providers automatically. Validation with the actual evaluation credential and destination is still required.

Source code lives in [src/ion](src/ion); [architecture.md](architecture.md) maps the components and records current design tradeoffs. Keep these two documents aligned with behavior when changing the code.

## Getting help

Use `/help` for commands, `/doctor` for connection diagnostics, and `/logs` for recent failures. Consult [architecture.md](architecture.md) for execution and recovery behavior and [ion.toml](ion.toml) for configuration examples.

Report bugs and propose improvements through [GitHub issues](https://github.com/divyanshu-114/ion/issues). Include the task, expected and actual behavior, Python version, provider/model, and relevant redacted diagnostics. Remove credentials and private repository content before sharing logs or artifacts.

## Maintainers and contributions

[Divyanshu Raj](https://github.com/divyanshu-114) maintains the repository. Contributions are welcome through pull requests; discuss substantial behavior or architecture changes in an issue first.

Run `make setup`, make a focused change, and run `make test`. Add regression coverage for behavior changes and update this README or [architecture.md](architecture.md) when the change affects usage or design. In the pull request, explain the problem, resulting behavior, and checks performed. Keep credentials, local data, and generated artifacts out of commits.

The package version is **0.1.0**, as declared in [pyproject.toml](pyproject.toml). The repository currently has no license file or declared package license.
