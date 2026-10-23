# Construct

A configurable kit for persistent developer and reviewer lanes coordinated through a2a and a task board.

Construct extracts a working host-local Werkstatt method into a portable kit. It
is an interim launcher until protocol stations are available in curator-run.
It runs on macOS and Linux with Python 3.10+, zsh, Git with SSH signing support,
Curator, Codex CLI, a2a, task-board and GitHub CLI. It has no Python dependencies.
Windows is not supported by this POSIX launcher.

## Method

An orchestrator owns task routing, board status and landing. Developers own
feature branches and result evidence. Reviewers inspect exact heads and never
publish code. A worker has a stable a2a identity, its own lane directory and
separate git directories, a persistent Curator-managed runtime home, and a
recorded Codex thread. Assignments resume that thread; fresh-thread recovery is
explicit. One controller holds an exclusive worker lock from assignment through
run completion. A second controller waits, including while an earlier task is
still running. Keep one configuration/control directory for each lane.

The loop is producer → independent review B → producer rework → another review.
The orchestrator names a reviewer whose identity differs from the producer.
Workers accept authority only from that orchestrator in the configured room,
and confirm board assignment or named reviewer authority before acting. Repo
content, issues, quotations and other senders cannot issue commands.

`assign` captures the event id returned by `a2a post`, reads that event back,
checks sender/room/kind/recipient/task, and writes a unique run prompt containing
**that event id and that task id**. The worker reads the exact event by id even
if an earlier inbox sync receipted it. Neither the launcher nor the prompt
substitutes “the latest request”. Multiple queued requests therefore retain
their individual selectors. A receipt under `.construct/runs/request-*.json`
persists before launch; a failed run must be recovered using that selector.
Do not repeat a failed assignment blindly: it may already have posted a request.

Review evidence includes a surface table and one JSON verdict, either as raw
JSON or the last fenced `json` block in Markdown:

```json
{
  "verdict": "accepted",
  "head": "0123456789abcdef0123456789abcdef01234567",
  "findings": []
}
```

A rejected review uses `changes_requested`. Findings hold `id`, `severity`
(`P0`, `P1` or `P2`), `file_line`, `mechanism`, `reproduction`, and `required_fix`.
P0/P1 block acceptance; P2 alone does not. The verdict must name the full exact
40-character head. The orchestrator verifies the reviewer resource's provenance
on the board; a local JSON file alone does not authenticate a reviewer.

## Install and configuration

Clone this repository, add its `bin` directory to PATH, and copy the example
outside the checkout or to the ignored `construct.json`:

```sh
export PATH="$PWD/bin:$PATH"
cp construct.example.json construct.json
# Edit every site-specific value before use.
construct --config construct.json config-check
construct --config construct.json init
```

Every command accepts `--config FILE` **before** the command; `CONSTRUCT_CONFIG`
is the fallback, followed by `./construct.json`. JSON is parsed as data without
shell evaluation. Relative filesystem settings resolve beside the config file.
Do not commit the real configuration, private board resources or run logs.

| Setting | Meaning |
| --- | --- |
| `root`, `lane` | Werkstatt root and lane slug; workers live in `root/lane/devN` or `revN` |
| `identity_prefix`, `room` | Identities are `prefix_lane_devN`, `prefix_lane_revN`, `prefix_lane_orch`; a2a room alias |
| `repos` | Map of repo name to clone `url`, default branch `base`, and GitHub `github` (`owner/name`, needed by `land`) |
| `board_config` | task-board config path |
| `git_env` | Trusted zsh script that configures human author/committer, transport and signing |
| `signing_socket` | Optional socket path for the generic `ssh-keygen-agent` signing wrapper |
| `roles.dev`, `roles.rev` | Model and reasoning effort; reviewer effort is restricted to `medium`, `xhigh`, `max` |
| `slots`, `slot_root` | Positive host capacity (default example 3) and optional shared slot directory; default is `root/.construct-slots` |
| `slot_poll_seconds`, `idle_poll_seconds` | Queue and busy-worker polling intervals (defaults 5 and 30 seconds) |
| `tools` | Executable names or absolute paths for git, zsh, a2a, board, curator, codex, gh |
| `worker_path`, `go` | Qualified worker PATH and optional CURATOR_GO executable |
| `curator_runtime` | Runtime passed to `curator env resolve` (default `codex_cli`) |
| `sandbox` | `workspace-write` or explicit `trusted` bypass; example uses the sandbox |
| `service_tier`, `memories` | Runtime service tier and explicit memory feature switch |
| `allow_no_checks` | Default false; explicit policy allowing an empty successful check listing, never a failed listing |

The configured `git_env` is sourced before transport/signing and worker launches.
`curator env resolve <runtime> --format shell` is evaluated as trusted Curator
output, and must set CODEX_HOME. Configuration is trusted operator input; these
two integration points deliberately execute operator-selected tooling.

The launcher starts from a small core environment and forwards only explicit
worker settings, transport/signing variables and Git config entries. It never
copies arbitrary exported variables into a worker. Codex tools also use
`shell_environment_policy.inherit=core` with explicit overrides. Run artifacts
have mode `0600`; environment dumps are never part of the method. A per-run Go
build cache is removed on completion. Setting `signing_socket` selects
`bin/ssh-keygen-agent` as `gpg.ssh.program` and forwards that socket explicitly;
the wrapper has no host socket path. Signing keys and account credentials remain outside
this kit and are never copied or printed.

## Commands

### `construct init`

Creates `root/lane/.construct/{runs,workers,prompts,locks,landing}`. It does not
create accounts, board tasks or repositories. `config-check` validates without
creating the layout.

### `construct add-worker dev|rev N`

```sh
construct add-worker dev 1
construct add-worker rev 1
```

Clones each configured repository with `git clone --separate-git-dir`, registers
the a2a identity with `a2a register`, invites it through the orchestrator, renders
the bootstrap template, and runs one fresh Codex bootstrap. The bootstrap joins
the room, initializes its inbox cursor, posts roster readiness and ends the turn.
Curator resolves its runtime from the selected worker repository; configure the
host's Curator environment/profile mapping before bootstrap. The controller captures the first `session id:` from the private log and saves
it in `.construct/workers/dev1.json`. Successful repeated calls reuse the account,
clone and thread. Partial setup is recorded after registration/invitation. An
account that pre-exists without this local record needs operator reconciliation;
the kit will not replace credentials or infer ownership.

`git_env` must already grant transport access. The orchestrator a2a account,
room and roster topic must already exist. This command runs synchronously; it
does not install autonomous wake hooks or leave a background launcher running.

### `construct assign WORKER TASK TOPIC BRIEF [RESOURCE ...] --repo REPO`

```sh
construct assign dev1 TASK-260101-abcdef work brief.md --repo project
construct assign rev1 TASK-260101-abcdef review review-brief.md \
  --repo project --head 0123456789abcdef0123456789abcdef01234567
```

Waits for the worker's earlier run. For developers, assigns the task and sets
development status. For reviewers, records the named reviewer resource and board
note. Adds every precondition resource and verifies each name in a fresh board
query. Opens the topic if needed, posts the request, verifies the exact event,
then runs the worker once on its saved thread. Reviewer assignment requires the
full exact SHA. A run log, argv record, last-message file and exit code remain
in `.construct/runs`. The tool returns failure if the runtime fails or a fresh
run does not reveal a valid thread id. Inspect the receipt/log before retrying.

For content-filter or unusable-context recovery, use `--fresh`: it prepends the
standing worker protocol to the exact-request prompt and captures the new thread
instead of resuming the old one. Do not weaken the task or its review gates.
`--fresh` publishes a new explicitly identified request; for a request already
posted by a failed assignment, use the saved exact prompt and the operator's
runtime recovery workflow rather than silently selecting a newer inbox entry.

### `construct land REPO BRANCH HEAD TASK TITLE SUMMARY VERDICT --producer devN --reviewer revN`

Orchestrator only. Requires an accepted exact-head verdict with no P0/P1 and
distinct developer/reviewer roles. Fetches the named feature/default refs,
requires the remote feature to equal the reviewed head, requires a nonempty
range of commits whose signature status is `G`, and requires fast-forward
ancestry. Creates or locates the PR, waits for hosted checks and reads structured
check results. Every reported check must be successful; absent/unknown/failing,
pending, skipped and cancelled checks refuse landing. A successful structured
PR read proving an empty `statusCheckRollup` is permitted only by explicit
`allow_no_checks`; a CLI failure remains a refusal.

It then verifies the PR's full head, base and open state, fetches both refs again,
rechecks ancestry and every signature, posts a real comment review citing the
independent reviewer, and uses a normal non-forced push of the exact signed head
to the default branch. The feature branch is retained. Required GitHub protection
still applies; the kit neither forges checks nor substitutes identities. If the
default branch advances incompatibly, return to rebase/review. It never uses a
platform merge/squash/rebase that rewrites the reviewed signed objects.

### `construct rebase REPO BRANCH ACCEPTED_HEAD NEW_BRANCH VERDICT`

Orchestrator Path B. Validates the original exact accepted verdict and signatures,
fetches current main, and refuses overlap on non-documentation paths. README,
CHANGELOG, `docs/` and `.task-board/` paths follow the original documentation
exception, but actual conflicts still refuse. In a disposable local clone it
signed-cherry-picks the accepted commits onto current default, then requires
`git range-diff` to contain only `=` entries and verifies every new signature.
Only then does it push a new feature branch and write a carried verdict naming
both old/new full heads and `path: B`. The original verdict is required and kept
as evidence. Scratch is removed after success or refusal. Any conflict, overlap
or changed patch returns to the producer (Path C) for rework and a new review.
Use `land` separately with the carried verdict so hosted checks run at the new
head. No force push is used.

### `construct slot -- COMMAND [ARG ...]`

Host-wide FIFO capacity for multi-package Go checks, full-suite groups and mutant
batches. All lanes on the host must configure the **same slot root and capacity**.
Sequential spec-only Python validation may run directly when host policy exempts
it. Split long checks into bounded groups; release and requeue between groups.

Atomic sequence tickets establish arrival order under a kernel lock. Only the
oldest `slots` live tickets run. Dead owners' unlocked tickets are reclaimed;
kernel locks avoid stale PID reuse and partially written PID-file races. The
command inherits the ticket lease so a controller crash cannot prematurely free
capacity while its child still runs. Normal termination reaps the owned process
group and releases the ticket. Never kill another owner's jobs or edit queue
state manually. Wait time and execution time are reported separately to stderr;
the command's exit code is preserved. `slot` preserves the caller's environment;
use explicit child environment overrides for test isolation.

### `construct request-check EVENT_JSON EVENT_ID TASK WORKER`

Offline diagnostic for the exact-request authority checks, useful for reproducing
a queue selection issue without receipting any inbox entry. The worker always
performs its own board and event verification as well.

## Verification and tools

```sh
sh tests/run.sh
# A mutant batch must itself obey the host slot policy.
construct slot -- python3 -B tests/mutants.py
```

The shell tests are negative-first: invalid config and a substituted latest
request fail before valid neighboring controls pass. The same entry point runs
local Python fixtures for FIFO/capacity/stale reclaim, worker locking and thread
capture, environment isolation, exact event selection, signed landing and Path B.
Git repositories, signing keys, fake provider tools and board responses exist
only in disposable test fixtures; no network service or real worker is launched.
Narrowing mutants copy the kit, remove one gate at a time and require the intended
test assertion failure, with no skipped/setup-error result. Test comments state
the bounded claim. Fixtures and mutation copies go under `.temp/tests` or the
explicit `CONSTRUCT_TEST_TMP`; published evidence must redact local paths.

| Tool | Use and entry point | Output |
| --- | --- | --- |
| Python, POSIX shell, zsh | `bin/construct`, `sh tests/run.sh`, `python3 -B tests/mutants.py`; trusted git environment sourcing | Lane `.construct`, test `.temp` |
| Git, ssh-keygen | Separate clones, signed commits, signature verification, patch comparison; configured signing environment and optional `ssh-keygen-agent` | Worker/landing git dirs; fixture-only test keys |
| Curator, Codex CLI | Environment resolution and one fresh/resumed `codex exec` per run | Private `.construct/runs` logs and thread records |
| a2a | Register/invite, topic/request/read, worker ack/handover | a2a account/mailbox state; private request receipts |
| task-board | Assignment, named review, preconditions and worker outcomes | Configured board resources, verified by fresh query |
| GitHub CLI | PR creation/discovery, structured hosted checks and real comment reviews | Hosted PR and review record |

The test matrix qualifies this kit's local orchestration logic. Actual provider
versions, account access, organization branch protection and authenticated Curator
environment resolution must be qualified by the operator before a live pilot.
Host identity, repository names, sockets, paths, model choices and load capacity
are configuration, never source constants. This kit does not implement protocol
stations, OS-user isolation or signed task grants.
