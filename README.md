# compfinder

Tool for finding class-scoped comprehensions whose name resolution might be impacted by
PEP 709.

## Usage

No dependencies except Python 3.10+.

```shell
python finder.py path
```

`path` may be a file or directory (which is scanned recursively).

## Development

### Set-up

Install development dependencies.

```shell
pip install -r frozen.txt
```

### Testing

```shell
python -m pytest
```

### Linting

```shell
python -m black --check finder.py test_finder.py
python -m mypy finder.py test_finder.py
```
