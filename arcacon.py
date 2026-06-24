from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse


ARCA_URL_RE = re.compile(r"^https?://(?:www\.)?arca\.live/e/\d+/?(?:[?#].*)?$")
MP4_URL_RE = re.compile(r"https?://[^\s\"'<>]+\.mp4(?:\?[^\s\"'<>]*)?", re.I)
IMAGE_URL_RE = re.compile(
    r"https?://(?:[^/\s\"'<>]+\.)?(?:namu\.la|arca\.live)/"
    r"[^\s\"'<>]+\.(?:png|jpe?g|webp|gif)(?:\?[^\s\"'<>]*)?",
    re.I,
)
INVALID_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
LOCAL_APP_DATA = Path(os.environ.get("LOCALAPPDATA", Path.home()))
BROWSER_CANDIDATES = (
    (
        "Brave",
        "chrome",
        (
            LOCAL_APP_DATA / r"BraveSoftware\Brave-Browser\Application\brave.exe",
            Path(r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"),
            Path(
                r"C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe"
            ),
        ),
    ),
    (
        "Chrome",
        "chrome",
        (
            LOCAL_APP_DATA / r"Google\Chrome\Application\chrome.exe",
            Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
            Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
        ),
    ),
    (
        "Edge",
        "edge",
        (
            LOCAL_APP_DATA / r"Microsoft\Edge\Application\msedge.exe",
            Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
            Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        ),
    ),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="아카콘 링크에서 영상과 이미지를 찾아 GIF로 변환합니다."
    )
    parser.add_argument("url", help="예: https://arca.live/e/52927")
    parser.add_argument(
        "-o", "--output", type=Path, default=Path("download"), help="출력 폴더"
    )
    parser.add_argument("--fps", type=int, default=15, help="GIF FPS (기본: 15)")
    parser.add_argument(
        "--width", type=int, default=512, help="GIF 최대 너비 (기본: 512)"
    )
    parser.add_argument(
        "--wait", type=float, default=0.08, help="각 요소 hover 대기 시간"
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=min(6, max(2, os.cpu_count() or 2)),
        help="동시 다운로드·변환 작업 수 (기본: CPU 기준 자동)",
    )
    parser.add_argument(
        "--keep-source",
        "--keep-mp4",
        dest="keep_source",
        action="store_true",
        help="변환 후 원본 영상·이미지 유지",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="브라우저를 숨겨 실행 (Cloudflare에서 실패할 수 있음)",
    )
    return parser.parse_args()


def require_environment() -> tuple[str, str, str, Path]:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg를 찾지 못했습니다. ffmpeg를 PATH에 추가하세요.")

    browser = None
    for name, driver_kind, paths in BROWSER_CANDIDATES:
        executable = next((path for path in paths if path.exists()), None)
        if executable is not None:
            browser = (name, driver_kind, executable)
            break
    if browser is None:
        raise RuntimeError("Brave, Chrome, Edge 중 설치된 브라우저를 찾지 못했습니다.")

    try:
        import selenium  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "selenium이 없습니다. 먼저 다음 명령을 실행하세요:\n"
            "  python -m pip install -r requirements.txt"
        ) from exc

    return ffmpeg, *browser


def extract_mp4_urls(value: object) -> list[str]:
    if not isinstance(value, str):
        return []
    return [match.rstrip("),]") for match in MP4_URL_RE.findall(value)]


def extract_image_urls(value: object) -> list[str]:
    if not isinstance(value, str):
        return []
    return [match.rstrip("),]") for match in IMAGE_URL_RE.findall(value)]


def install_mp4_observer(driver) -> set[str]:
    urls = driver.execute_script(
        r"""
        window.__arcaconMp4Urls = new Set();
        const found = window.__arcaconMp4Urls;
        const add = value => {
          if (typeof value !== "string") return;
          const matches = value.match(/https?:\/\/[^\s"'<>]+\.mp4(?:\?[^\s"'<>]*)?/ig);
          if (matches) matches.forEach(url => found.add(url));
        };

        performance.getEntriesByType("resource").forEach(entry => add(entry.name));
        document.querySelectorAll("video, source").forEach(element => {
          add(element.currentSrc);
          add(element.src);
        });
        window.__arcaconMp4Observer?.disconnect();
        window.__arcaconMp4Observer = new PerformanceObserver(list => {
          list.getEntries().forEach(entry => add(entry.name));
        });
        window.__arcaconMp4Observer.observe({type: "resource", buffered: true});
        return [...found];
        """
    )
    return set(urls)


def read_observed_mp4_urls(driver) -> set[str]:
    urls = driver.execute_script(
        r"""
        const found = window.__arcaconMp4Urls || new Set();
        const add = value => {
          if (typeof value !== "string") return;
          const matches = value.match(/https?:\/\/[^\s"'<>]+\.mp4(?:\?[^\s"'<>]*)?/ig);
          if (matches) matches.forEach(url => found.add(url));
        };
        document.querySelectorAll("video, source").forEach(element => {
          add(element.currentSrc);
          add(element.src);
        });
        return [...found];
        """
    )
    return set(urls)


def collect_image_urls_for_elements(driver, elements: list) -> list[set[str]]:
    values_by_element = driver.execute_script(
        """
        return arguments[0].map(root => {
          const values = [];
          const add = value => {
            if (typeof value === 'string' && value) values.push(value);
          };
          const inspect = element => {
            add(element.currentSrc);
            add(element.src);
            add(element.getAttribute?.('src'));
            add(element.getAttribute?.('data-src'));
            add(element.getAttribute?.('data-original'));
            add(element.getAttribute?.('poster'));
            add(getComputedStyle(element).backgroundImage);
          };
          inspect(root);
          root.querySelectorAll?.('img, source, video').forEach(inspect);
          return values;
        });
        """,
        elements,
    )
    result: list[set[str]] = []
    for values in values_by_element:
        urls: set[str] = set()
        for value in values:
            urls.update(extract_image_urls(value))
        result.append(urls)
    return result


def trigger_hover(driver, element) -> None:
    point = driver.execute_script(
        """
        const element = arguments[0];
        element.scrollIntoView({block: 'center', inline: 'center'});
        const rect = element.getBoundingClientRect();
        return {
          x: rect.left + rect.width / 2,
          y: rect.top + rect.height / 2
        };
        """,
        element,
    )
    driver.execute_cdp_cmd(
        "Input.dispatchMouseEvent",
        {
            "type": "mouseMoved",
            "x": point["x"],
            "y": point["y"],
            "button": "none",
            "buttons": 0,
            "pointerType": "mouse",
        },
    )


def load_pack_media_elements(driver) -> list:
    exact_elements = driver.execute_script(
        """
        return [...document.querySelectorAll(
          '.article-body.emoticon-body .emoticons-wrapper > img.emoticon'
        )];
        """
    )
    if exact_elements:
        for index in range(0, len(exact_elements), 12):
            driver.execute_script(
                "arguments[0].scrollIntoView({block:'center'});",
                exact_elements[index],
            )
            time.sleep(0.12)
        driver.execute_script("window.scrollTo(0, 0)")
        time.sleep(0.2)
        return driver.execute_script(
            """
            return [...document.querySelectorAll(
              '.article-body.emoticon-body .emoticons-wrapper > img.emoticon'
            )];
            """
        )

    # Fallback for a future page structure change.
    boundary = driver.execute_script(
        """
        const documentY = element =>
          element.getBoundingClientRect().top + window.scrollY;
        const visible = element => {
          const style = getComputedStyle(element);
          const rect = element.getBoundingClientRect();
          return style.display !== 'none' &&
            style.visibility !== 'hidden' &&
            rect.width > 0 && rect.height > 0;
        };

        const rankingBoundaries = [];
        document.querySelectorAll('h1, h2, h3, h4, h5, h6').forEach(element => {
          const text = (element.textContent || '').replace(/\\s+/g, ' ').trim();
          if (text.includes('전체 아카콘') && visible(element)) {
            rankingBoundaries.push(documentY(element));
          }
        });
        if (rankingBoundaries.length) return Math.min(...rankingBoundaries);

        const purchaseBoundaries = [];
        document.querySelectorAll('button, a, [role="button"]').forEach(element => {
          const text = (element.textContent || '').replace(/\\s+/g, ' ').trim();
          if (text.includes('구매하기') && visible(element)) {
            purchaseBoundaries.push(documentY(element));
          }
        });
        return purchaseBoundaries.length ? Math.max(...purchaseBoundaries) : null;
        """
    )
    if boundary is None:
        boundary = driver.execute_script(
            "return Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)"
        )

    viewport = driver.execute_script("return window.innerHeight") or 800
    step = max(300, int(viewport * 0.75))
    position = 0
    while position < boundary:
        driver.execute_script("window.scrollTo(0, arguments[0])", position)
        time.sleep(0.12)
        new_boundary = driver.execute_script(
            """
            const documentY = element =>
              element.getBoundingClientRect().top + window.scrollY;
            const headings = [...document.querySelectorAll('h1, h2, h3, h4, h5, h6')]
              .filter(element =>
                (element.textContent || '').replace(/\\s+/g, ' ').trim()
                  .includes('전체 아카콘')
              );
            return headings.length
              ? Math.min(...headings.map(documentY))
              : arguments[0];
            """,
            boundary,
        )
        boundary = min(boundary, new_boundary)
        position += step

    driver.execute_script("window.scrollTo(0, 0)")
    time.sleep(0.2)
    return driver.execute_script(
        """
        const boundary = arguments[0];
        const documentY = element =>
          element.getBoundingClientRect().top + window.scrollY;
        return [...document.querySelectorAll(
          'img, video, picture, [data-src], [data-original], [data-video]'
        )].filter(element => documentY(element) < boundary);
        """,
        boundary,
    )


def get_pack_title(driver) -> str:
    title = driver.execute_script(
        """
        const selectors = [
          'meta[property="og:title"]',
          'meta[name="twitter:title"]',
          'main h1',
          'h1'
        ];
        for (const selector of selectors) {
          const element = document.querySelector(selector);
          const value = element?.content || element?.textContent;
          if (value?.trim()) return value.trim();
        }
        return document.title || '';
        """
    )
    title = str(title or "").strip()
    title = re.sub(r"\s*[-|]\s*아카라이브\s*$", "", title).strip()
    return title


def safe_folder_name(title: str, pack_id: str) -> str:
    title = INVALID_FILENAME_RE.sub("_", title)
    title = re.sub(r"\s+", " ", title).strip(" .")
    if not title or title.lower() in {"아카라이브", "arca.live"}:
        return f"arcacon-{pack_id}"
    return f"{title}({pack_id})"


def get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for_debug_port(port: int, process: subprocess.Popen) -> None:
    endpoint = f"http://127.0.0.1:{port}/json/version"
    deadline = time.time() + 20
    while time.time() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                "브라우저가 실행되지 않았습니다. 기존 다운로드용 브라우저 창을 닫고 "
                "다시 실행하세요."
            )
        try:
            with urllib.request.urlopen(endpoint, timeout=1):
                return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError("브라우저 디버깅 연결 시간이 초과됐습니다.")


def get_browser_major_version(browser_path: Path) -> int | None:
    try:
        escaped_path = str(browser_path).replace("'", "''")
        version = subprocess.check_output(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"(Get-Item -LiteralPath '{escaped_path}').VersionInfo.ProductVersion",
            ],
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        ).strip()
        return int(version.split(".", 1)[0])
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def is_cloudflare_challenge(driver) -> bool:
    title = driver.title.lower()
    url = driver.current_url.lower()
    return (
        "just a moment" in title
        or "__cf_chl" in url
        or "/cdn-cgi/challenge" in url
    )


def find_media_urls(
    page_url: str,
    browser_name: str,
    driver_kind: str,
    browser_path: Path,
    wait: float,
    headless: bool,
) -> tuple[list[str], list[str], str, dict[str, str]]:
    from selenium import webdriver
    from selenium.common.exceptions import WebDriverException

    profile_dir = LOCAL_APP_DATA / (
        f"arcacon-downloader/{browser_name.lower()}-profile"
    )
    profile_dir.mkdir(parents=True, exist_ok=True)
    browser_process = None
    if driver_kind == "edge":
        from selenium.webdriver.edge.options import Options

        options = Options()
        options.binary_location = str(browser_path)
        options.add_argument(f"--user-data-dir={profile_dir}")
        options.add_argument("--window-size=1280,1000")
        if headless:
            options.add_argument("--headless=new")
        driver = webdriver.Edge(options=options)
    else:
        import setuptools  # noqa: F401
        import undetected_chromedriver as uc

        options = uc.ChromeOptions()
        options.add_argument("--window-size=1280,1000")
        options.add_argument("--autoplay-policy=no-user-gesture-required")
        options.add_argument("--disable-notifications")
        driver = uc.Chrome(
            options=options,
            browser_executable_path=str(browser_path),
            user_data_dir=str(profile_dir),
            version_main=get_browser_major_version(browser_path),
            headless=headless,
            use_subprocess=True,
        )
    video_urls: set[str] = set()
    image_urls: set[str] = set()
    pack_title = ""
    request_headers: dict[str, str] = {}

    try:
        print("[1/3] 아카콘 페이지를 여는 중...")
        driver.get(page_url)

        if "/u/login" in driver.current_url:
            if headless:
                raise RuntimeError(
                    "최초 로그인이 필요합니다. --headless 없이 한 번 실행해 로그인하세요."
                )
            print("      최초 1회 로그인이 필요합니다.")
            print(
                f"      열린 {browser_name}에서 로그인하세요. "
                "완료될 때까지 최대 3분 기다립니다."
            )
            login_deadline = time.time() + 180
            while time.time() < login_deadline and "/u/login" in driver.current_url:
                time.sleep(1)
            if "/u/login" in driver.current_url:
                raise RuntimeError("로그인 대기 시간이 초과됐습니다.")
            driver.get(page_url)

        deadline = time.time() + 120
        while time.time() < deadline:
            if not is_cloudflare_challenge(driver):
                break
            print(
                "      Cloudflare 확인을 기다리는 중... "
                "브라우저에 확인 화면이 보이면 직접 완료하세요.",
                end="\r",
                flush=True,
            )
            time.sleep(1)
        else:
            raise RuntimeError(
                "Cloudflare 확인을 통과하지 못했습니다. --headless 없이 다시 실행하세요."
            )

        time.sleep(1.5)
        pack_title = get_pack_title(driver)
        request_headers["User-Agent"] = driver.execute_script(
            "return navigator.userAgent"
        )
        cookies = driver.get_cookies()
        if cookies:
            request_headers["Cookie"] = "; ".join(
                f"{cookie['name']}={cookie['value']}" for cookie in cookies
            )
        seen_video_urls = install_mp4_observer(driver)

        print("[2/3] 아카콘 이미지와 영상 요청을 수집하는 중...")
        elements = load_pack_media_elements(driver)
        print(f"      본문 미디어 후보 {len(elements)}개")
        candidate_images_by_index = collect_image_urls_for_elements(driver, elements)

        hovered = 0
        for element, candidate_images in zip(elements, candidate_images_by_index):
            try:
                if not element.is_displayed():
                    continue
                size = element.size
                if size["width"] < 16 or size["height"] < 16:
                    continue
                if size["width"] > 700 or size["height"] > 700:
                    continue

                if not candidate_images and element.tag_name.lower() != "video":
                    continue

                trigger_hover(driver, element)
                hovered += 1

                new_videos: set[str] = set()
                hover_deadline = time.perf_counter() + wait
                while time.perf_counter() < hover_deadline:
                    time.sleep(min(0.03, max(0.005, wait)))
                    discovered = read_observed_mp4_urls(driver)
                    new_videos = discovered - seen_video_urls
                    if new_videos:
                        break
                final_discovered = read_observed_mp4_urls(driver)
                new_videos.update(final_discovered - seen_video_urls)
                seen_video_urls.update(final_discovered)
                if new_videos:
                    video_urls.update(new_videos)
                else:
                    image_urls.update(candidate_images)
                if new_videos or hovered % 10 == 0 or hovered == len(elements):
                    print(
                        f"      요소 {hovered}/{len(elements)}개 확인 / "
                        f"영상 {len(video_urls)}개 / 이미지 {len(image_urls)}개",
                        end="\r",
                        flush=True,
                    )
            except WebDriverException:
                continue

        print()
    finally:
        try:
            driver.quit()
        finally:
            if browser_process is not None and browser_process.poll() is None:
                browser_process.terminate()
                try:
                    browser_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    browser_process.kill()

    return sorted(video_urls), sorted(image_urls), pack_title, request_headers


def download_file(
    url: str,
    destination: Path,
    referer: str,
    session_headers: dict[str, str] | None = None,
) -> None:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/138.0.0.0 Safari/537.36"
        ),
        "Referer": referer,
        "Origin": "https://arca.live",
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,"
        "image/*,video/*,*/*;q=0.8",
    }
    if session_headers:
        headers.update(session_headers)
    request = urllib.request.Request(
        url,
        headers=headers,
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        if response.status not in (200, 206):
            raise RuntimeError(f"HTTP {response.status}")
        destination.write_bytes(response.read())


def convert_video_to_gif(
    ffmpeg: str, source: Path, destination: Path, fps: int, width: int
) -> None:
    video_filter = (
        f"fps={fps},scale='min({width},iw)':-2:flags=lanczos,"
        "split[s0][s1];"
        "[s0]palettegen=stats_mode=diff[p];"
        "[s1][p]paletteuse=dither=sierra2_4a"
    )
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "fatal",
        "-threads",
        "1",
        "-filter_complex_threads",
        "1",
        "-y",
        "-i",
        str(source),
        "-filter_complex",
        video_filter,
        "-loop",
        "0",
        str(destination),
    ]
    subprocess.run(command, check=True)


def convert_image_to_gif(
    ffmpeg: str, source: Path, destination: Path, width: int
) -> None:
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "fatal",
        "-threads",
        "1",
        "-y",
        "-i",
        str(source),
        "-vf",
        f"scale='min({width},iw)':-2:flags=lanczos",
        "-frames:v",
        "1",
        str(destination),
    ]
    subprocess.run(command, check=True)


def process_media(
    index: int,
    media_type: str,
    url: str,
    temp_dir: Path,
    output_dir: Path,
    referer: str,
    session_headers: dict[str, str],
    ffmpeg: str,
    fps: int,
    width: int,
    keep_source: bool,
) -> tuple[int, str, str | None]:
    stem = f"arcacon_{index:03d}"
    source_suffix = (
        ".mp4"
        if media_type == "mp4"
        else Path(urlparse(url).path).suffix.lower() or ".img"
    )
    source_path = temp_dir / f"{stem}{source_suffix}"
    gif_path = output_dir / f"{stem}.gif"

    try:
        download_file(url, source_path, referer, session_headers)
        if media_type == "mp4":
            convert_video_to_gif(ffmpeg, source_path, gif_path, fps, width)
        else:
            convert_image_to_gif(ffmpeg, source_path, gif_path, width)
        return index, gif_path.name, None
    except Exception as exc:
        gif_path.unlink(missing_ok=True)
        return index, gif_path.name, str(exc)
    finally:
        if not keep_source:
            source_path.unlink(missing_ok=True)


def main() -> int:
    args = parse_args()
    if not ARCA_URL_RE.match(args.url):
        print("오류: https://arca.live/e/숫자 형태의 링크를 입력하세요.", file=sys.stderr)
        return 2
    if args.fps < 1 or args.width < 16 or args.wait < 0 or args.workers < 1:
        print("오류: fps, width, wait, workers 값을 확인하세요.", file=sys.stderr)
        return 2

    try:
        ffmpeg, browser_name, driver_kind, browser_path = require_environment()
        print(f"브라우저: {browser_name}")
        video_urls, image_urls, pack_title, session_headers = find_media_urls(
            args.url,
            browser_name,
            driver_kind,
            browser_path,
            args.wait,
            args.headless,
        )
        media = [("mp4", url) for url in video_urls]
        media.extend(("image", url) for url in image_urls)
        if not media:
            raise RuntimeError(
                "아카콘 미디어를 찾지 못했습니다. 페이지가 완전히 표시됐는지 확인하세요."
            )

        pack_id = urlparse(args.url).path.rstrip("/").split("/")[-1]
        output_dir = args.output.resolve() / safe_folder_name(pack_title, pack_id)
        temp_dir = output_dir / "_source"
        temp_dir.mkdir(parents=True, exist_ok=True)

        print(
            f"[3/3] 영상 {len(video_urls)}개, 이미지 {len(image_urls)}개를 "
            f"GIF로 변환합니다. (병렬 작업 {args.workers}개)"
        )
        completed = 0
        finished = 0
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = [
                executor.submit(
                    process_media,
                    index,
                    media_type,
                    url,
                    temp_dir,
                    output_dir,
                    args.url,
                    session_headers,
                    ffmpeg,
                    args.fps,
                    args.width,
                    args.keep_source,
                )
                for index, (media_type, url) in enumerate(media, 1)
            ]
            for future in as_completed(futures):
                index, gif_name, error = future.result()
                finished += 1
                if error is None:
                    completed += 1
                    print(
                        f"      [{finished}/{len(media)} 완료] "
                        f"{gif_name} (원본 #{index})"
                    )
                else:
                    print(
                        f"      [{finished}/{len(media)} 완료] "
                        f"{gif_name} 실패: {error}",
                        file=sys.stderr,
                    )

        if not args.keep_source:
            try:
                temp_dir.rmdir()
            except OSError:
                pass

        if completed == 0:
            raise RuntimeError("변환에 성공한 파일이 없습니다.")

        print(f"\n완료: {completed}개 GIF")
        print(output_dir)
        return 0
    except KeyboardInterrupt:
        print("\n중단되었습니다.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
