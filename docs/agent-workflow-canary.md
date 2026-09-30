# Agent Workflow Canary

FirstProject supports isolated local-agent workflows. This file serves as a canary marker
verifying that an agent can safely add documentation without modifying production code.

## Canary

- **Purpose:** Verify agent workflow integrity (add-only, no runtime changes).
- **Scope:** Documentation only; no changes to `app.py`, `static/`, `Dockerfile`, or `compose.yaml`.
