"""Tests for the vault ULID generator."""

import time

from pyclaw.memory.vault.ulid import generate, timestamp


class TestUlidGenerate:
    def test_length_is_26(self):
        ulid = generate()
        assert len(ulid) == 26

    def test_all_uppercase_crockford(self):
        """ULIDs should only contain Crockford base32 characters."""
        valid_chars = set("0123456789ABCDEFGHJKMNPQRSTVWXYZ")
        for _ in range(20):
            ulid = generate()
            assert all(c in valid_chars for c in ulid), f"Invalid chars in ULID: {ulid}"

    def test_ulids_sort_chronologically(self):
        """ULIDs generated in sequence should sort in the same order."""
        ulids = []
        for _ in range(10):
            ulids.append(generate())
            time.sleep(0.001)  # 1ms sleep to ensure different timestamps
        sorted_ulids = sorted(ulids)
        assert ulids == sorted_ulids, "ULIDs do not sort chronologically"

    def test_uniqueness(self):
        """No two generated ULIDs should be identical."""
        ulids = [generate() for _ in range(100)]
        assert len(set(ulids)) == 100

    def test_string_type(self):
        ulid = generate()
        assert isinstance(ulid, str)


class TestUlidTimestamp:
    def test_timestamp_is_datetime(self):
        from datetime import datetime
        ulid = generate()
        ts = timestamp(ulid)
        assert isinstance(ts, datetime)

    def test_timestamp_is_utc(self):
        from datetime import timezone
        ulid = generate()
        ts = timestamp(ulid)
        assert ts.tzinfo == timezone.utc

    def test_timestamp_close_to_now(self):
        from datetime import datetime, timedelta, timezone
        before = datetime.now(timezone.utc)
        ulid = generate()
        after = datetime.now(timezone.utc)
        ts = timestamp(ulid)
        # Allow 1ms tolerance: ULID stores millisecond precision so the
        # reconstructed timestamp may be truncated relative to before/after.
        tolerance = timedelta(milliseconds=1)
        assert (before - tolerance) <= ts <= (after + tolerance)

    def test_timestamp_invalid_length(self):
        import pytest
        with pytest.raises(ValueError, match="26"):
            timestamp("TOOSHORT")

    def test_timestamp_invalid_chars(self):
        import pytest
        with pytest.raises(ValueError):
            # 'I' is not in Crockford base32 alphabet
            timestamp("IIIIIIIIIIIIIIIIIIIIIIIIII")

    def test_older_ulid_has_earlier_timestamp(self):
        ulid1 = generate()
        time.sleep(0.01)
        ulid2 = generate()
        ts1 = timestamp(ulid1)
        ts2 = timestamp(ulid2)
        assert ts1 < ts2
