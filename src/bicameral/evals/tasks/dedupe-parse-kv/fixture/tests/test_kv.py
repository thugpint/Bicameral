import pytest

from cli_args import overrides
from config_loader import load


def test_load():
    assert load(["# comment", "", "name = 'demo'", "port=8080"]) == {"name": "demo", "port": "8080"}


def test_overrides():
    assert overrides(["--set", "debug=true", "--set", 'msg="hi there"']) == {"debug": "true", "msg": "hi there"}


def test_bad_line():
    with pytest.raises(ValueError):
        load(["novalue"])
