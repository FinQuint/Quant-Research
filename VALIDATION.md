# Phase 10B validation — 2026-09-27

Baseline: `FinQuint/Quant-Research` `main` at
`d6b4f0c95615c166349ebc0cd298467cdc1a9fe6`.

The complete local suite passes on Windows with Python 3.12: **135 passed**.
All 126 inherited tests remain passing and nine Phase 10B tests were added.

Coverage includes capability enforcement, isolated reads and writes, absolute
and traversal-path rejection, changed-file and action budgets, validation
allowlists, structured traces and metrics, registry uniqueness, safe failed-task
capture, multi-agent ready-task execution, and continuation of independent work
while another task awaits human approval. The controlled-agent example also runs
successfully.

Python 3.10/3.11 and Linux are covered by GitHub Actions after upload. Phase 10B
does not provide arbitrary shell, unrestricted network, or merge capabilities.
