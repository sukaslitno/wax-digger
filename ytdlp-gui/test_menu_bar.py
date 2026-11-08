"""Checks that hiding a window does not stop ongoing downloads."""
import os
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app as wax


class WindowActionsTests(unittest.TestCase):
    def test_close_hides_without_stopping_download(self):
        api = wax.Api()
        api.window = Mock()
        api.menu_bar_ready = True
        process = Mock()
        with patch.object(wax, "active_procs", {process}):
            api.close_window()
        api.window.hide.assert_called_once()
        process.terminate.assert_not_called()

    def test_close_without_menu_exits(self):
        api = wax.Api()
        api.window = Mock()
        api.quit = Mock()
        api.close_window()
        api.quit.assert_called_once()

    def test_reopen_restores_minimized_window(self):
        api = wax.Api()
        api.window = Mock()
        api.open_window()
        api.window.restore.assert_called_once()
        api.window.show.assert_called_once()


if __name__ == "__main__":
    unittest.main()
