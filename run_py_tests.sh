python3 -m venv venv
. venv/bin/activate
pip install -e ".[dev]"
pytest tests/python/
