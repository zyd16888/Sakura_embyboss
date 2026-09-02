#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pyrogram import Client, utils
from pyrogram.errors import ListenerTimeout
from pyrogram.helpers import array_chunk, ikb

spec = importlib.util.spec_from_file_location(
    "keyboard_under_test", ROOT / "bot" / "func_helper" / "keyboard.py"
)
keyboard_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(keyboard_module)
InlineButton = keyboard_module.InlineButton
InlineKeyboard = keyboard_module.InlineKeyboard


class PyroforkCompatibilityTests(unittest.TestCase):
    def test_large_channel_id_is_supported(self):
        peer_id = -1002223922785
        self.assertEqual(utils.get_peer_type(peer_id), "channel")
        self.assertEqual(utils.get_channel_id(peer_id), 2223922785)

    def test_builtin_pyromod_features_are_available(self):
        self.assertTrue(hasattr(Client, "listen"))
        self.assertTrue(issubclass(ListenerTimeout, Exception))
        self.assertEqual(array_chunk([1, 2, 3], 2), [[1, 2], [3]])
        self.assertEqual(len(ikb([[('A', 'a')]]).inline_keyboard), 1)

    def test_local_keyboard_compatibility(self):
        keyboard = InlineKeyboard(row_width=2)
        keyboard.add(InlineButton("A", "a"), InlineButton("B", "b"))
        keyboard.paginate(7, 4, "page:{number}")
        keyboard.row(InlineButton("Back", "back"))

        self.assertEqual(len(keyboard.inline_keyboard), 3)
        self.assertEqual(keyboard.inline_keyboard[0][0].callback_data, "a")
        self.assertIn("page:4", [button.callback_data
                                 for button in keyboard.inline_keyboard[1]])
        self.assertEqual(keyboard.inline_keyboard[-1][0].callback_data, "back")


if __name__ == "__main__":
    unittest.main(verbosity=2)
