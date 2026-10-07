from __future__ import annotations

import logging
import unittest

from classroom_app import _configure_logging


class LoggingConfigurationTests(unittest.TestCase):
    def test_debug_mode_does_not_enable_websocket_protocol_logging(self) -> None:
        _configure_logging(debug=True)

        self.assertGreaterEqual(
            logging.getLogger("websockets.client").getEffectiveLevel(),
            logging.WARNING,
        )


if __name__ == "__main__":
    unittest.main()
