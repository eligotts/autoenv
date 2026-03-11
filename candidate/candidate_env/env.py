"""
Candidate environment — the agent implements this.

The only hard contract:
  load_environment(**kwargs) -> vf.Environment

Everything else — file structure, data generation, environment type,
tool definitions, rubric functions — is up to you. Refer to spec.md
for what to build.
"""

import verifiers as vf


def load_environment(**kwargs) -> vf.Environment:
    """Load and return the candidate environment.

    This function is called by vf-eval. It must return a vf.Environment
    instance (any subclass: SingleTurnEnv, MultiTurnEnv, ToolEnv,
    StatefulToolEnv, etc.).

    You are free to:
    - Add any parameters with defaults to this function signature
    - Create additional files/modules in this package
    - Generate synthetic data, load from HuggingFace, or use any other source
    - Use any Environment subclass from verifiers
    - Define custom rubrics, parsers, tools, stop conditions, etc.

    The only constraint: this function must be importable and return
    a working vf.Environment.
    """
    raise NotImplementedError(
        "Environment not yet implemented. Read spec.md and implement me!"
    )
