# AutoEnv

AutoEnv is an autonomous research loop that uses an AI coding agent (Claude Code) to iteratively build and improve RL training environments from a human-written specification.

You write a spec describing the environment you want. The agent builds it, evaluates it against a target model, reads quantitative and qualitative feedback, and iterates — indefinitely, without human intervention.

Inspired by Karpathy's [autoresearch](https://github.com/karpathy/autoresearch) concept.

## How it works

```
Human writes spec.md
        │
        ▼
┌─────────────────────────────────────────────┐
│  Agent reads spec + prior feedback           │
│  Agent implements/improves candidate/        │
│  Agent commits changes                       │
│                                              │
│  evaluate.sh runs:                           │
│    1. Install candidate environment          │
│    2. Smoke test (1 task, 1 rollout)         │
│    3. Full eval (N tasks × M rollouts)       │
│    4. Compute numeric stats + verdict        │
│    5. Launch Claude Code judge on rollouts   │
│                                              │
│  Agent reads feedback:                       │
│    - Numeric: mean reward, solve rate, RL    │
│      readiness, keep/discard verdict         │
│    - Qualitative: spec fidelity, reward      │
│      faithfulness, prioritized issues        │
│                                              │
│  Agent decides what to fix/improve next      │
│  ─────────────────── LOOP ──────────────────▶│
└─────────────────────────────────────────────┘
```

Each iteration is a git commit on a per-run branch. The feedback log and judge analysis are keyed by commit hash, so you can trace every decision.

## Repo structure

```
autoenv/
├── program.md          # Agent instructions (the "system prompt")
├── evaluate.sh         # Evaluation harness (fixed)
├── feedback.py         # Numeric stats pipeline (fixed)
├── config.toml         # Target model + eval budget (fixed)
├── setup.sh            # Venv setup
├── candidate/          # Minimal skeleton — agent builds here
│   ├── pyproject.toml
│   └── candidate_env/
│       ├── __init__.py
│       └── env.py      # load_environment() stub
└── verifiers/          # Git submodule — the verifiers framework
```

**Not on main (generated per-run):**
- `spec.md` — your environment specification (added on branch)
- `feedback_log.jsonl` — numeric stats per iteration
- `feedback/<commit>.md` — qualitative judge analysis per iteration

## Usage

### Prerequisites

- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) CLI installed
- [uv](https://github.com/astral-sh/uv) for Python package management
- [Prime](https://github.com/PRIME-RL/PRIME) for inference (`prime eval`)
- A `verifiers` endpoints config pointing to your inference provider

### Setup

```bash
git clone https://github.com/eligotts/autoenv.git
cd autoenv
git submodule update --init
bash setup.sh
```

### Running a new environment

```bash
# 1. Create a branch for this run
git checkout -b autoenv/<tag> main

# 2. Write your environment spec
vim spec.md

# 3. (Optional) Edit config.toml to set your target model

# 4. Launch the autonomous agent
claude --dangerously-skip-permissions
# Then: "Read program.md and begin. Run tag is <tag>."
```

The agent will loop indefinitely — building, evaluating, reading feedback, and improving. Each iteration is a commit. You can walk away and come back to a git log full of experiments.

### Watching progress

```bash
# Iteration history
git log --oneline autoenv/<tag>

# Latest numeric stats
tail -1 feedback_log.jsonl | python3 -m json.tool

# Latest judge feedback
ls feedback/   # files named by commit hash
```

## The feedback loop

### Numeric signals

The evaluation pipeline computes per-iteration metrics against the target training model:

- **Mean reward** — target range 0.2-0.7 for RL (enough signal to learn, room to improve)
- **Reward variance** — std > 0.1 (model sees varied outcomes)
- **Solve rate** — 10-80% (not all-or-nothing)
- **Keep/discard verdict** — soft signal comparing current metrics to the previous iteration

### Qualitative signals

A separate Claude Code instance acts as a judge, reading the actual rollout transcripts and analyzing:

- **Spec fidelity** — does the environment match the spec? What's missing or incorrect?
- **Reward faithfulness** — do high scores mean genuinely good behavior? Can the model hack the reward?
- **Prioritized issues** — concrete, actionable list of what to fix next

## Writing a good spec

The spec is the most important input. It should describe:

- What the task is (scheduling, coding, negotiation, etc.)
- What tools the agent has access to
- What constraints or rules govern the task
- How scoring/rewards should work
- What difficulty levels look like
- What a "good" solution looks like vs. a "bad" one

The more specific and concrete the spec, the better the agent can implement and the better the judge can evaluate.

## Design decisions

- **Fixed eval budget**: Every iteration runs the same number of examples and rollouts, making metrics comparable across iterations.
- **Commit-per-iteration**: The git history is the research log. No work is lost.
- **Keep/discard verdict**: A soft signal, not a command. The agent uses judgment — a "discard" with insightful judge feedback may still be worth keeping.
- **Separate judge instance**: The judge runs as an isolated Claude Code process that can browse rollout files directly, rather than receiving pre-formatted summaries. This gives it full context.
- **Smoke test circuit breaker**: A 1-task, 1-rollout test runs before the full eval. If the environment is broken, it fails fast instead of wasting a full eval budget.
