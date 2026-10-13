"""Tests for the UPS status file reader (_read_ups_file / _read_ups_status)."""

import json
import os
import stat
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as app_module


@pytest.fixture
def tmp_status_file(tmp_path):
    """Return a path to a temp file that can be used as UPS_STATUS_FILE."""
    return tmp_path / "ups-status.json"


def _make_valid_json(observed_at=None):
    if observed_at is None:
        observed_at = datetime.now(timezone.utc).isoformat()
    return json.dumps({
        "power_source": "ups",
        "battery_percent": 85,
        "ups_present": True,
        "monitor_state": "running",
        "observed_at": observed_at,
    })


class TestReadUpsFile:
    """Unit tests for _read_ups_file."""

    def test_valid_file_returns_text(self, tmp_status_file):
        """A valid JSON file returns its text content."""
        content = _make_valid_json()
        tmp_status_file.write_text(content)
        result = app_module._read_ups_file(tmp_status_file)
        assert result == content

    def test_missing_file_returns_none(self, tmp_status_file):
        """A non-existent file returns None."""
        result = app_module._read_ups_file(tmp_status_file)
        assert result is None

    def test_oversized_file_returns_none(self, tmp_status_file):
        """A file larger than the cap returns None."""
        limit = app_module.UPS_STATUS_MAX_BYTES
        tmp_status_file.write_bytes(b'A' * (limit + 1))
        result = app_module._read_ups_file(tmp_status_file)
        assert result is None

    def test_exactly_at_limit_returns_text(self, tmp_status_file):
        """A file exactly at the limit returns its content."""
        limit = app_module.UPS_STATUS_MAX_BYTES
        data = b'x' * limit
        tmp_status_file.write_bytes(data)
        result = app_module._read_ups_file(tmp_status_file)
        assert result is not None
        assert len(result.encode()) == limit

    def test_invalid_utf8_returns_none(self, tmp_status_file):
        """A file with invalid UTF-8 returns None."""
        tmp_status_file.write_bytes(b'\xff\xfe\x00\x01')
        result = app_module._read_ups_file(tmp_status_file)
        assert result is None

    def test_directory_returns_none(self, tmp_path):
        """A directory path returns None."""
        d = tmp_path / "some_dir"
        d.mkdir()
        result = app_module._read_ups_file(d)
        assert result is None

    def test_empty_file_returns_empty_string(self, tmp_status_file):
        """An empty file returns an empty string."""
        tmp_status_file.write_bytes(b'')
        result = app_module._read_ups_file(tmp_status_file)
        assert result == ''

    def test_file_growth_bounded_read(self, tmp_status_file):
        """File grows between fstat and read; read is still bounded."""
        limit = app_module.UPS_STATUS_MAX_BYTES
        # Write a small valid file so fstat passes
        small_data = b'{"power_source": "mains"}'
        tmp_status_file.write_bytes(small_data)

        read_calls = []
        returned_lengths = []
        close_calls = []
        real_read = os.read
        real_fstat = os.fstat
        real_close = os.close

        def tracking_read(fd, nbytes):
            read_calls.append(nbytes)
            data = real_read(fd, nbytes)
            returned_lengths.append(len(data))
            return data

        def growing_fstat(fd):
            st = real_fstat(fd)
            # Grow the same inode after stat is captured
            with open(tmp_status_file, "ab") as f:
                f.write(b'A' * (limit + 100))
            return st

        def tracking_close(fd):
            close_calls.append(fd)
            return real_close(fd)

        with patch.object(app_module.os, "read", side_effect=tracking_read), \
             patch.object(app_module.os, "fstat", side_effect=growing_fstat), \
             patch.object(app_module.os, "close", side_effect=tracking_close):
            result = app_module._read_ups_file(tmp_status_file)

        assert read_calls == [limit + 1]
        assert returned_lengths == [limit + 1]
        assert result is None
        assert len(close_calls) == 1


class TestReadUpsStatus:
    """Unit tests for _read_ups_status."""

    def test_valid_json_returns_dict(self, tmp_status_file, monkeypatch):
        """Valid JSON file returns a dict with fresh health."""
        content = _make_valid_json()
        tmp_status_file.write_text(content)
        monkeypatch.setattr(app_module, "UPS_STATUS_FILE", tmp_status_file)
        now = datetime.now(timezone.utc)
        result = app_module._read_ups_status(now)
        assert result["power_source"] == "ups"
        assert result["battery_percent"] == 85
        assert result["source_health"] == "fresh"
        assert result["stale"] is False

    def test_missing_file_returns_missing(self, tmp_status_file, monkeypatch):
        """Missing file returns dict with source_health=missing."""
        monkeypatch.setattr(app_module, "UPS_STATUS_FILE", tmp_status_file)
        now = datetime.now(timezone.utc)
        result = app_module._read_ups_status(now)
        assert result["source_health"] == "missing"
        assert result["stale"] is True

    def test_invalid_json_returns_malformed(self, tmp_status_file, monkeypatch):
        """Invalid JSON returns dict with source_health=malformed."""
        tmp_status_file.write_text("not valid json {{{")
        monkeypatch.setattr(app_module, "UPS_STATUS_FILE", tmp_status_file)
        now = datetime.now(timezone.utc)
        result = app_module._read_ups_status(now)
        assert result["source_health"] == "malformed"

    def test_missing_fields_returns_unknown(self, tmp_status_file, monkeypatch):
        """JSON missing required fields returns dict with unknown power_source."""
        tmp_status_file.write_text(json.dumps({"foo": "bar"}))
        monkeypatch.setattr(app_module, "UPS_STATUS_FILE", tmp_status_file)
        now = datetime.now(timezone.utc)
        result = app_module._read_ups_status(now)
        assert result["power_source"] == "unknown"
        assert result["source_health"] == "invalid"

    def test_stale_timestamp_returns_stale(self, tmp_status_file, monkeypatch):
        """A stale observed_at timestamp returns stale."""
        stale = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        content = json.dumps({
            "power_source": "ups",
            "battery_percent": 50,
            "ups_present": True,
            "monitor_state": "running",
            "observed_at": stale,
        })
        tmp_status_file.write_text(content)
        monkeypatch.setattr(app_module, "UPS_STATUS_FILE", tmp_status_file)
        now = datetime.now(timezone.utc)
        result = app_module._read_ups_status(now)
        assert result["source_health"] == "stale"
        assert result["stale"] is True

    def test_fresh_timestamp_returns_fresh(self, tmp_status_file, monkeypatch):
        """A fresh observed_at timestamp returns fresh."""
        fresh = datetime.now(timezone.utc).isoformat()
        content = json.dumps({
            "power_source": "ups",
            "battery_percent": 50,
            "ups_present": True,
            "monitor_state": "running",
            "observed_at": fresh,
        })
        tmp_status_file.write_text(content)
        monkeypatch.setattr(app_module, "UPS_STATUS_FILE", tmp_status_file)
        now = datetime.now(timezone.utc)
        result = app_module._read_ups_status(now)
        assert result["source_health"] == "fresh"
        assert result["stale"] is False

    def test_oversized_file_returns_invalid(self, tmp_status_file, monkeypatch):
        """An oversized file returns dict with source_health=invalid."""
        limit = app_module.UPS_STATUS_MAX_BYTES
        tmp_status_file.write_bytes(b'A' * (limit + 1))
        monkeypatch.setattr(app_module, "UPS_STATUS_FILE", tmp_status_file)
        now = datetime.now(timezone.utc)
        result = app_module._read_ups_status(now)
        assert result["source_health"] == "invalid"

    def test_invalid_utf8_returns_invalid(self, tmp_status_file, monkeypatch):
        """Invalid UTF-8 in file returns dict with source_health=invalid."""
        tmp_status_file.write_bytes(b'\xff\xfe\x00\x01')
        monkeypatch.setattr(app_module, "UPS_STATUS_FILE", tmp_status_file)
        now = datetime.now(timezone.utc)
        result = app_module._read_ups_status(now)
        assert result["source_health"] == "invalid"

    def test_permission_denied_returns_invalid(self, tmp_status_file, monkeypatch):
        """A file with no read permission returns dict with source_health=invalid."""
        tmp_status_file.write_text(_make_valid_json())
        tmp_status_file.chmod(0o000)
        monkeypatch.setattr(app_module, "UPS_STATUS_FILE", tmp_status_file)
        try:
            now = datetime.now(timezone.utc)
            result = app_module._read_ups_status(now)
            assert result["source_health"] == "invalid"
        finally:
            tmp_status_file.chmod(0o644)

    def test_power_source_ac(self, tmp_status_file, monkeypatch):
        """power_source of 'ac' is correctly parsed."""
        fresh = datetime.now(timezone.utc).isoformat()
        content = json.dumps({
            "power_source": "ac",
            "battery_percent": 100,
            "ups_present": True,
            "monitor_state": "running",
            "observed_at": fresh,
        })
        tmp_status_file.write_text(content)
        monkeypatch.setattr(app_module, "UPS_STATUS_FILE", tmp_status_file)
        now = datetime.now(timezone.utc)
        result = app_module._read_ups_status(now)
        assert result["power_source"] == "ac"
        assert result["source_health"] == "fresh"

    def test_power_source_ups(self, tmp_status_file, monkeypatch):
        """power_source of 'ups' is correctly parsed."""
        fresh = datetime.now(timezone.utc).isoformat()
        content = json.dumps({
            "power_source": "ups",
            "battery_percent": 42,
            "ups_present": True,
            "monitor_state": "running",
            "observed_at": fresh,
        })
        tmp_status_file.write_text(content)
        monkeypatch.setattr(app_module, "UPS_STATUS_FILE", tmp_status_file)
        now = datetime.now(timezone.utc)
        result = app_module._read_ups_status(now)
        assert result["power_source"] == "ups"
        assert result["battery_percent"] == 42
        assert result["source_health"] == "fresh"

    def test_extra_fields_not_in_normalized(self, tmp_status_file, monkeypatch):
        """Extra fields in the JSON are not in the normalized output."""
        fresh = datetime.now(timezone.utc).isoformat()
        content = json.dumps({
            "power_source": "ups",
            "battery_percent": 77,
            "ups_present": True,
            "monitor_state": "running",
            "observed_at": fresh,
            "extra_field": "hello",
        })
        tmp_status_file.write_text(content)
        monkeypatch.setattr(app_module, "UPS_STATUS_FILE", tmp_status_file)
        now = datetime.now(timezone.utc)
        result = app_module._read_ups_status(now)
        assert "extra_field" not in result

    def test_battery_percent_zero(self, tmp_status_file, monkeypatch):
        """battery_percent of 0 is valid and preserved."""
        fresh = datetime.now(timezone.utc).isoformat()
        content = json.dumps({
            "power_source": "ups",
            "battery_percent": 0,
            "ups_present": True,
            "monitor_state": "running",
            "observed_at": fresh,
        })
        tmp_status_file.write_text(content)
        monkeypatch.setattr(app_module, "UPS_STATUS_FILE", tmp_status_file)
        now = datetime.now(timezone.utc)
        result = app_module._read_ups_status(now)
        assert result["battery_percent"] == 0

    def test_battery_percent_hundred(self, tmp_status_file, monkeypatch):
        """battery_percent of 100 is valid and preserved."""
        fresh = datetime.now(timezone.utc).isoformat()
        content = json.dumps({
            "power_source": "ups",
            "battery_percent": 100,
            "ups_present": True,
            "monitor_state": "running",
            "observed_at": fresh,
        })
        tmp_status_file.write_text(content)
        monkeypatch.setattr(app_module, "UPS_STATUS_FILE", tmp_status_file)
        now = datetime.now(timezone.utc)
        result = app_module._read_ups_status(now)
        assert result["battery_percent"] == 100

    def test_non_numeric_battery_returns_none(self, tmp_status_file, monkeypatch):
        """Non-numeric battery_percent is normalized to None."""
        fresh = datetime.now(timezone.utc).isoformat()
        content = json.dumps({
            "power_source": "ups",
            "battery_percent": "not_a_number",
            "ups_present": True,
            "monitor_state": "running",
            "observed_at": fresh,
        })
        tmp_status_file.write_text(content)
        monkeypatch.setattr(app_module, "UPS_STATUS_FILE", tmp_status_file)
        now = datetime.now(timezone.utc)
        result = app_module._read_ups_status(now)
        assert result["battery_percent"] is None

    def test_invalid_power_source_returns_unknown(self, tmp_status_file, monkeypatch):
        """Invalid power_source value is normalized to unknown."""
        fresh = datetime.now(timezone.utc).isoformat()
        content = json.dumps({
            "power_source": "invalid_source",
            "battery_percent": 50,
            "ups_present": True,
            "monitor_state": "running",
            "observed_at": fresh,
        })
        tmp_status_file.write_text(content)
        monkeypatch.setattr(app_module, "UPS_STATUS_FILE", tmp_status_file)
        now = datetime.now(timezone.utc)
        result = app_module._read_ups_status(now)
        assert result["power_source"] == "unknown"

    def test_json_array_returns_malformed(self, tmp_status_file, monkeypatch):
        """A JSON array (not object) returns malformed."""
        tmp_status_file.write_text("[1, 2, 3]")
        monkeypatch.setattr(app_module, "UPS_STATUS_FILE", tmp_status_file)
        now = datetime.now(timezone.utc)
        result = app_module._read_ups_status(now)
        assert result["source_health"] == "malformed"

    def test_json_string_returns_malformed(self, tmp_status_file, monkeypatch):
        """A JSON string (not object) returns malformed."""
        tmp_status_file.write_text('"just a string"')
        monkeypatch.setattr(app_module, "UPS_STATUS_FILE", tmp_status_file)
        now = datetime.now(timezone.utc)
        result = app_module._read_ups_status(now)
        assert result["source_health"] == "malformed"

    def test_json_null_returns_malformed(self, tmp_status_file, monkeypatch):
        """A JSON null returns malformed."""
        tmp_status_file.write_text("null")
        monkeypatch.setattr(app_module, "UPS_STATUS_FILE", tmp_status_file)
        now = datetime.now(timezone.utc)
        result = app_module._read_ups_status(now)
        assert result["source_health"] == "malformed"


# ---------------------------------------------------------------------------
# Additional safety-coverage tests (appended)
# ---------------------------------------------------------------------------

import sys
import stat as stat_mod


class TestReadUpsFileSymlink:
    """_read_ups_file must reject symlinks via O_NOFOLLOW."""

    def test_symlink_to_valid_file_returns_none(self, tmp_path, monkeypatch):
        """A symlink pointing to a valid JSON file must be rejected (O_NOFOLLOW)."""
        if not hasattr(os, "O_NOFOLLOW"):
            pytest.skip("O_NOFOLLOW not available on this platform")

        # Create a real valid file
        real_file = tmp_path / "real.json"
        real_file.write_bytes(b'{"power_source": "mains"}')

        # Create a symlink to it
        link = tmp_path / "link.json"
        link.symlink_to(real_file)

        result = app_module._read_ups_file(link)
        assert result is None


class TestReadUpsFileFifo:
    """_read_ups_file must handle FIFO (named pipe) safely."""

    def test_mkfifo_returns_none_no_producer(self, tmp_path, monkeypatch):
        """Opening a FIFO with O_NONBLOCK should not block; fstat should show non-regular."""
        if not hasattr(os, "O_NONBLOCK"):
            pytest.skip("O_NONBLOCK not available on this platform")
        if not hasattr(os, "mkfifo"):
            pytest.skip("mkfifo not available on this platform")

        fifo_path = tmp_path / "test_fifo"
        os.mkfifo(str(fifo_path))

        # With O_NONBLOCK, os.open on a FIFO should succeed (no producer needed)
        # fstat will show S_ISFIFO, not S_ISREG, so _read_ups_file returns None
        result = app_module._read_ups_file(fifo_path)
        assert result is None


class TestReadUpsFileFDClosed:
    """Verify os.close is called exactly once on every code path."""

    def test_valid_read_closes_fd_once(self, tmp_path, monkeypatch):
        """On a valid read, the fd must be closed exactly once."""
        f = tmp_path / "status.json"
        f.write_bytes(b'{"power_source": "mains"}')

        close_calls = []
        real_close = os.close

        def tracking_close(fd):
            close_calls.append(fd)
            return real_close(fd)

        monkeypatch.setattr(os, "close", tracking_close)

        result = app_module._read_ups_file(f)
        assert result == '{"power_source": "mains"}'
        assert len(close_calls) == 1
        # Verify the fd was actually closed (real close was called)
        # If the fd were leaked, we can't easily detect it, but we verified
        # real_close was invoked with the correct fd.

    def test_invalid_utf8_closes_fd_once(self, tmp_path, monkeypatch):
        """On invalid UTF-8, the fd must be closed exactly once."""
        f = tmp_path / "status.json"
        f.write_bytes(b'\xff\xfe\x00\x01')  # invalid UTF-8

        close_calls = []
        real_close = os.close

        def tracking_close(fd):
            close_calls.append(fd)
            return real_close(fd)

        monkeypatch.setattr(os, "close", tracking_close)

        result = app_module._read_ups_file(f)
        assert result is None
        assert len(close_calls) == 1

    def test_fstat_nonregular_closes_fd_once(self, tmp_path, monkeypatch):
        """When fstat shows non-regular file, fd must be closed exactly once."""
        if not hasattr(os, "mkfifo"):
            pytest.skip("mkfifo not available on this platform")

        fifo_path = tmp_path / "test_fifo"
        os.mkfifo(str(fifo_path))

        close_calls = []
        real_close = os.close

        def tracking_close(fd):
            close_calls.append(fd)
            return real_close(fd)

        monkeypatch.setattr(os, "close", tracking_close)

        result = app_module._read_ups_file(fifo_path)
        assert result is None
        assert len(close_calls) == 1

    def test_oversize_rejection_closes_fd_once(self, tmp_path, monkeypatch):
        """When file is oversized, fd must be closed exactly once."""
        f = tmp_path / "status.json"
        f.write_bytes(b'A' * (app_module.UPS_STATUS_MAX_BYTES + 100))

        close_calls = []
        real_close = os.close

        def tracking_close(fd):
            close_calls.append(fd)
            return real_close(fd)

        monkeypatch.setattr(os, "close", tracking_close)

        result = app_module._read_ups_file(f)
        assert result is None
        assert len(close_calls) == 1

    def test_os_read_raises_oseerror_closes_fd_once(self, tmp_path, monkeypatch):
        """When os.read raises OSError, fd must be closed exactly once."""
        f = tmp_path / "status.json"
        f.write_bytes(b'{"power_source": "mains"}')

        close_calls = []
        real_close = os.close
        real_read = os.read

        def failing_read(fd, nbytes):
            raise OSError("simulated read failure")

        def tracking_close(fd):
            close_calls.append(fd)
            return real_close(fd)

        monkeypatch.setattr(os, "read", failing_read)
        monkeypatch.setattr(os, "close", tracking_close)

        result = app_module._read_ups_file(f)
        assert result is None
        assert len(close_calls) == 1


class TestReadUpsStatusSymlink:
    """_read_ups_status must report invalid when the status file is a symlink."""

    def test_symlink_source_health_invalid_stale_true(self, tmp_path, monkeypatch):
        """Symlink to valid file => source_health='invalid', stale=True."""
        if not hasattr(os, "O_NOFOLLOW"):
            pytest.skip("O_NOFOLLOW not available on this platform")

        real_file = tmp_path / "real.json"
        real_file.write_bytes(b'{"power_source": "mains", "observed_at": "2025-01-01T00:00:00Z"}')

        link = tmp_path / "link.json"
        link.symlink_to(real_file)

        monkeypatch.setattr(app_module, "UPS_STATUS_FILE", link)

        from datetime import datetime, timezone
        now = datetime(2025, 1, 1, 0, 0, 10, tzinfo=timezone.utc)
        result = app_module._read_ups_status(now)

        assert result["source_health"] == "invalid"
        assert result["stale"] is True
        assert result["power_source"] == "unknown"


class TestReadUpsStatusDirectory:
    """_read_ups_status must report invalid when the path is a directory."""

    def test_directory_source_health_invalid(self, tmp_path, monkeypatch):
        """A directory at the status file path => source_health='invalid'."""
        dir_path = tmp_path / "status_dir"
        dir_path.mkdir()

        monkeypatch.setattr(app_module, "UPS_STATUS_FILE", dir_path)

        from datetime import datetime, timezone
        now = datetime(2025, 1, 1, 0, 0, 10, tzinfo=timezone.utc)
        result = app_module._read_ups_status(now)

        assert result["source_health"] == "invalid"
        assert result["stale"] is True
        assert result["power_source"] == "unknown"


# ---------------------------------------------------------------------------
# New validation tests: timestamp, core fields, presence, last_event
# ---------------------------------------------------------------------------

class TestParseIsoUtc:
    """Tests for _parse_iso_utc validation."""

    def test_naive_timestamp_rejected(self):
        """Naive timestamp (no tzinfo) is rejected."""
        result = app_module._parse_iso_utc("2024-01-01T12:00:00")
        assert result is None

    def test_aware_utc_z_accepted(self):
        """Aware UTC with Z suffix is accepted."""
        result = app_module._parse_iso_utc("2024-01-01T12:00:00Z")
        assert result is not None
        assert result.tzinfo is not None
        assert result.utcoffset() == timedelta(0)

    def test_aware_utc_offset_accepted(self):
        """Aware UTC with +00:00 offset is accepted."""
        result = app_module._parse_iso_utc("2024-01-01T12:00:00+00:00")
        assert result is not None
        assert result.utcoffset() == timedelta(0)

    def test_aware_nonzero_offset_normalized_to_utc(self):
        """Aware non-zero offset is normalized to UTC."""
        result = app_module._parse_iso_utc("2024-01-01T12:00:00+05:00")
        assert result is not None
        assert result.utcoffset() == timedelta(0)
        # 12:00+05:00 = 07:00 UTC
        assert result.hour == 7

    def test_invalid_string_returns_none(self):
        """Invalid string returns None."""
        assert app_module._parse_iso_utc("not_a_date") is None
        assert app_module._parse_iso_utc("") is None
        assert app_module._parse_iso_utc("2024-99-99T99:99:99") is None

    def test_non_string_returns_none(self):
        """Non-string types return None."""
        assert app_module._parse_iso_utc(None) is None
        assert app_module._parse_iso_utc(12345) is None
        assert app_module._parse_iso_utc(12345.678) is None
        assert app_module._parse_iso_utc(["2024-01-01T12:00:00Z"]) is None

    def test_overflow_returns_none(self):
        """Overflow values return None safely."""
        assert app_module._parse_iso_utc("99999-01-01T00:00:00+00:00") is None


class TestTimestampValidation:
    """Tests for timestamp freshness validation in _read_ups_status."""

    def _write_and_read(self, tmp_status_file, monkeypatch, observed_at):
        content = json.dumps({
            "power_source": "ups",
            "battery_percent": 80,
            "ups_present": True,
            "monitor_state": "running",
            "observed_at": observed_at,
        })
        tmp_status_file.write_text(content)
        monkeypatch.setattr(app_module, "UPS_STATUS_FILE", tmp_status_file)
        now = datetime.now(timezone.utc)
        return app_module._read_ups_status(now)

    def test_naive_timestamp_stale(self, tmp_status_file, monkeypatch):
        """Naive timestamp cannot mark source fresh."""
        naive = datetime.now().isoformat()  # no tzinfo
        result = self._write_and_read(tmp_status_file, monkeypatch, naive)
        assert result["source_health"] == "stale"
        assert result["stale"] is True
        assert result["source_fresh"] is False

    def test_future_timestamp_stale(self, tmp_status_file, monkeypatch):
        """Future timestamp is stale."""
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        result = self._write_and_read(tmp_status_file, monkeypatch, future)
        assert result["source_health"] == "stale"
        assert result["stale"] is True
        assert result["source_fresh"] is False

    def test_stale_over_30sec(self, tmp_status_file, monkeypatch):
        """Timestamp >30s old is stale."""
        old = (datetime.now(timezone.utc) - timedelta(seconds=31)).isoformat()
        result = self._write_and_read(tmp_status_file, monkeypatch, old)
        assert result["source_health"] == "stale"
        assert result["stale"] is True
        assert result["source_fresh"] is False

    def test_exact_30sec_boundary_stale(self, tmp_status_file, monkeypatch):
        """Exactly 30 seconds old is stale (boundary)."""
        exact = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
        result = self._write_and_read(tmp_status_file, monkeypatch, exact)
        assert result["source_health"] == "stale"
        assert result["stale"] is True
        assert result["source_fresh"] is False

    def test_just_under_30sec_fresh(self, tmp_status_file, monkeypatch):
        """Just under 30 seconds is fresh."""
        recent = (datetime.now(timezone.utc) - timedelta(seconds=29)).isoformat()
        result = self._write_and_read(tmp_status_file, monkeypatch, recent)
        assert result["source_health"] == "fresh"
        assert result["stale"] is False
        assert result["source_fresh"] is True

    def test_aware_utc_z_fresh(self, tmp_status_file, monkeypatch):
        """Aware UTC Z timestamp within 30s is fresh."""
        now = datetime.now(timezone.utc)
        z_time = now.replace(microsecond=0).isoformat().replace("+00:00", "Z")
        content = json.dumps({
            "power_source": "ups",
            "battery_percent": 80,
            "ups_present": True,
            "monitor_state": "running",
            "observed_at": z_time,
        })
        tmp_status_file.write_text(content)
        monkeypatch.setattr(app_module, "UPS_STATUS_FILE", tmp_status_file)
        result = app_module._read_ups_status(datetime.now(timezone.utc))
        assert result["source_health"] == "fresh"
        assert result["source_fresh"] is True


class TestCoreFieldValidation:
    """Tests for core field type/range validation."""

    def _write_and_read(self, tmp_status_file, monkeypatch, **overrides):
        base = {
            "power_source": "ups",
            "battery_percent": 80,
            "ups_present": True,
            "monitor_state": "running",
            "observed_at": datetime.now(timezone.utc).isoformat(),
        }
        base.update(overrides)
        tmp_status_file.write_text(json.dumps(base))
        monkeypatch.setattr(app_module, "UPS_STATUS_FILE", tmp_status_file)
        now = datetime.now(timezone.utc)
        return app_module._read_ups_status(now)

    @pytest.mark.parametrize("field", [
        "power_source", "battery_percent", "ups_present", "monitor_state",
    ])
    def test_missing_core_field_invalid(self, tmp_status_file, monkeypatch, field):
        """Missing any core field => source_health invalid."""
        base = {
            "power_source": "ups",
            "battery_percent": 80,
            "ups_present": True,
            "monitor_state": "running",
            "observed_at": datetime.now(timezone.utc).isoformat(),
        }
        del base[field]
        tmp_status_file.write_text(json.dumps(base))
        monkeypatch.setattr(app_module, "UPS_STATUS_FILE", tmp_status_file)
        now = datetime.now(timezone.utc)
        result = app_module._read_ups_status(now)
        assert result["source_health"] == "invalid"
        assert result["stale"] is True
        assert result["source_fresh"] is False

    def test_bool_battery_rejected(self, tmp_status_file, monkeypatch):
        """Bool battery (int subclass) is rejected."""
        result = self._write_and_read(
            tmp_status_file, monkeypatch, battery_percent=True
        )
        assert result["source_health"] == "invalid"
        assert result["battery_percent"] is None

    def test_float_battery_rejected(self, tmp_status_file, monkeypatch):
        """Float battery is rejected (must be int)."""
        result = self._write_and_read(
            tmp_status_file, monkeypatch, battery_percent=80.5
        )
        assert result["source_health"] == "invalid"
        assert result["battery_percent"] is None

    def test_string_battery_rejected(self, tmp_status_file, monkeypatch):
        """String battery is rejected."""
        result = self._write_and_read(
            tmp_status_file, monkeypatch, battery_percent="80"
        )
        assert result["source_health"] == "invalid"
        assert result["battery_percent"] is None

    def test_list_battery_rejected(self, tmp_status_file, monkeypatch):
        """List battery is rejected."""
        result = self._write_and_read(
            tmp_status_file, monkeypatch, battery_percent=[80]
        )
        assert result["source_health"] == "invalid"
        assert result["battery_percent"] is None

    @pytest.mark.parametrize("value", [-1, 101, 200, -100])
    def test_battery_out_of_range(self, tmp_status_file, monkeypatch, value):
        """Battery outside 0..100 is rejected."""
        result = self._write_and_read(
            tmp_status_file, monkeypatch, battery_percent=value
        )
        assert result["source_health"] == "invalid"
        assert result["battery_percent"] is None

    @pytest.mark.parametrize("value", [0, 1, 50, 99, 100])
    def test_battery_in_range_valid(self, tmp_status_file, monkeypatch, value):
        """Battery 0..100 is accepted."""
        result = self._write_and_read(
            tmp_status_file, monkeypatch, battery_percent=value
        )
        assert result["source_health"] == "fresh"
        assert result["battery_percent"] == value

    def test_bool_presence_rejected(self, tmp_status_file, monkeypatch):
        """Int-as-bool presence (e.g. 1) is rejected."""
        result = self._write_and_read(
            tmp_status_file, monkeypatch, ups_present=1
        )
        assert result["source_health"] == "invalid"
        assert result["ups_present"] is None

    def test_string_presence_rejected(self, tmp_status_file, monkeypatch):
        """String presence is rejected."""
        result = self._write_and_read(
            tmp_status_file, monkeypatch, ups_present="true"
        )
        assert result["source_health"] == "invalid"
        assert result["ups_present"] is None

    def test_list_presence_rejected(self, tmp_status_file, monkeypatch):
        """List presence is rejected."""
        result = self._write_and_read(
            tmp_status_file, monkeypatch, ups_present=[True]
        )
        assert result["source_health"] == "invalid"
        assert result["ups_present"] is None

    @pytest.mark.parametrize("value", ["mains", "battery", "unknown", ""])
    def test_invalid_power_source(self, tmp_status_file, monkeypatch, value):
        """Invalid power_source values are rejected."""
        result = self._write_and_read(
            tmp_status_file, monkeypatch, power_source=value
        )
        assert result["source_health"] == "invalid"
        assert result["power_source"] == "unknown"

    def test_list_power_source_rejected(self, tmp_status_file, monkeypatch):
        """List power_source is rejected."""
        result = self._write_and_read(
            tmp_status_file, monkeypatch, power_source=["ac"]
        )
        assert result["source_health"] == "invalid"
        assert result["power_source"] == "unknown"

    @pytest.mark.parametrize("value", ["paused", "idle", "unknown", ""])
    def test_invalid_monitor_state(self, tmp_status_file, monkeypatch, value):
        """Invalid monitor_state values are rejected."""
        result = self._write_and_read(
            tmp_status_file, monkeypatch, monitor_state=value
        )
        assert result["source_health"] == "invalid"
        assert result["monitor_state"] == "unknown"

    def test_list_monitor_state_rejected(self, tmp_status_file, monkeypatch):
        """List monitor_state is rejected."""
        result = self._write_and_read(
            tmp_status_file, monkeypatch, monitor_state=["running"]
        )
        assert result["source_health"] == "invalid"
        assert result["monitor_state"] == "unknown"


class TestPresenceAndMonitorDistinction:
    """Tests for ups_present=False and monitor_state=stopped distinction."""

    def _write_and_read(self, tmp_status_file, monkeypatch, **overrides):
        base = {
            "power_source": "ups",
            "battery_percent": 80,
            "ups_present": True,
            "monitor_state": "running",
            "observed_at": datetime.now(timezone.utc).isoformat(),
        }
        base.update(overrides)
        tmp_status_file.write_text(json.dumps(base))
        monkeypatch.setattr(app_module, "UPS_STATUS_FILE", tmp_status_file)
        now = datetime.now(timezone.utc)
        return app_module._read_ups_status(now)

    def test_false_presence_valid_fresh(self, tmp_status_file, monkeypatch):
        """ups_present=False with valid data => fresh (UPS absent, not healthy)."""
        result = self._write_and_read(
            tmp_status_file, monkeypatch, ups_present=False
        )
        assert result["source_health"] == "fresh"
        assert result["source_fresh"] is True
        assert result["ups_present"] is False

    def test_stopped_monitor_still_fresh(self, tmp_status_file, monkeypatch):
        """monitor_state=stopped with fresh timestamp => source_fresh True.

        source_fresh is about timestamp freshness, separate from monitor health.
        """
        result = self._write_and_read(
            tmp_status_file, monkeypatch, monitor_state="stopped"
        )
        assert result["source_health"] == "fresh"
        assert result["source_fresh"] is True
        assert result["monitor_state"] == "stopped"

    def test_do_not_guess_running_from_timestamp(self, tmp_status_file, monkeypatch):
        """A fresh timestamp does NOT imply monitor_state=running."""
        # monitor_state is "stopped" but timestamp is fresh
        result = self._write_and_read(
            tmp_status_file, monkeypatch, monitor_state="stopped"
        )
        assert result["monitor_state"] == "stopped"
        assert result["source_fresh"] is True
        # source_fresh is True but monitor is stopped - they are separate
        assert result["monitor_state"] != "running"


class TestLastEventAndSecrets:
    """Tests for last_event bounding and secret/extra field handling."""

    def _write_and_read(self, tmp_status_file, monkeypatch, **overrides):
        base = {
            "power_source": "ups",
            "battery_percent": 80,
            "ups_present": True,
            "monitor_state": "running",
            "observed_at": datetime.now(timezone.utc).isoformat(),
        }
        base.update(overrides)
        tmp_status_file.write_text(json.dumps(base))
        monkeypatch.setattr(app_module, "UPS_STATUS_FILE", tmp_status_file)
        now = datetime.now(timezone.utc)
        return app_module._read_ups_status(now)

    def test_last_event_truncated_to_500(self, tmp_status_file, monkeypatch):
        """last_event longer than 500 chars is truncated."""
        long_event = "x" * 1000
        result = self._write_and_read(
            tmp_status_file, monkeypatch, last_event=long_event
        )
        assert result["last_event"] == "x" * 500
        assert len(result["last_event"]) == 500

    def test_last_event_exactly_500(self, tmp_status_file, monkeypatch):
        """last_event of exactly 500 chars is preserved."""
        event = "y" * 500
        result = self._write_and_read(
            tmp_status_file, monkeypatch, last_event=event
        )
        assert result["last_event"] == event

    def test_last_event_short_preserved(self, tmp_status_file, monkeypatch):
        """Short last_event is preserved as-is."""
        result = self._write_and_read(
            tmp_status_file, monkeypatch, last_event="battery low"
        )
        assert result["last_event"] == "battery low"

    def test_last_event_non_string_ignored(self, tmp_status_file, monkeypatch):
        """Non-string last_event is ignored (None)."""
        result = self._write_and_read(
            tmp_status_file, monkeypatch, last_event=12345
        )
        assert result["last_event"] is None

    def test_last_event_list_ignored(self, tmp_status_file, monkeypatch):
        """List last_event is ignored (None)."""
        result = self._write_and_read(
            tmp_status_file, monkeypatch, last_event=["event1", "event2"]
        )
        assert result["last_event"] is None

    def test_secret_field_not_leaked(self, tmp_status_file, monkeypatch):
        """Secret fields in raw JSON are not in the output."""
        result = self._write_and_read(
            tmp_status_file, monkeypatch,
            api_secret="super_secret_value",
            db_password="hunter2",
        )
        assert "api_secret" not in result
        assert "db_password" not in result
        assert "super_secret_value" not in json.dumps(result)
        assert "hunter2" not in json.dumps(result)

    def test_raw_path_not_leaked(self, tmp_status_file, monkeypatch):
        """Raw file path is not in the output."""
        result = self._write_and_read(
            tmp_status_file, monkeypatch,
            source_path="/etc/secret/ups.json",
        )
        assert "source_path" not in result
        assert "/etc/secret/ups.json" not in json.dumps(result)

    def test_html_in_last_event_truncated_not_rendered(self, tmp_status_file, monkeypatch):
        """HTML in last_event is truncated to 500 chars (plain string)."""
        html_event = "<script>alert('xss')</script>" + "A" * 600
        result = self._write_and_read(
            tmp_status_file, monkeypatch, last_event=html_event
        )
        assert len(result["last_event"]) == 500
        # It's a plain string, not rendered HTML
        assert isinstance(result["last_event"], str)


# ---------------------------------------------------------------------------
# Focused integration tests for the /api/ups-status endpoint.
#
# These use the real FastAPI app (TestClient), the real /api/login session
# cookie flow, and the real create_password_record / temp USERS_FILE patterns
# from tests/test_user_admin_api.py. They do NOT inject request.state or
# bypass middleware. Temp USERS_FILE / AUDIT_LOG_FILE / UPS_STATUS_FILE are
# isolated, and sessions + login attempts are cleared before and after each
# test under the appropriate locks. All fixtures are synthetic (no sensors,
# no real credentials).
# ---------------------------------------------------------------------------

import pytest
from fastapi.testclient import TestClient


def _seed_ups_file(ups_file, *, fresh=True, extra=None):
    """Write a synthetic UPS status file (no sensors, no real credentials)."""
    if fresh:
        observed_at = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    else:
        observed_at = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
    payload = {
        "power_source": "ac",
        "battery_percent": 100,
        "ups_present": True,
        "monitor_state": "running",
        "observed_at": observed_at,
        "last_event": "ac power restored",
    }
    if extra:
        payload.update(extra)
    ups_file.write_text(json.dumps(payload), encoding="utf-8")
    return payload


def _login(client, username, password):
    """Real /api/login session cookie flow (no request.state injection)."""
    return client.post(
        "/api/login",
        json={"username": username, "password": password},
    )


@pytest.fixture()
def ups_env(tmp_path):
    users_file = tmp_path / "users.json"
    audit_file = tmp_path / "audit.jsonl"
    ups_file = tmp_path / "ups-status.json"

    orig_users = app_module.USERS_FILE
    orig_audit = app_module.AUDIT_LOG_FILE
    orig_ups = app_module.UPS_STATUS_FILE

    app_module.USERS_FILE = users_file
    app_module.AUDIT_LOG_FILE = audit_file
    app_module.UPS_STATUS_FILE = ups_file

    with app_module._users_lock:
        users = app_module.load_users_unlocked()
        users["admin"] = app_module.create_password_record("admin-pass-123", "admin")
        users["operator"] = app_module.create_password_record("operator-pass-123", "operator")
        users["viewer"] = app_module.create_password_record("viewer-pass-123", "viewer")
        app_module.save_users_atomic(users)

    # Clear sessions + login attempts BEFORE the test (under locks).
    with app_module._session_lock:
        app_module._sessions.clear()
    with app_module._login_rate_lock:
        app_module._login_attempts.clear()

    client = TestClient(app_module.app)

    yield {
        "client": client,
        "users_file": users_file,
        "audit_file": audit_file,
        "ups_file": ups_file,
        "users": users,
    }

    # Clear sessions + login attempts AFTER the test (under locks).
    with app_module._session_lock:
        app_module._sessions.clear()
    with app_module._login_rate_lock:
        app_module._login_attempts.clear()

    app_module.USERS_FILE = orig_users
    app_module.AUDIT_LOG_FILE = orig_audit
    app_module.UPS_STATUS_FILE = orig_ups


def test_anonymous_get_ups_status_401(ups_env):
    client = ups_env["client"]
    resp = client.get("/api/ups-status")
    assert resp.status_code == 401


@pytest.mark.parametrize("role", ["admin", "operator", "viewer"])
def test_authenticated_get_ups_status_fresh(ups_env, role):
    client = ups_env["client"]
    _seed_ups_file(ups_env["ups_file"], fresh=True)

    login_resp = _login(client, role, f"{role}-pass-123")
    assert login_resp.status_code == 200

    resp = client.get("/api/ups-status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["source_health"] == "fresh"
    assert body["source_fresh"] is True
    assert body["stale"] is False
    assert body["power_source"] == "ac"
    assert body["battery_percent"] == 100
    assert body["ups_present"] is True
    assert body["monitor_state"] == "running"


def test_get_ups_status_does_not_modify_source(ups_env):
    client = ups_env["client"]
    _seed_ups_file(ups_env["ups_file"], fresh=True)
    before = ups_env["ups_file"].read_bytes()

    assert _login(client, "admin", "admin-pass-123").status_code == 200
    resp = client.get("/api/ups-status")
    assert resp.status_code == 200

    assert ups_env["ups_file"].read_bytes() == before


def test_get_ups_status_excludes_secrets_and_paths(ups_env):
    client = ups_env["client"]
    _seed_ups_file(
        ups_env["ups_file"],
        fresh=True,
        extra={
            "secret_token": "TOPSECRET-DO-NOT-LEAK-12345",
            "host_path": "/etc/shadow",
            "raw_error": "internal-error-detail-abc",
        },
    )

    assert _login(client, "admin", "admin-pass-123").status_code == 200
    resp = client.get("/api/ups-status")
    assert resp.status_code == 200
    body = resp.json()
    text = resp.text
    assert "secret_token" not in body
    assert "host_path" not in body
    assert "raw_error" not in body
    assert "TOPSECRET-DO-NOT-LEAK-12345" not in text
    assert "/etc/shadow" not in text
    assert "internal-error-detail-abc" not in text


def test_missing_source_safe_for_logged_in(ups_env):
    client = ups_env["client"]
    assert not ups_env["ups_file"].exists()
    assert _login(client, "viewer", "viewer-pass-123").status_code == 200
    resp = client.get("/api/ups-status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["source_health"] == "missing"
    assert body["power_source"] == "unknown"
    assert body["monitor_state"] == "unknown"


def test_malformed_source_safe_for_logged_in(ups_env):
    client = ups_env["client"]
    ups_env["ups_file"].write_text("{not valid json", encoding="utf-8")
    assert _login(client, "viewer", "viewer-pass-123").status_code == 200
    resp = client.get("/api/ups-status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["source_health"] == "malformed"


def test_authenticated_post_ups_status_denied(ups_env):
    client = ups_env["client"]
    _seed_ups_file(ups_env["ups_file"], fresh=True)
    before = ups_env["ups_file"].read_bytes()

    assert _login(client, "admin", "admin-pass-123").status_code == 200
    resp = client.post("/api/ups-status", json={"power_source": "ac"})
    assert resp.status_code in (401, 403, 405)

    assert ups_env["ups_file"].read_bytes() == before
