# Contributing

Issues and pull requests are welcome. Small, focused changes land fastest.

## Setup

```bash
git clone https://github.com/thugpint/Bicameral.git && cd Bicameral
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"   # POSIX: .venv/bin/python
.venv/Scripts/python -m pytest -q
```

The tests run fully offline against scripted fake backends (`tests/fake.py`) and a throwaway git repository. No account or API key is needed.

## Before opening a pull request

- Open an issue first for anything larger than a bug fix, so the design can be agreed before the code exists.
- Keep the change surgical. Touch only what the fix or feature needs; leave unrelated formatting and refactors out.
- Add or update tests. A behaviour change without a test will be sent back.
- Run `pytest -q` and `ruff check src tests` and make sure both pass.
- Match the surrounding code: 120-column lines, type hints, dataclasses, standard library first.
- Write a commit message that says what changed and why, in the imperative mood.

## Reporting bugs

Use the bug report template. Include the Bicameral version, Python version, operating system, which Editor backend you were using, the exact command, and the output. Runs leave logs under `.bicameral/`; the relevant lines from there are usually what makes a bug reproducible.

## Security

Do not open a public issue for a vulnerability. See [SECURITY.md](SECURITY.md).

## License

By contributing you agree that your contributions are licensed under the [MIT License](LICENSE).
