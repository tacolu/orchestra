# Demo: CLI TODO App

Build a simple command-line TODO application in Python.

## Requirements

- Add tasks with a title and optional priority (low/medium/high)
- List all tasks, filtered by status (pending/done)
- Mark tasks as done
- Delete tasks
- Persist tasks to a JSON file (`todos.json`)

## Technical constraints

- Pure Python, no external dependencies
- Single file: `todo.py`
- Use argparse for CLI interface
- Pretty-print with colors (ANSI escape codes)

## Example usage

```
python todo.py add "Buy groceries" --priority high
python todo.py list
python todo.py done 1
python todo.py list --status done
python todo.py delete 1
```

## Acceptance criteria

- `python todo.py add "Test"` creates a task and prints confirmation
- `python todo.py list` shows all pending tasks with IDs
- `python todo.py done <id>` marks task as done
- Tasks persist across runs (saved to todos.json)
