# Resource Monitor Overlay (자원 모니터)

A small always-on-top overlay for Windows + NVIDIA that shows **GPU · VRAM · CPU · RAM · FPS** at a glance,
plus a detail window that tells you *which program* is using them. Built for people who run local AI
(ComfyUI, Ollama, Stable Diffusion …) next to games and browsers on one GPU.

윈도우 + NVIDIA 그래픽카드용 작은 오버레이예요. GPU·VRAM·CPU·RAM·FPS를 한눈에 보여 주고,
더블클릭하면 **어떤 프로그램이** 얼마나 쓰는지 큰 창으로 보여 줘요. 로컬 AI(ComfyUI, Ollama 등)를 돌리면서
게임이나 브라우저를 같이 쓰는 사람에게 맞춰 만들었어요.

![overlay](docs/overlay.png)

## 기능

| | |
|---|---|
| 오버레이 | GPU 사용률, VRAM, CPU, RAM은 퍼센트 막대로, FPS는 숫자로 보여 줘요. 92%를 넘으면 주황색이 돼요. |
| 조작 | 끌어서 옮기기(위치 기억), **더블클릭** = 큰 창, **오른쪽 클릭** = 메뉴(큰 창 / FPS 켜기·끄기 / 종료) |
| 큰 창 | 프로그램별로 **사용(바쁨) 막대**와 **VRAM·RAM 차지 막대**를 따로 보여 줘요. 혼자 절반 넘게 차지하면 주황색. |
| FPS | 앞에 띄운 창(게임·영상·브라우저)의 초당 프레임. Intel 공식 PresentMon을 써요. |
| Ollama | 모델이 CPU에 올라가 있는데 GPU가 놀고 VRAM이 넉넉하면, 모델을 다시 올려서 GPU로 옮겨요(끌 수 있어요). |
| 가벼움 | 윈도우 성능 카운터를 직접 읽어서 CPU를 1% 안팎만 써요. |

## 설치

### 방법 1: exe (가장 쉬움)
[Releases](../../releases)에서 `ResourceMonitorOverlay.exe`를 받아서 더블클릭하면 끝이에요.

### 방법 2: 파이썬으로 실행
1. [Python 3.10+](https://www.python.org/downloads/) 설치 (설치할 때 "Add to PATH" 체크)
2. 이 저장소를 받아서 `run.bat` 더블클릭 (처음 한 번 `psutil`을 설치해요)

## FPS 권한

FPS는 윈도우의 화면 기록(ETW)을 읽어야 해서 권한이 필요해요.

- 처음 켜면 Intel 공식 PresentMon(0.93MB)을 [공식 GitHub 릴리스](https://github.com/GameTechDev/PresentMon/releases)에서
  자동으로 받아요. 파일 해시(SHA-256)가 맞을 때만 써요.
- FPS 칸에 **"권한"**이 뜨면: 오버레이 **오른쪽 클릭 → "FPS 켜기 (관리자 확인)" → 윈도우 확인 창에서 '예'**.
  1시간 동안 재고, 끝나면 다시 누르면 돼요.
- 매번 누르기 싫으면 **한 번만** 관리자 PowerShell에서 아래를 실행하고 로그아웃했다 들어오세요.
  그 뒤로는 확인 창 없이 항상 재요.

```powershell
Add-LocalGroupMember -SID S-1-5-32-559 -Member $env:USERNAME
```

(S-1-5-32-559 = 윈도우 기본 그룹 "Performance Log Users". 성능 기록을 읽을 수 있게 해 주는 그룹이에요.)

## 요구 사항

- Windows 10 / 11
- NVIDIA 그래픽카드 (`nvidia-smi` 사용). 다른 그래픽카드는 GPU 줄이 비어 있을 수 있어요.
- Ollama 기능은 Ollama가 `127.0.0.1:11434`에서 돌 때만 동작해요.

## 데이터 위치

설정·기록은 `%LOCALAPPDATA%\ResourceMonitorOverlay`에만 저장해요(오버레이 위치, PresentMon, Ollama 전환 기록).
인터넷에는 PresentMon을 처음 받을 때만 접속해요.

## 라이선스

MIT. PresentMon은 Intel의 MIT 라이선스 프로그램이며, 이 저장소에는 포함하지 않고 공식 릴리스에서 받아요.
