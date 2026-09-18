"""Benchmark the model tier with a coding agent answering over MCP instead of an API provider.

    python -m benchmarks.agent prepare --variants 5 --seed 7 [--valentine DIR --per-group 2]
    claude mcp add schemagate-bench -- <python> benchmarks/agent/server.py   # then let agents answer
    python -m benchmarks.agent status
    python -m benchmarks.agent score --run haiku --run sonnet

``prepare`` runs the schemagate resolver chain over a benchmark suite and records the exact request the
model tier would receive for each sender variant (only the columns the deterministic tiers left over).
The MCP server hands those requests to an agent one at a time and stores its answers per run name.
``score`` replays the chain with the stored answers in place of a provider and reports the same metrics
as ``benchmarks.run``, including how many high-confidence model answers the 0.75 cap held back and how
many of those were wrong. Ground truth never leaves this process: the server only serves the prompts.
"""
