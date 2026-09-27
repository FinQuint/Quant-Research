# Phase 10B push notes

- Repository: `FinQuint/Quant-Research`
- Branch: `feature/specialist-agent-execution`
- PR base: `main`
- Baseline: merged Phase 10A commit `d6b4f0c95615c166349ebc0cd298467cdc1a9fe6`

Suggested commit: `feat: add controlled specialist agent execution`

Suggested PR title: `Phase 10B: Add capability-scoped specialist-agent execution`

Upload the contents of the inner `Quant-Research` directory at the repository
root, including `.github` and `.gitignore`. Merge the workflow file specifically
at `.github/workflows/tests.yml`; do not create a root-level `workflows` folder.

Before merging, run `python -m pytest` and
`python examples/specialist_agent_workflow.py`, then wait for the Python
3.10–3.12 GitHub Actions matrix.
