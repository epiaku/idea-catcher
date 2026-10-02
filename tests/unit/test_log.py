import logging

import pytest

from catcher.core.log import configure_logging


@pytest.fixture(autouse=True)
def restore_logger():
    logger = logging.getLogger("catcher")
    handlers, level = list(logger.handlers), logger.level
    yield
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    for handler in handlers:
        logger.addHandler(handler)
    logger.setLevel(level)


def test_lines_go_to_stderr_and_the_file(tmp_path, capsys):
    log_file = tmp_path / "logs" / "catcher.log"
    configure_logging("INFO", log_file)
    logging.getLogger("catcher.run").info("hello (1/2)")
    logging.getLogger("catcher.run").debug("hidden")
    assert "INFO" in capsys.readouterr().err
    text = log_file.read_text()
    assert "catcher.run: hello (1/2)" in text and "hidden" not in text


def test_configuring_twice_does_not_duplicate_lines(tmp_path):
    log_file = tmp_path / "c.log"
    configure_logging("INFO", log_file)
    configure_logging("INFO", log_file)
    logging.getLogger("catcher.run").info("once")
    assert log_file.read_text().count("once") == 1


def test_unknown_level_is_rejected():
    with pytest.raises(ValueError, match="unknown log level"):
        configure_logging("LOUD")
