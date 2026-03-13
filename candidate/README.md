# On-Call Engineer Environment

A verifiers StatefulToolEnv for training agents to act as on-call engineers. The agent triages, diagnoses, and remediates infrastructure incidents across a procedurally generated microservices architecture using 14 observability and operational tools.

## Features

- 1000 procedurally generated incident scenarios across 10 fault types and 3 difficulty tiers
- 14 tools: alerts, logs, metrics, traces, topology, service status, deploys, config, run commands, runbooks, incident channel, status updates, paging, and resolve
- Multi-dimensional reward: diagnosis (30%), remediation (30%), efficiency (15%), communication (10%), collateral avoidance (10%), escalation (5%)
- Simulated time budget (60 min) with per-tool time costs

## Usage

```python
from candidate_env import load_environment

env = load_environment(num_tasks=1000)
```
