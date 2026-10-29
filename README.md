# Skills

A collection of self-contained agent skills.

## Available skills

| Skill | Purpose | Requirements |
| --- | --- | --- |
| [delegate](skills/delegate/SKILL.md) | Dispatch a coding task to Grok, Claude Code, Codex, Cursor, or Pi and return its changes for review. | Python 3.10+, Git, POSIX for live runs, and the selected provider CLI. |
| [github-ready](skills/github-ready/SKILL.md) | Prepare a local repository for GitHub publication or sharing with a bot: privacy, hygiene, docs, checks, readiness report. | An agent that can edit and run local commands, plus Git, Python 3.10+, and Bash. |

## Install

With the [skills CLI](https://github.com/vercel-labs/skills):

    npx skills add vx19/skills --skill delegate
    npx skills add vx19/skills --skill github-ready

Keep the complete `skills/<name>/` folder when installing manually. Installing
a skill does not install provider CLIs or authenticate them.

## Checks

From `skills/delegate/`:

    python3 tests/run.py
    bash tests/contract.sh

Both checks use fake runners and disposable repositories. They do not call
live providers.

From `skills/github-ready/`:

    python3 -m unittest discover -s scripts -p 'test_*.py'

## License

[MIT](LICENSE). Individual skills may also include their own license file.
