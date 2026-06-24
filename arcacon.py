from __future__ import annotations

import argparse
import json
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
        description="아카콘 링크에서 MP4를 찾아 GIF로 변환합니다."
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
        "--wait", type=float, default=0.18, help="각 요소 hover 대기 시간"
    )
    parser.add_argument(
        "--keep-mp4", action="store_true", help="변환 후 원본 MP4 유지"
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


def collect_from_performance_log(driver) -> set[str]:
    from selenium.common.exceptions import WebDriverException

    urls: set[str] = set()
    try:
        entries = driver.get_log("performance")
    except WebDriverException:
        return urls

    for item in entries:
        try:
            message = json.loads(item["message"])["message"]
        except (KeyError, TypeError, json.JSONDecodeError):
            continue

        if message.get("method") != "Network.responseReceived":
            continue

        response = message.get("params", {}).get("response", {})
        url = response.get("url", "")
        mime = str(response.get("mimeType", "")).lower()
        if ".mp4" in url.lower() or mime == "video/mp4":
            urls.update(extract_mp4_urls(url))
    return urls


def collect_from_page(driver) -> set[str]:
    script = r"""
        const found = new Set();
        const add = value => {
          if (typeof value !== "string") return;
          const matches = value.match(/https?:\/\/[^\s"'<>]+\.mp4(?:\?[^\s"'<>]*)?/ig);
          if (matches) matches.forEach(url => found.add(url));
        };

        performance.getEntriesByType("resource").forEach(entry => add(entry.name));
        document.querySelectorAll("*").forEach(el => {
          add(el.currentSrc);
          add(el.src);
          add(el.href);
          for (const attr of el.attributes || []) add(attr.value);
        });
        return [...found];
    """
    return set(driver.execute_script(script))


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


def find_mp4_urls(
    page_url: str,
    browser_name: str,
    driver_kind: str,
    browser_path: Path,
    wait: float,
    headless: bool,
) -> tuple[list[str], str]:
    from selenium import webdriver
    from selenium.common.exceptions import WebDriverException
    from selenium.webdriver.common.action_chains import ActionChains
    from selenium.webdriver.common.by import By

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
    urls: set[str] = set()
    pack_title = ""

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
        urls.update(collect_from_page(driver))
        urls.update(collect_from_performance_log(driver))

        print("[2/3] 아카콘 영상 요청을 수집하는 중...")
        elements = driver.find_elements(
            By.CSS_SELECTOR,
            (
                "img, picture, video, [data-src], [data-original], [data-video], "
                "[style*='background']"
            ),
        )

        hovered = 0
        for element in elements:
            try:
                if not element.is_displayed():
                    continue
                size = element.size
                if size["width"] < 16 or size["height"] < 16:
                    continue
                if size["width"] > 700 or size["height"] > 700:
                    continue

                driver.execute_script(
                    "arguments[0].scrollIntoView({block:'center', inline:'center'});",
                    element,
                )
                ActionChains(driver).move_to_element(element).perform()
                time.sleep(wait)
                hovered += 1

                urls.update(collect_from_page(driver))
                urls.update(collect_from_performance_log(driver))
                print(
                    f"      요소 {hovered}개 확인 / MP4 {len(urls)}개 발견",
                    end="\r",
                    flush=True,
                )
            except WebDriverException:
                continue

        print()
        urls.update(collect_from_page(driver))
        urls.update(collect_from_performance_log(driver))
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

    return sorted(urls), pack_title


def download_mp4(url: str, destination: Path, referer: str) -> None:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/138.0.0.0 Safari/537.36"
            ),
            "Referer": referer,
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        if response.status not in (200, 206):
            raise RuntimeError(f"HTTP {response.status}")
        destination.write_bytes(response.read())


def convert_to_gif(
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
        "error",
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


def main() -> int:
    args = parse_args()
    if not ARCA_URL_RE.match(args.url):
        print("오류: https://arca.live/e/숫자 형태의 링크를 입력하세요.", file=sys.stderr)
        return 2
    if args.fps < 1 or args.width < 16 or args.wait < 0:
        print("오류: fps, width, wait 값을 확인하세요.", file=sys.stderr)
        return 2

    try:
        ffmpeg, browser_name, driver_kind, browser_path = require_environment()
        print(f"브라우저: {browser_name}")
        urls, pack_title = find_mp4_urls(
            args.url,
            browser_name,
            driver_kind,
            browser_path,
            args.wait,
            args.headless,
        )
        if not urls:
            raise RuntimeError(
                "MP4 요청을 찾지 못했습니다. 페이지가 완전히 표시됐는지 확인하세요."
            )

        pack_id = urlparse(args.url).path.rstrip("/").split("/")[-1]
        output_dir = args.output.resolve() / safe_folder_name(pack_title, pack_id)
        temp_dir = output_dir / "_mp4"
        temp_dir.mkdir(parents=True, exist_ok=True)

        print(f"[3/3] {len(urls)}개 파일을 다운로드하고 GIF로 변환합니다.")
        completed = 0
        for index, url in enumerate(urls, 1):
            stem = f"arcacon_{index:03d}"
            mp4_path = temp_dir / f"{stem}.mp4"
            gif_path = output_dir / f"{stem}.gif"
            try:
                download_mp4(url, mp4_path, args.url)
                convert_to_gif(ffmpeg, mp4_path, gif_path, args.fps, args.width)
                completed += 1
                print(f"      [{index}/{len(urls)}] {gif_path.name}")
                if not args.keep_mp4:
                    mp4_path.unlink(missing_ok=True)
            except Exception as exc:
                print(f"      [{index}/{len(urls)}] 실패: {exc}", file=sys.stderr)

        if not args.keep_mp4:
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
