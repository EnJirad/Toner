
# Toner

Toner is a reusable AI coding agent for projects selected through settings.json.
It is not tied to a specific repository or programming language.

## Requirements

- Python 3.10 or newer
- `requests`
- An API endpoint compatible with the Chat Completions API
- An API model/provider that supports tool calling when using file-editing tools
- Git is recommended for source control and change review

## Installation on Termux

```bash
pkg update
pkg install python git
python -m pip install requests
```

Create the Toner files in:

```text
/storage/emulated/0/VNE/PythonAG/
```

Allow Termux to access shared storage if necessary:

```bash
termux-setup-storage
```

## API key

Set the API key in the current Termux session:

```bash
export AIUNLIMIT_API_KEY='YOUR_API_KEY'
```

Replace `YOUR_API_KEY` with your own key. Do not publish it or commit it to Git.

If the API provider uses a different model name, set the exact supported name in settings.json.

## Select a project

Edit settings.json:

```json
{
  "project_root": "/storage/emulated/0/VNE/Vel/velnox"
}
```

Change the path to the project you want Toner to work on.

The selected project must already exist. Toner will create its own `.toner/` workspace there.

## Start Toner

```bash
cd /storage/emulated/0/VNE/PythonAG
python toner.py
```

## Commands

- `/check` — read and execute the task in `.toner/new_command.md`
- `/auto` — read Markdown plans in `.toner/files_md/`, create a task plan, and run tasks step by step
- `/status` — display saved task state
- `/resume` — continue unfinished work if the plan is unchanged
- `/report` — create a Markdown report
- `/help` — show help
- `/exit` — exit the agent

## Workspace created in the target project

```text
.toner/
├── files_md/
├── ai_recheck/
│   ├── auto_state.json
│   ├── progress.jsonl
│   ├── errors.jsonl
│   ├── decisions.md
│   ├── checkpoints/
│   ├── reports/
│   └── backups/
└── new_command.md
```

Add planning documents such as `01-overview.md` and `02-implementation.md`
to `.toner/files_md/` before using `/auto`.

## Safety

- Read the proposed changes and commands before approving them.
- Review `git status` and `git diff` regularly.
- Keep backups and an independent copy of important project data.
- Do not run destructive database, deployment, or Git commands unless you understand their effects.
- Do not assume approval prompts provide a complete security sandbox.
- Test changes yourself before deploying to production.

## Troubleshooting

### API authentication error
Check `AIUNLIMIT_API_KEY` and the provider's authentication requirements.

### Unknown model or tool-calling error
Verify that the provider supports the selected model and tool calling with the requested API format.

### Project path error
Check that `project_root` points to an existing directory and that Termux has permission to access it.

### Resume refuses to continue
Review the Markdown plans and saved state. Toner intentionally avoids silently reusing a plan after its files change.
