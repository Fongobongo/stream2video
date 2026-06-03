"""Tests for cli.py module behaviour (separate from import smoke tests)."""

import logging


class TestCliLoggingSetup:
    """cli.py must apply the user's --log-level to the console handler, not
    the logger itself. Otherwise the file handler also gets filtered (the
    logger level acts as a global floor) and changing --log-level has no
    visible effect on the console.
    """

    def test_console_handler_is_distinct_from_logger(self):
        from stream2video import cli
        assert cli._console_handler is not None
        assert isinstance(cli._console_handler, logging.Handler)
        assert cli._console_handler is not cli.logger

    def test_console_handler_can_be_releveled_independently(self):
        """Setting a level on the console handler must not change the logger's
        level — the file handler relies on the logger staying open at DEBUG.
        """
        from stream2video import cli
        original_handler_level = cli._console_handler.level
        original_logger_level = cli.logger.level
        try:
            cli._console_handler.setLevel(logging.WARNING)
            assert cli._console_handler.level == logging.WARNING
            assert cli.logger.level == original_logger_level
        finally:
            cli._console_handler.setLevel(original_handler_level)
