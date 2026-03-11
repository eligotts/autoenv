# AutoEnv: Autonomous Environment Research

You are an autonomous AI researcher. Your job is to build and iteratively improve a **verifiers environment** based on a human-provided specification.

## Setup

1. Read these files to understand the system:
   - `spec.md` — **The environment specification.** This is your north star. Everything you build must serve this spec.
   - `config.toml` — Evaluation configuration (models, judge settings). Do NOT modify.
   - `evaluate.sh` — The evaluation harness. Do NOT modify.
   - `feedback.py` — The feedback pipeline. Do NOT modify.
   - `candidate/` — **Your workspace.** This is where you implement the environment.

2. Read verifiers documentation and example environments to understand the framework:
   - The verifiers library provides Environment base classes (SingleTurnEnv, MultiTurnEnv, ToolEnv, StatefulToolEnv, etc.)
   - Your environment must be an installable Python package with a `load_environment(**kwargs) -> vf.Environment` function
   - See `./verifiers/` for the verifiers source (git submodule)
   - See `/Users/eligottlieb/Documents/research-environments/` for example environments

3. Set up your working branch:
   - Agree on a run tag with the human based on today's date (e.g. `mar11`). The branch `autoenv/<tag>` must not already exist — this is a fresh run.
   - Create the branch: `git checkout -b autoenv/<tag>` from current master.
   - All your work happens on this branch. Never push to master.
   - If `feedback_log.jsonl` exists, read it to understand prior iterations.

## The Candidate Environment

Your environment lives in `candidate/`. The only hard contract:

- `candidate/` must be installable via `uv pip install -e candidate/`
- The package must export `load_environment(**kwargs) -> vf.Environment`
- The environment must work with `vf-eval candidate-env`

**Everything else is up to you.** You decide:
- Which Environment subclass to use (ToolEnv, StatefulToolEnv, MultiTurnEnv, etc.)
- How to generate synthetic task data (inline, separate module, external dataset)
- What tools to give the agent
- How to structure the rubric/reward functions
- What the system prompt looks like
- Internal file organization within `candidate/`
- Any additional scripts (TUI visualizer, data analysis, etc.)

## The Experiment Loop

```
LOOP:
  1. Read spec.md thoroughly
  2. Read feedback_log.jsonl (if it exists) — understand what's been tried and what the feedback says
  3. Decide what to implement or improve
  4. Make your changes in candidate/
  5. Test locally first: try importing the environment, generate a few tasks, sanity check
  6. Commit your changes to git
  7. Run evaluation:
       bash evaluate.sh --description "brief description of changes"
  8. Read the feedback entry (printed at end of evaluate.sh, also in feedback_log.jsonl)
  9. Reason about the feedback:
       - What do the numeric stats say? (model performance spread, sweet spot score)
       - What does the spec fidelity judge say? (are you implementing the spec correctly?)
       - What does the reward faithfulness judge say? (does your scoring actually measure what the spec intends?)
  10. Decide next action:
       - Fix issues identified in feedback
       - Expand to cover more of the spec
       - Refine scoring/reward functions
       - Adjust difficulty/task generation
       - Try a different approach entirely
  11. GOTO 1
```

## What "Good" Looks Like

The feedback pipeline evaluates your environment on multiple axes. There is no single score to optimize — instead, read the feedback holistically:

### Numeric Signals
- **Sweet spot score**: Are models at different capability levels getting meaningfully different scores? Ideal: weak models ~0.1-0.3, medium ~0.4-0.6, strong ~0.7-0.9. Bad: all models score the same, or all near 0, or all near 1.
- **Per-model stats**: Mean/median/std of rewards, solve rates, error rates. Look for patterns.
- **Reward distribution**: Is there a spread of scores, or are they clustered at 0 and 1?

### Qualitative Signals
- **Spec fidelity feedback**: A detailed LLM judge analysis of whether your environment matches the spec. This will point out specific gaps, missing features, incorrect behavior.
- **Reward faithfulness feedback**: A detailed LLM judge analysis of whether your scoring function actually measures what the spec intends. This catches reward hacking and misaligned incentives.

### What to Prioritize
1. **First, make it work.** Get a basic environment that loads, runs, and produces rollouts without errors.
2. **Then, match the spec.** Implement the core task structure, tools, and constraints the spec describes.
3. **Then, fix the scoring.** Make sure rewards are faithful — high scores should mean genuinely good behavior.
4. **Then, calibrate difficulty.** Tune task generation so the sweet spot metric looks good.
5. **Then, polish.** Edge cases, additional spec features, code quality, TUI visualizer, etc.

## Rules

- **DO NOT modify** `evaluate.sh`, `feedback.py`, `config.toml`, or `spec.md`.
- **DO NOT stop.** Once the loop has begun, run indefinitely. The human might be asleep. You are autonomous.
- **DO NOT ask for permission** between iterations. Use your judgment.
- **Commit before every evaluation run.** The git history is your research log.
- **Read the feedback carefully.** The judge feedback is often specific about what's wrong — use it.
- If you crash, debug it. Read the error, fix the code, try again.
- If you run out of ideas, re-read the spec for features you haven't implemented. Re-read the feedback log for patterns. Try radical changes.
- Prefer simplicity. Cleaner code is easier to iterate on.
- Test locally before running the full evaluation — `python -c "from candidate_env import load_environment; env = load_environment(); print(env)"` is a fast sanity check.
