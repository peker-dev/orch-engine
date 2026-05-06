"""External runner adapters.

Modules in this package implement `BaseRunnerAdapter` for non-LLM verification
roles (Unity batchmode, lighthouse runner, custom test harnesses, ...). The
engine resolves a runner by importing `runners.<provider_name>` when the
provider id in `.orch/config/roles.yaml` does not match a reserved LLM CLI
name (`claude_cli`, `codex_cli`, `codex_app`).

See `memory/option-c-notes.md` for the surrounding design.
"""
