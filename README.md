# Ion



Ion is a terminal coding harness for the AI Harness Hackathon 2026. It reads text tasks, inspects local repositories, applies guarded edits, runs bounded commands, and reports changed files with verification evidence.

## Start

Requires a terminal and Python 3.12 or newer. From your cloned Ion repository:

```sh
export AI_API_KEY="<your key>"
make setup
make run
```

The default development profile is `openrouter-coding-free` (Cohere North Mini Code via OpenRouter), which completed a live small bug-fix test. Launch `ion` from the repository you want to edit; that directory becomes the immutable workspace for the process. `/doctor` checks the active credential, endpoint, model, tool support, live limits, and OpenRouter free quota. `/logs` shows the recent internal request, tool, and budget sequence. `/models` lists profiles and, when a key is available, discovers selectable provider models. `/history` shows saved runs; `/inspect TASK_ID` shows their details. `/steer TEXT` adds an instruction to a running task at its next model turn. `make test` runs the offline suite. `make clean` removes disposable build and test caches.

The clone can live anywhere. `make run` uses the directory it runs in as the workspace. To work on a separate repository with the same Ion checkout, run these commands from that target repository:

```sh
make -f /path/to/ion/Makefile setup
make -f /path/to/ion/Makefile run
```

Ion's dependencies stay in its checkout; its file tools and repository commands operate in the launch directory. Plain `make setup` and `make run` need Ion's Makefile in the current directory. Avoid `make -C /path/to/ion run` when targeting another repository, because `-C` changes the working directory to Ion's checkout.

## Terminal interface

Ion uses a compact workbench layout built around a task dock, session timeline, and run-state rail. The interface is designed for JetBrains Mono; select that font in your terminal profile before launching Ion. Textual inherits the terminal emulator's active font and cannot replace it from application CSS.

| Action | Shortcut / command |
| --- | --- |
| Send task / steer a running task | Enter |
| Insert newline | Shift+Enter or Ctrl+J |
| List commands | `/help` |
| Choose model | `/models` |
| View providers and masked keys | `/providers` |
| Show locked workspace | Sidebar or bottom status bar |
| Connect a configured provider | `/connect` |
| Inspect saved tasks | `/sessions` or `/history` |
| New task view | `/new` |
| Toggle context sidebar | `/sidebar` |
| Cancel active task | `/stop` |
| View recent activity | `/logs` |
| Quit | Ctrl+C or `/quit` |

The sidebar hides automatically in narrow terminals. Session transcripts and highlighted diffs reflow when the terminal is resized. Sessions are journaled for inspection and `/resume TASK_ID` performs recovery checks before preparing a safe re-submission; it never replays an old mutation. Each submitted task starts a foreground engine run; messages sent while it is running are steering instructions.

`/connect` offers the providers configured in `ion.toml`. Its masked input keeps the key in the current process only and does not write credentials to disk. Catalog access or the next request checks whether the key works. Existing environment configuration remains available. OAuth and additional provider protocols are separate work; matching the dialog layout does not add those capabilities.

Provider views show configured credentials as `***`; key entry uses asterisks. `/logs` shows timestamped actions, model requests, failures, retries, and outcomes in plain language. Detailed structured events remain in the diagnostic file shown below the log. Ctrl+C stops active work and exits, including from dialogs; `/stop` keeps Ion open.

## Choose a provider

The committed [ion.toml](ion.toml) contains Groq Qwen, OpenRouter coding, Qwen, and free-router profiles, plus direct DeepSeek and Qwen API examples. Export the matching key:

| Profile | Credential |
| --- | --- |
| `groq-qwen-dev` | `GROQ_API_KEY` |
| `openrouter-coding-free`, `openrouter-qwen-free`, `openrouter-free-router` | `OPENROUTER_API_KEY` |
| `deepseek-direct` | `DEEPSEEK_API_KEY` |
| `qwen-direct` | `DASHSCOPE_API_KEY` |

The direct provider APIs may require a paid account. A `:free` OpenRouter model is subject to provider availability and rate limits. Catalog availability is checked live; these profiles are examples, not a promise that every model remains free or available.

For local use, copy `.env.example` to `.env` beside the active `ion.toml` and fill in the key for the provider you selected. Ion loads that file without overriding environment variables already set by the shell. Set `ION_ENV_FILE` to use a different local file. An externally exported `AI_API_KEY` automatically activates locked evaluation, skips dotenv, and takes precedence over provider-specific credentials.

For optional developer configuration, set `AI_API_KEY` and `AI_PROVIDER`. These additional settings are not part of the evaluator's required flow. Supported shortcuts are `deepseek`, `qwen`, `groq`, `openrouter`, and `openai`. DeepSeek defaults to `deepseek-flash`; Qwen defaults to `qwen-plus`. Set `AI_MODEL` to override either default; the other shortcuts require `AI_MODEL`. Set `AI_BASE_URL` to override the endpoint, including the region for your Qwen key. Any other OpenAI-compatible service can use `AI_BASE_URL` plus `AI_MODEL`, with an optional custom `AI_PROVIDER` label. These explicit routing settings select a new startup profile that reads only `AI_API_KEY`, so an old provider-specific key cannot take precedence. A key alone does not identify its issuer; Ion never probes other providers with your key.

Environment connections use native tool calling and a conservative 8,192-token context budget. For custom context limits or structured JSON tools, create a TOML file with `schema_version = 1`, `default_profile`, and a `[profiles.NAME]` section containing `provider`, `base_url` (HTTPS), `model`, `api_key_env`, `protocol = "openai_chat"`, `tool_protocol = "native"` or `"structured_json"`, `text_only = true`, `locked = false`, `context_window`, and `max_output_tokens`. Launch with `ION_CONFIG=/path/to/your.toml make run` and leave the routing environment settings unset. Existing TOML profiles prefer their provider-specific key and fall back to `AI_API_KEY`. Never place key values in TOML or Git. Services requiring a different wire protocol need a separate adapter.

## Quick start usage

Ion follows an economy mode workflow for small tasks:

Type ordinary questions or edit requests into the composer. For example, “how should I use this repo?” asks Ion to inspect relevant files and explain usage. Recognized repository questions use read-only tools; model attempts to edit or execute commands are rejected. “Improve this repo's README usage section” requests an actual documentation edit. Read-only answers can finish as ordinary text after inspection; edit requests that stop at advice are redirected toward applying changes. Large required instructions that cannot fit the model context are reported as a context limit, separately from a spent request/token budget.

**Basic workflow:**
1. **Read** a file to inspect its contents
2. **Edit** with a brief plan and exact replacement
3. **Check** bounded repository commands verify changes
4. **Finish** with final diff and evidence

Tools available for repository operations:
- `repo_list`: List files in directories
- `repo_search`: Find literal text with path filtering
- `file_read`: Read bounded pages (up to 12,000 chars) with read IDs
- `edit_file`: Apply exact text replacements
- `write_file`: Create or rewrite complete files
- `command_start`: Run bounded repository checks
- `finish_request`: Complete tasks or report blockers

## Small tasks and token budget

Restart Ion after changing `.env` or the default profile. Start with a specific task naming the file and expected behavior. The currently running TUI retains its selected model until you change it through `/models` or restart.

Product runs default to economy mode: **read → brief plan + edit → bounded check → final diff and evidence**. The TUI enables eight tools, including the policy-checked command runner:

| Tool | Purpose |
| --- | --- |
| `repo_list` | List files within a directory, with pagination |
| `repo_search` | Find literal text with a path filter and source offsets |
| `file_read` | Read a bounded page and obtain a guarded read ID; optional `limit` up to 16,000 characters |
| `edit_file` | Apply an exact replacement in observed text |
| `write_file` | Create a file (including missing directories), or rewrite a fully read file without repeating old text |
| `diff_inspect` | Inspect accumulated changes when needed |
| `command_start` | Run one bounded repository check with a clean environment and retained output artifact |
| `finish_request` | Finish or report a blocker honestly |

Both edit tools require a short plan, displayed before execution. `done=true` ends a successful edit without another model request; use `done=false` to continue across files. Reads and searches stay available after edits. Existing files require current read IDs; whole-file replacement also requires reading all pages first. Hash checks reject stale edits and creation never overwrites an existing file. File deletion is unavailable. Product mode permits one bounded command at a time; destructive commands, shell chains, redirection, private files, and API-key environment variables are rejected.

For a task naming one existing file, Ion can inspect up to 12,000 characters locally before its first model request, provided the source fits the context budget. An explicit whole-file rewrite with complete observed source directs native tool selection to `write_file`. This removes model calls spent locating and rereading a known target. Larger files and tasks across files keep normal paginated inspection. If the provider rejects directed selection, Ion makes one attempt using automatic selection.

The workspace is captured from the launch process's current directory and cannot be changed through `/repo` or `ION_REPO`. File tools reject absolute paths, parent traversal, and symlink targets. Commands run in that workspace with a filtered environment and process-group cleanup; they cannot access files outside it through the harness.

Search supports an optional `relative_path` and returns character offsets for reading the surrounding code directly. A command only verifies a task when its output identifies a relevant passing check and the final workspace fingerprint is unchanged. Diff capture records what changed; it does not prove correctness.

Diagnostics are appended to `$ION_DATA_DIR/logs/ion.jsonl`, defaulting to `~/.local/share/ion/logs/ion.jsonl`, and rotate at 2 MB. They include phases, offered tool names, token accounting, safe path/offset metadata, tool status, retries, and outcomes. They exclude credentials, prompts, search text, source text, patch contents, and model tool arguments. Use `/logs` or `/debug` in the TUI to inspect the latest events.

Configure `[economy]` in `ion.toml`: `enabled = true`, `max_requests = 12`, `max_total_tokens = 24000`. Each request reserves estimated input plus its output cap; complete provider usage replaces that reservation when available. This is an estimated admission limit, not an exact tokenizer or provider billing ceiling. Failed requests and retries consume budget. One repair attempt is allowed for invalid actions/tool failures, and at most one consecutive rate-limit retry. Saved results include request counts, reported input/output tokens, and accounted tokens. Set `enabled = false` to restore the legacy tool workflow; locked evaluation mode uses that workflow independently.

The default coding profile allows up to 4,096 output tokens. Economy requests start at 1,024 tokens before reading and 2,048 afterward, bounded by the profile limit; explicit rewrites start at the full profile limit. A truncated response triggers one retry at the profile limit; partial tool calls are never executed. Provider usage is retained even when an action is truncated or malformed. Logs include the stop reason and reported reasoning-token count. Every profile defaults to an estimated 6,000 input tokens per request, including tool schemas; customize `input_budget_tokens` in trusted TOML if needed. Estimates use serialized character counts, not a model-specific tokenizer. Older complete tool turns are dropped together; pinned task instructions and the latest tool turn are retained. Oversized required context stops the task instead of silently dropping instructions.

To spend provider quota on an isolated live check, run `uv run python scripts/smoke_economy.py` from the Ion source checkout. It submits the exact task `rewrite the readme` against a temporary README copy, prints safe diagnostic events, and leaves the working repository untouched.

Ion keeps bounded working memory in each run's artifact directory and stores reusable, source-linked repository memory in its local data directory. Compacted prompts receive pointers instead of replaying old source/log output; stale file references are marked for rereading. Repeated tool cycles trigger a warning after three repetitions and stop after four without workspace progress. See [context management](docs/context-management.md) and [memory architecture](docs/memory-architecture.md) for the runtime contracts.

File reads return 4,000-character pages; economy prompts receive a short read ID instead of the full-file hash. Listing/search results and command feedback are bounded. Economy runs use the limits above; legacy runs have a 24-request ceiling including a verification reserve. Both have a ten-minute admission deadline. The activity view shows request counts and token usage. Free endpoints can still be rate limited; changing providers or models requires explicit selection.

File-read activity includes the path and character offset. Equivalent reads of the same unchanged page are counted even when the model varies its call arguments. The controller supplies the next-page offset and directs the model toward a patch; five reads of the same unchanged page stop the run. Older duplicate page bodies and obsolete file versions are replaced with metadata, while distinct pages remain available within the context budget.

Live smoke result (2026-09-27): the default model fixed subtraction to addition in a disposable Python repository via the TUI Run button, preserved all tests, and passed three tests. Ion returned `verified` with one verification record. That run used eight requests and reported 5,804 input / 698 output tokens. This establishes a small-task baseline, not general issue-solving reliability. The verification observer recognizes focused pytest, unittest, npm/pnpm/yarn, cargo, and go checks; documentation-only edits remain unverified unless a relevant executable check exists.

## Hackathon evaluation

The evaluator uses exactly this startup sequence:

```sh
export AI_API_KEY="your-provider-key"
make setup
make run
```

No additional environment variables, login, or evaluator configuration edits are required. The exported key automatically activates locked evaluation, skips local dotenv loading, and takes precedence over provider-specific keys. Ion uses the committed `evaluation_profile`, or locks `default_profile` when no evaluation profile is configured.

Before submission, the team must commit the committee's direct provider endpoint, exact model ID, and limits in `ion.toml`. Direct DeepSeek and Qwen credentials cannot be reliably distinguished from a bare key. The current development default is OpenRouter; it is not a universal destination for direct provider keys. Supporting either provider's key without knowing which provider issued it remains unresolved until the committee specifies the destination. The team must resolve this before submission rather than adding evaluator steps. Offline tests cover key-only launch and credential isolation; a live end-to-end run with the committee's exact endpoint, model, and key is still required.

Ion executes repository commands on the local machine. Use it on repositories you trust. Tool output, patches, and run history are retained under the user's Ion data directory. The foreground TUI records operation intent before dispatch, uses a durable workspace admission claim, exposes reconnectable session event primitives, and wires source-linked memory plus atomic compaction checkpoints into runs. Detached client-independent execution remains a future increment; current runs stay attached to the foreground TUI. See the [agile delivery guide](docs/agile.md) and [hackathon rules](docs/rules.md) for the remaining release gates.

See [the product documentation](docs/README.md), [hackathon rules](docs/rules.md), and [architecture](docs/architecture.md).
