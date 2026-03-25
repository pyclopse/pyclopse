"""
Tests for inbound message deduplication in Gateway._is_duplicate_message().
"""

import time
import pytest
from unittest.mock import MagicMock


def _make_gateway():
    from pyclawops.core.gateway import Gateway
    gw = Gateway.__new__(Gateway)
    gw._seen_message_ids = {}
    gw._dedup_ttl_seconds = 60
    gw._logger = MagicMock()
    return gw


class TestIsDuplicateMessage:

    def test_first_message_not_duplicate(self):
        gw = _make_gateway()
        assert gw._is_duplicate_message("telegram", "msg-1") is False

    def test_second_call_is_duplicate(self):
        gw = _make_gateway()
        gw._is_duplicate_message("telegram", "msg-1")
        assert gw._is_duplicate_message("telegram", "msg-1") is True

    def test_different_ids_not_duplicate(self):
        gw = _make_gateway()
        gw._is_duplicate_message("telegram", "msg-1")
        assert gw._is_duplicate_message("telegram", "msg-2") is False

    def test_different_channels_not_duplicate(self):
        gw = _make_gateway()
        gw._is_duplicate_message("telegram", "msg-1")
        # Same message_id on a different channel is not a duplicate
        assert gw._is_duplicate_message("slack", "msg-1") is False

    def test_stale_entry_evicted_and_not_duplicate(self):
        gw = _make_gateway()
        gw._dedup_ttl_seconds = 0  # expire immediately
        gw._is_duplicate_message("telegram", "msg-old")
        # Force timestamp to be in the past
        gw._seen_message_ids["telegram:msg-old"] = time.monotonic() - 1
        # After TTL expires the entry should be evicted → not a duplicate
        assert gw._is_duplicate_message("telegram", "msg-old") is False

    def test_stale_entries_evicted_on_next_call(self):
        gw = _make_gateway()
        gw._dedup_ttl_seconds = 0
        gw._seen_message_ids["telegram:stale-1"] = time.monotonic() - 100
        gw._seen_message_ids["telegram:stale-2"] = time.monotonic() - 100
        # Fresh call should evict stale entries
        gw._is_duplicate_message("telegram", "fresh")
        assert "telegram:stale-1" not in gw._seen_message_ids
        assert "telegram:stale-2" not in gw._seen_message_ids

    def test_fresh_entries_not_evicted(self):
        gw = _make_gateway()
        gw._dedup_ttl_seconds = 60
        gw._is_duplicate_message("telegram", "fresh-1")
        gw._is_duplicate_message("telegram", "fresh-2")
        assert "telegram:fresh-1" in gw._seen_message_ids
        assert "telegram:fresh-2" in gw._seen_message_ids

    def test_key_format_channel_colon_id(self):
        gw = _make_gateway()
        gw._is_duplicate_message("telegram", "12345")
        assert "telegram:12345" in gw._seen_message_ids

    def test_many_unique_messages_all_stored(self):
        gw = _make_gateway()
        for i in range(10):
            result = gw._is_duplicate_message("telegram", str(i))
            assert result is False
        assert len(gw._seen_message_ids) == 10
