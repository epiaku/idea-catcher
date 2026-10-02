from datetime import UTC, datetime

import pytest

from catcher.core.db import require_aware, utc_now


def test_require_aware_rejects_a_naive_datetime():
    with pytest.raises(ValueError, match="naive datetime"):
        require_aware(datetime(2026, 10, 2, 12, 0))
    aware = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
    assert require_aware(aware) is aware


def test_utc_now_is_aware():
    assert utc_now().utcoffset() is not None
