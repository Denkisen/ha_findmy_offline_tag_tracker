"""Shared pytest configuration.

Points the test harness at this repo as the config dir so that HA's
``_async_mount_config_dir`` imports *this* project's ``custom_components``
package (otherwise the fixture's own ``testing_config/custom_components``
shadows it and the integration is reported as "not found").
"""

from __future__ import annotations

import pathlib
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

# Make the repo importable for test modules.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture
def hass_config_dir() -> str:
    """Use this repo as the Home Assistant config directory."""
    return str(REPO_ROOT)
