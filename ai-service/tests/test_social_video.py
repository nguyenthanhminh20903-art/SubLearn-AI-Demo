import unittest
from io import BytesIO
from pathlib import Path
from subprocess import CompletedProcess
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.error import HTTPError

from app.media import download_video
from app.social_video import VideoDownloadError, parse_video_url, resolve_short_link


class SocialVideoTests(unittest.TestCase):
    def test_public_video_platforms(self):
        cases = {
            'https://www.facebook.com/reel/12345': 'Facebook',
            'https://www.facebook.com/example/posts/pfbid0123abc': 'Facebook',
            'https://www.tiktok.com/@user/video/7012345678901234567': 'TikTok',
            'https://www.iesdouyin.com/share/video/7012345678901234567/': 'Douyin',
            'https://www.youtube.com/shorts/8RI9ShcQ-h8?si=test': 'YouTube',
            'https://www.instagram.com/reel/Cd123Ab/': 'Instagram',
            'https://x.com/account/status/123456/video/1': 'X',
            'https://www.bilibili.com/video/BV1sC411G7gf': 'Bilibili',
            'https://player.vimeo.com/video/12345678?h=abc123': 'Vimeo',
            'https://dai.ly/x8vfafk': 'Dailymotion',
        }
        for url, platform in cases.items():
            with self.subTest(url=url):
                self.assertEqual(parse_video_url(url).platform, platform)

    def test_rejects_untrusted_hosts_and_non_video_urls(self):
        for url in (
            'https://www.facebook.com.example.test/reel/1', 'https://user@www.facebook.com/reel/1',
            'https://127.0.0.1/video/1', 'file:///video.mp4',
            'https://www.youtube.com/playlist?list=PL123', 'https://www.tiktok.com/@username',
        ):
            with self.subTest(url=url):
                self.assertIsNone(parse_video_url(url))

    def test_normalizes_social_share_variants(self):
        facebook = parse_video_url('https://m.facebook.com/story.php?story_fbid=12345&id=67890&ref=share')
        self.assertEqual(facebook.url, 'https://www.facebook.com/story.php?story_fbid=12345&id=67890')
        tiktok = parse_video_url('https://www.tiktokv.com/share/video/7668090902816017671/?ref=share')
        self.assertEqual(tiktok.url, 'https://www.tiktokv.com/share/video/7668090902816017671')
        self.assertEqual(tiktok.extractors, '^tiktok$')

    @patch('app.social_video.build_opener')
    def test_short_links_resolve_only_within_the_same_platform(self, build):
        source = parse_video_url('https://v.douyin.com/abc123/')
        open_url = build.return_value.open
        open_url.side_effect = HTTPError(
            source.url, 302, '', {'Location': 'https://www.iesdouyin.com/share/video/12345/'}, BytesIO(),
        )
        self.assertEqual(resolve_short_link(source).url, 'https://www.douyin.com/video/12345')
        open_url.side_effect = HTTPError(
            source.url, 302, '', {'Location': 'http://127.0.0.1:8001/'}, BytesIO(),
        )
        open_url.reset_mock()
        with self.assertRaisesRegex(VideoDownloadError, 'Douyin'):
            resolve_short_link(source)
        self.assertEqual(open_url.call_count, 1)

    @patch('app.media.subprocess.run')
    def test_download_limits_extractors_and_hides_upstream_error(self, run):
        run.return_value.returncode = 1
        run.return_value.stderr = 'upstream detail'
        with TemporaryDirectory() as directory:
            with self.assertRaisesRegex(VideoDownloadError, 'Không tải được video từ Facebook'):
                download_video('https://www.facebook.com/reel/12345', Path(directory))
        command = run.call_args.args[0]
        self.assertEqual(command[command.index('--use-extractors') + 1], '^facebook$|^facebook:reel$')
        self.assertEqual(command[-2:], ['--', 'https://www.facebook.com/reel/12345'])
        self.assertNotIn('generic', command)

    @patch('app.media.subprocess.run')
    def test_reports_tiktok_web_response_without_exposing_raw_diagnostic(self, run):
        run.return_value.returncode = 1
        run.return_value.stderr = 'Unexpected response from webpage request: private diagnostic'
        with TemporaryDirectory() as directory:
            with self.assertRaisesRegex(VideoDownloadError, 'Máy chủ chưa lấy được tệp video từ TikTok') as failure:
                download_video('https://www.tiktok.com/@fixture/video/12345', Path(directory))
        self.assertNotIn('private diagnostic', str(failure.exception))

    @patch('app.media.subprocess.run')
    def test_reports_permission_and_dns_before_blame_on_the_link(self, run):
        run.return_value.returncode = 1
        url = 'https://www.facebook.com/reel/12345'
        for diagnostic, expected in (
            ('[WinError 10013] Attempt to access a socket was forbidden', 'bị chặn kết nối HTTPS'),
            ('curl: (28) Resolving timed out after 9000 milliseconds', 'DNS'),
            ('certificate_verify_failed', 'chứng chỉ HTTPS'),
        ):
            with self.subTest(diagnostic=diagnostic), TemporaryDirectory() as directory:
                run.return_value.stderr = diagnostic
                with self.assertRaisesRegex(VideoDownloadError, expected):
                    download_video(url, Path(directory))

    @patch('app.media.subprocess.run')
    @patch('app.media.shutil.which')
    @patch('imageio_ffmpeg.get_ffmpeg_exe')
    def test_passes_bundled_ffmpeg_to_download_merger(self, get_ffmpeg, which, run):
        bundled = 'C:/local/binaries/ffmpeg-win.exe'
        get_ffmpeg.return_value = bundled
        which.side_effect = lambda executable: bundled if executable == bundled else None
        run.return_value.returncode = 1
        run.return_value.stderr = 'upstream detail'
        with TemporaryDirectory() as directory, self.assertRaises(VideoDownloadError):
            download_video('https://www.facebook.com/reel/12345', Path(directory))
        command = run.call_args.args[0]
        self.assertEqual(command[command.index('--ffmpeg-location') + 1], bundled)
        self.assertIn('bestvideo[height<=720]+bestaudio', command[command.index('-f')+1])

    @patch('app.media.probe_media')
    @patch('app.media.subprocess.run')
    def test_retries_an_unreachable_cdn_in_a_fresh_output_directory(self, run, probe):
        def attempt(command, **options):
            path = Path(command[command.index('-o') + 1].replace('%(ext)s', 'mp4'))
            if '--check-formats' not in command:
                path.with_suffix('.mp4.part').write_bytes(b'first candidate')
                return CompletedProcess(command, 1, '', '[download] Connection to the CDN timed out')
            self.assertEqual(path.parent.name, 'fallback')
            self.assertFalse(path.with_suffix('.mp4.part').exists())
            path.write_bytes(b'working candidate')
            return CompletedProcess(command, 0, '', '')

        run.side_effect = attempt
        with TemporaryDirectory() as directory:
            media = download_video('https://www.tiktok.com/@fixture/video/12345', Path(directory))
            self.assertEqual(media.read_bytes(), b'working candidate')
            probe.assert_called_once_with(media)
        self.assertEqual(run.call_count, 2)
