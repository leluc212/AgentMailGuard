"""TokenCounter says whether it counts with the BPE encoding or with the word heuristic.

The heuristic is a silent fallback (R9.4 accepts it for chunking); a caller whose numbers must be
the ai-worker container's asks ``uses_bpe``. The encoding is faked: nothing here needs the network.
"""

from __future__ import annotations

import sys
import types

import pytest

from packages.knowledge.token_counter import TokenCounter


class _Encoding:
    def encode(self, text: str, disallowed_special: object = ()) -> list[int]:
        return list(range(len(text)))


def _tiktoken(monkeypatch: pytest.MonkeyPatch, *, loads: bool) -> None:
    module = types.ModuleType("tiktoken")

    def get_encoding(name: str) -> _Encoding:
        if not loads:
            raise ConnectionError("no network for the encoding download")
        return _Encoding()

    module.get_encoding = get_encoding  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tiktoken", module)


def test_a_counter_with_its_encoding_uses_bpe(monkeypatch: pytest.MonkeyPatch) -> None:
    _tiktoken(monkeypatch, loads=True)

    counter = TokenCounter()

    assert counter.uses_bpe is True and counter.count_tokens("abcd") == 4


def test_a_counter_whose_encoding_did_not_load_says_it_uses_the_heuristic(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _tiktoken(monkeypatch, loads=False)

    counter = TokenCounter()

    assert counter.uses_bpe is False
    assert counter.count_tokens("one two three four") == int(4 * 1.33)  # the word heuristic
