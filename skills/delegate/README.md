# delegate

One CLI dispatches a coding task to Grok, Claude Code, Codex, Cursor or Pi. It
returns a JSON result and the worker's changes for review; the caller remains
responsible for reviewing and committing them.

## Requirements

- Python 3.10 or newer
- Git
- A POSIX host for live runs
- The provider CLI you intend to use, installed and authenticated separately

Delegate has no Python packages to install. Install the whole folder as an
agent skill; `SKILL.md` uses the bundled CLI, modules and templates.

## Quick start

From the skill folder, check the command without starting a provider. The
target repo needs at least one commit:

```sh
bin/delegate --help
bin/delegate doctor --provider grok --cwd /path/to/repo   # runner, git, trust
bin/delegate run --provider pi --cwd /path/to/repo --dry-run -- "example task"
```

Run a task and inspect its result:

```sh
bin/delegate run --provider grok --cwd /path/to/repo -- "one concrete task"
```

The [skill instructions](SKILL.md) explain the flags, provider behavior,
review workflow, output and safety limits.

## Tests

```sh
python3 tests/run.py
bash tests/contract.sh
```

Both checks use fake runners and disposable repositories; they do not call live
providers.

## License

[MIT](LICENSE).
