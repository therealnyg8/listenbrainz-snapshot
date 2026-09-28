import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from email.message import Message

import snapshot as s


def listen(timestamp, artist="Artist", track="Track", album="Album", client="Roon"):
    return {"listened_at": timestamp, "track_metadata": {
        "artist_name": artist, "track_name": track, "release_name": album,
        "additional_info": {"submission_client": client}}}


class SnapshotTests(unittest.TestCase):
    def test_duplicate_imports_merge_but_repeated_plays_remain(self):
        result = s.normalize([listen(100), listen(100, client="Last.fm"), listen(200)])
        self.assertEqual(len(result), 2)
        self.assertEqual(set(result[1]["submission_clients"]), {"Roon", "Last.fm"})

    def test_identical_names_at_different_times_and_missing_albums(self):
        result = s.aggregate(s.normalize([listen(100), listen(200), listen(300, album=None)]))
        self.assertEqual(result["listen_count"], 3)
        self.assertEqual(result["albums"][0]["listen_count"], 2)
        self.assertEqual(result["albums"][0]["unique_tracks"], 1)
        self.assertEqual(result["listens_without_album"], 1)

    @patch("snapshot.time.sleep")
    def test_boundary_second_is_refetched_and_deduplicated(self, sleep):
        pages = [[listen(100), listen(99)], [listen(99), listen(99, track="Other")], [listen(98)]]
        cursors = []
        def fetch(params):
            cursors.append(params["max_ts"])
            # A full bucket at one second must fail instead of silently skipping.
            return {"payload": {"listens": pages.pop(0)}}
        with patch.object(s, "PAGE_SIZE", 2):
            with self.assertRaisesRegex(RuntimeError, "cannot advance"):
                s.fetch_recent(100, fetch)
        self.assertEqual(cursors, [101, 100])

    @patch("snapshot.time.sleep")
    def test_pagination_overlap_cutoff_and_complete(self, sleep):
        now = s.DAYS * 86400 + 50
        pages = [[listen(now), listen(now - 1)], [listen(now - 1), listen(49)]]
        with patch.object(s, "PAGE_SIZE", 2):
            records, complete, count = s.fetch_recent(now, lambda params: {"payload": {"listens": pages.pop(0)}})
        self.assertTrue(complete)
        self.assertEqual(count, 2)
        self.assertEqual(len(records), 2)

    @patch("snapshot.time.sleep")
    def test_page_limit_marks_incomplete(self, sleep):
        with patch.object(s, "PAGE_SIZE", 2), patch.object(s, "MAX_PAGES", 1):
            records, complete, _ = s.fetch_recent(100, lambda params: {"payload": {"listens": [listen(100), listen(99)]}})
        self.assertFalse(complete)
        self.assertEqual(len(records), 2)

    def test_empty_account_is_valid(self):
        records, complete, _ = s.fetch_recent(100, lambda params: {"payload": {"listens": []}})
        result = s.build_snapshot(records, complete, 1, 100)
        self.assertEqual(result["recent_listens"], [])
        self.assertIsNone(result["coverage"]["newest_listen_utc"])

    def test_window_counts_and_schema(self):
        now = s.DAYS * 86400
        records = [listen(now), listen(now - 8 * 86400), listen(now - 31 * 86400)]
        result = s.build_snapshot(records, True, 1, now)
        self.assertEqual([result["aggregates"][f"last_{d}_days"]["listen_count"] for d in (7, 30, 90)], [1, 2, 3])
        self.assertEqual(json.loads(json.dumps(result))["schema_version"], 1)

    def test_network_failure_preserves_existing_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            original = Path(directory) / f"{s.USER}.json"
            original.write_text('{"previous": true}')
            with patch("snapshot.fetch_recent", side_effect=RuntimeError("offline")), patch("snapshot.Path", return_value=original):
                with self.assertRaises(RuntimeError):
                    s.main()
            self.assertEqual(original.read_text(), '{"previous": true}')

    @patch("snapshot.time.sleep")
    def test_rate_limit_retry_and_no_authorization(self, sleep):
        headers = Message()
        headers["Retry-After"] = "2"
        response = unittest.mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"payload": {"listens": []}}'
        response.__enter__.return_value.headers = {}
        error = HTTPError(s.API, 429, "rate limited", headers, None)
        with patch("snapshot.urlopen", side_effect=[error, response]) as open_url:
            result = s.request_json({"count": 1})
        request = open_url.call_args.args[0]
        self.assertTrue(request.get_header("User-agent").startswith("ListenBrainzSnapshot/1.0"))
        self.assertIsNone(request.get_header("Authorization"))
        sleep.assert_called_once_with(3)
        self.assertEqual(result["payload"]["listens"], [])


if __name__ == "__main__":
    unittest.main()
