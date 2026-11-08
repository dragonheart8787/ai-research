"""The warning filter must silence ONE known message and nothing else."""
from __future__ import annotations

import warnings

import pytest


def test_arbitrary_warnings_are_errors():
    for cat in (UserWarning, DeprecationWarning, ResourceWarning, RuntimeWarning, FutureWarning):
        with pytest.raises(cat):
            warnings.warn("some other warning", cat, stacklevel=1)


def test_a_similar_starlette_message_is_not_hidden():
    with pytest.raises(DeprecationWarning):
        warnings.warn("Using `requests` with `starlette.testclient` is deprecated", DeprecationWarning, stacklevel=1)


def test_only_the_exact_httpx_testclient_message_is_ignored():
    with warnings.catch_warnings(record=True) as seen:
        warnings.warn("Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead.", DeprecationWarning, stacklevel=1)
    assert seen == []   # silenced by the single exact pyproject filter
