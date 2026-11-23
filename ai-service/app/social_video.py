import re
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


SUPPORTED_PLATFORMS = ('Facebook', 'TikTok', 'Douyin', 'YouTube', 'Instagram', 'X', 'Bilibili', 'Vimeo', 'Dailymotion')
INVALID_VIDEO_URL = 'Nhập đường dẫn đến một video trên ' + ', '.join(SUPPORTED_PLATFORMS) + '.'


@dataclass(frozen=True)
class VideoSource:
    platform: str
    url: str
    extractors: str
    short_link: bool = False


class VideoDownloadError(RuntimeError):
    pass


def download_network_error(platform, detail):
    detail = str(detail).lower()
    if any(marker in detail for marker in ('winerror 10013', 'socket in a way forbidden', 'operation not permitted')):
        return f'Tiến trình SubLearn AI bị chặn kết nối HTTPS tới {platform}. Thử lại khi backend có quyền truy cập mạng.'
    if any(marker in detail for marker in ('resolving timed out', 'could not resolve host', 'getaddrinfo failed',
                                          'name resolution', 'winerror 11001', 'nodename nor servname')):
        return f'Máy chủ không phân giải được địa chỉ {platform} qua DNS. Kiểm tra kết nối mạng rồi thử lại.'
    if any(marker in detail for marker in ('certificate_verify_failed', 'certificate verify failed',
                                          'self signed certificate', 'ssl certificate problem')):
        return f'Máy chủ không xác thực được chứng chỉ HTTPS của {platform}. Kiểm tra kết nối và chứng chỉ hệ thống.'
    if any(marker in detail for marker in ('timed out',
                                          'winerror 10060', 'network is unreachable', 'failed to connect')):
        return f'Máy chủ chưa kết nối được tới {platform}. Thử lại sau hoặc tải tệp vào dự án.'
    return None


def parse_video_url(value):
    if not isinstance(value, str) or not 0 < len(value) <= 2083 or any(
        char.isspace() or ord(char) < 32 for char in value
    ):
        return None
    try:
        url = urlsplit(value)
    except ValueError:
        return None
    host = url.hostname
    if not host or url.scheme not in ('http', 'https') or url.netloc.lower() != host or '\\' in value:
        return None
    path = url.path.rstrip('/')
    query = parse_qs(url.query)

    if host in ('youtube.com', 'www.youtube.com', 'm.youtube.com', 'youtu.be'):
        video = None
        if host == 'youtu.be':
            video = path.removeprefix('/')
        elif path == '/watch':
            video = query.get('v', [''])[0]
        else:
            match = re.fullmatch(r'/(?:shorts|embed|live)/([\w-]+)', path)
            if match:
                video = match[1]
        if video and re.fullmatch(r'[A-Za-z0-9_-]{11}', video):
            return VideoSource('YouTube', f'https://www.youtube.com/watch?v={video}', '^youtube$')

    if host in ('facebook.com', 'www.facebook.com', 'm.facebook.com', 'web.facebook.com', 'fb.watch'):
        if (host == 'fb.watch' and re.fullmatch(r'/[A-Za-z0-9_-]+', path)) or (
            host != 'fb.watch' and re.fullmatch(r'/share/(?:[prv]/)?[A-Za-z0-9_-]+', path)
        ):
            return VideoSource('Facebook', f'https://{host}{path}', '^facebook$|^facebook:reel$', True)
        if host != 'fb.watch':
            if path in ('/story.php', '/permalink.php'):
                story = query.get('story_fbid', [''])[0]
                owner = query.get('id', [''])[0]
                if re.fullmatch(r'(?:[0-9]+|pfbid[A-Za-z0-9]+)', story) and (not owner or re.fullmatch(r'[0-9]+', owner)):
                    suffix = f'&id={owner}' if owner else ''
                    return VideoSource('Facebook', f'https://www.facebook.com{path}?story_fbid={story}{suffix}', '^facebook$')
            video = query.get('v', query.get('video_id', ['']))[0]
            bare_video = re.fullmatch(r'/videos/([0-9]+)', path)
            if bare_video:
                video = bare_video[1]
            if (bare_video or path in ('/watch', '/video.php', '/video/video.php', '/video/embed')) and re.fullmatch(r'[0-9]+', video):
                return VideoSource('Facebook', f'https://www.facebook.com/watch/?v={video}', '^facebook$')
            if re.fullmatch(r'/reel/[0-9]+|/[A-Za-z0-9_.-]+/videos/(?:[A-Za-z0-9_.-]+/)?[0-9]+', path):
                return VideoSource('Facebook', f'https://www.facebook.com{path}', '^facebook$|^facebook:reel$')
            if re.fullmatch(r'/(?:[A-Za-z0-9_.-]+/posts|groups/[A-Za-z0-9_.-]+/(?:posts|permalink))'
                            r'/(?:pfbid[A-Za-z0-9]+|[0-9]+)', path):
                return VideoSource('Facebook', f'https://www.facebook.com{path}', '^facebook$')

    if host in ('tiktok.com', 'www.tiktok.com', 'm.tiktok.com', 'vm.tiktok.com', 'vt.tiktok.com', 'www.tiktokv.com'):
        if (host in ('vm.tiktok.com', 'vt.tiktok.com') and re.fullmatch(r'/[A-Za-z0-9_-]+', path)) or (
            host in ('tiktok.com', 'www.tiktok.com') and re.fullmatch(r'/t/[A-Za-z0-9_-]+', path)
        ):
            return VideoSource('TikTok', f'https://{host}{path}', '^tiktok$', True)
        if host in ('tiktok.com', 'www.tiktok.com', 'm.tiktok.com', 'www.tiktokv.com') and re.fullmatch(
            r'/(?:@[\w.-]+/video|share/video|embed)/[0-9]+', path
        ):
            canonical_host = 'www.tiktokv.com' if host == 'www.tiktokv.com' else 'www.tiktok.com'
            return VideoSource('TikTok', f'https://{canonical_host}{path}', '^tiktok$')

    if host in ('douyin.com', 'www.douyin.com', 'v.douyin.com', 'www.iesdouyin.com', 'iesdouyin.com'):
        if host == 'v.douyin.com' and re.fullmatch(r'/[A-Za-z0-9_-]+', path):
            return VideoSource('Douyin', f'https://{host}{path}', '^douyin$', True)
        match = re.fullmatch(r'/(?:share/)?video/([0-9]+)', path)
        if host != 'v.douyin.com' and match:
            return VideoSource('Douyin', f'https://www.douyin.com/video/{match[1]}', '^douyin$')
        modal = query.get('modal_id', [''])[0]
        if host in ('douyin.com', 'www.douyin.com') and re.fullmatch(r'[0-9]+', modal):
            return VideoSource('Douyin', f'https://www.douyin.com/video/{modal}', '^douyin$')

    if host in ('instagram.com', 'www.instagram.com'):
        if re.fullmatch(r'/(?:p|reel|reels|tv)/[A-Za-z0-9_-]+', path):
            return VideoSource('Instagram', f'https://www.instagram.com{path}/', '^instagram$')
        if re.fullmatch(r'/share/(?:reel/|p/)?[A-Za-z0-9_-]+', path):
            return VideoSource('Instagram', f'https://{host}{path}/', '^instagram$', True)

    if host in ('x.com', 'www.x.com', 'twitter.com', 'www.twitter.com', 'mobile.twitter.com') and re.fullmatch(
        r'/(?:[A-Za-z0-9_]+|i/web)/status/[0-9]+(?:/video/[0-9]+)?', path
    ):
        return VideoSource('X', f'https://x.com{path}', '^twitter$|^twitter:card$|^twitter:amplify$')

    if host == 'b23.tv' and re.fullmatch(r'/[A-Za-z0-9]+', path):
        return VideoSource('Bilibili', f'https://{host}{path}', '^bilibili$', True)
    if host in ('bilibili.com', 'www.bilibili.com', 'm.bilibili.com') and re.fullmatch(r'/video/(?:BV[A-Za-z0-9]+|av[0-9]+)', path):
        return VideoSource('Bilibili', f'https://www.bilibili.com{path}', '^bilibili$')

    if host in ('vimeo.com', 'www.vimeo.com', 'player.vimeo.com') and re.fullmatch(
        r'/(?:video/)?[0-9]+(?:/[A-Za-z0-9]+)?', path
    ):
        access_hash = query.get('h', [''])[0]
        suffix = f'?h={access_hash}' if re.fullmatch(r'[A-Za-z0-9]+', access_hash) else ''
        return VideoSource('Vimeo', f'https://{host}{path}{suffix}', '^vimeo$')
    if host in ('dailymotion.com', 'www.dailymotion.com', 'dai.ly'):
        prefix = '/' if host == 'dai.ly' else '/video/'
        match = re.fullmatch(prefix + r'([A-Za-z0-9]+)(?:_[^/]*)?', path)
        if match:
            return VideoSource('Dailymotion', f'https://www.dailymotion.com/video/{match[1]}', '^dailymotion$')
    return None


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def resolve_short_link(source):
    opener = build_opener(NoRedirect())
    for _ in range(6):
        if not source.short_link:
            return source
        req = Request(source.url, headers={'User-Agent': 'Mozilla/5.0'}, method='GET')
        try:
            with opener.open(req, timeout=10):
                break
        except HTTPError as error:
            with error:
                if error.code not in (301, 302, 303, 307, 308) or not error.headers.get('Location'):
                    break
                next_source = parse_video_url(urljoin(source.url, error.headers['Location']))
                if not next_source or next_source.platform != source.platform:
                    break
                source = next_source
                if not source.short_link:
                    return source
        except (URLError, TimeoutError) as error:
            message = download_network_error(source.platform, error)
            if message:
                raise VideoDownloadError(message) from None
            break
    raise VideoDownloadError(f'Không mở được link chia sẻ {source.platform}. Hãy thử URL video đầy đủ hoặc tải tệp lên.')
