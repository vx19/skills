# Skills

A collection of self-contained agent skills.

## Available skills

| Skill | Purpose | Requirements |
| --- | --- | --- |
| [delegate](skills/delegate/SKILL.md) | Dispatch a coding task to Grok, Claude Code, Codex, Cursor, or Pi and return its changes for review. | Python 3.10+, Git, POSIX for live runs, and the selected provider CLI. |

## Install delegate

With the [skills CLI](https://github.com/vercel-labs/skills):

    npx skills add vx19/skills --skill delegate

Keep the complete skills/delegate/ folder when installing manually. It contains
the instructions, CLI, Python modules, and task templates.
Installing the skill does not install or authenticate provider CLIs.

To inspect the command without starting a provider:

    skills/delegate/bin/delegate --help

## Checks

From skills/delegate/:

    python3 tests/run.py
    bash tests/contract.sh

Both checks use fake runners and disposable repositories. They do not call
live providers.

## License

[MIT](LICENSE). The delegate skill also includes its own license.
