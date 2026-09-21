"""Every subcommand's help must render.

argparse only validates a help string's `%` placeholders when the help is
formatted (and, from Python 3.14, when the argument is added), so a stray
percent sign can take the whole CLI down -- every command builds the parser.
"""

import argparse

from gpp.cli import build_parser


def _subparsers(parser):
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            yield from action.choices.items()


def test_every_subcommand_help_renders():
    parser = build_parser()
    assert "usage: gpp" in parser.format_help()
    rendered = 0
    for name, sub in _subparsers(parser):
        assert name in sub.format_help()
        rendered += 1
        for inner_name, inner in _subparsers(sub):
            assert inner_name in inner.format_help()
            rendered += 1
    assert rendered >= 30
