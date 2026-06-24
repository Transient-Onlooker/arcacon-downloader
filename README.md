# arcacon-downloader

아카콘 페이지 링크를 받아 영상과 정적 이미지를 자동 수집하고 GIF로 변환하는
CLI입니다.
현재 아카콘 상품 목록만 처리하며, 헤더·별점·배너·페이지 하단의 인기·추천
아카콘은 제외합니다.

## 설치

Python 3.10 이상, FFmpeg와 Brave/Chrome/Edge 중 하나가 필요합니다.
브라우저는 `Brave → Chrome → Edge` 순서로 자동 선택됩니다.

```powershell
python -m pip install -r requirements.txt
```

## 사용

`run.bat`을 더블클릭한 후 아카콘 링크와 병렬 작업 수를 입력합니다. 작업 수를
비워 두면 CPU 기준 자동값을 사용합니다.

또는 PowerShell에서 직접 실행할 수 있습니다.

```powershell
python arcacon.py https://arca.live/e/52927
```

결과는 페이지 제목과 번호를 사용한 폴더에 저장됩니다.

```text
download/<아카콘 이름>(<번호>)/
```

`-o` 옵션으로 최상위 저장 폴더를 변경할 수 있습니다.

최초 실행 시 선택된 브라우저의 전용 창에서 아카라이브 로그인이 한 번 필요합니다.
로그인 정보는 `%LOCALAPPDATA%\arcacon-downloader\<브라우저>-profile`에
유지되므로 다음 실행부터는 링크만 입력하면 됩니다.

```powershell
python arcacon.py https://arca.live/e/52927 --fps 20 --width 384
python arcacon.py https://arca.live/e/52927 --workers 4
python arcacon.py https://arca.live/e/52927 --wait 0.12
python arcacon.py https://arca.live/e/52927 --keep-source
python arcacon.py https://arca.live/e/52927 --headless
```

기본 실행 시 선택된 브라우저 창이 잠깐 열렸다가 수집 후 자동으로 닫힙니다.
`--headless`는 창을 숨기지만 Cloudflare 확인 때문에 실패할 수 있습니다.
