import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import server


class UrlValidationTests(unittest.TestCase):
    def test_youtube_playlist(self):
        source, value = server.classify_url(
            "https://www.youtube.com/playlist?list=PL123"
        )
        self.assertEqual(source, "youtube")
        self.assertIn("PL123", value)

    def test_spotify_album(self):
        source, _ = server.classify_url("https://open.spotify.com/album/abc123")
        self.assertEqual(source, "spotify")

    def test_rejects_unknown_hosts(self):
        with self.assertRaises(ValueError):
            server.classify_url("https://example.com/watch?v=123")

    def test_rejects_http(self):
        with self.assertRaises(ValueError):
            server.classify_url("http://youtube.com/watch?v=123")


class FileScanTests(unittest.TestCase):
    def test_only_returns_mp3_files(self):
        job_id = "a" * 32
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(server, "DOWNLOAD_ROOT", root):
                job_dir = root / job_id
                job_dir.mkdir()
                (job_dir / "track.mp3").write_bytes(b"audio")
                (job_dir / "notes.txt").write_text("ignore")
                files = server.scan_audio_files(job_id)
        self.assertEqual([item["name"] for item in files], ["track.mp3"])


if __name__ == "__main__":
    unittest.main()

