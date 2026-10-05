"""A probe, not a test file: test_blocknet_plugin runs it by path (its name is not collected).

A plain run of the project must have the network guard installed."""

import blocknet


def test_the_guard_is_installed_in_a_plain_run():
    assert blocknet._originals != {}
