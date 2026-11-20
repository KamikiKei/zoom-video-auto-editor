@echo off
cd /d %~dp0
echo ========================================
echo  Zoom動画 かんたん編集（ローカル版）
echo ========================================
echo.

rem --- Python チェック（Windows標準のダミー python を見分けるため、バージョン文字列で判定）
python --version > "%TEMP%\zedit_pyver.txt" 2>&1
findstr /R /C:"^Python 3" "%TEMP%\zedit_pyver.txt" >nul 2>nul
if errorlevel 1 (
  echo [不足] Python が見つかりません。
  set /p ANS="今すぐインストールしますか？ [Y/N]: "
  if /i "%ANS%"=="Y" (
    winget install Python.Python.3.12 --accept-package-agreements --accept-source-agreements
    echo.
    echo インストールが終わったら、もう一度このファイルをダブルクリックしてください。
  ) else (
    echo 次のコマンドで入れてから、もう一度このファイルをダブルクリックしてください。
    echo   winget install Python.Python.3.12
  )
  echo.
  pause
  exit /b 1
)

rem --- ffmpeg チェック
where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo [不足] ffmpeg が見つかりません。
  set /p ANS="今すぐインストールしますか？ [Y/N]: "
  if /i "%ANS%"=="Y" (
    winget install Gyan.FFmpeg --accept-package-agreements --accept-source-agreements
    echo.
    echo インストールが終わったら、もう一度このファイルをダブルクリックしてください。
  ) else (
    echo 次のコマンドで入れてから、もう一度このファイルをダブルクリックしてください。
    echo   winget install Gyan.FFmpeg
  )
  echo.
  pause
  exit /b 1
)

rem --- 初回セットアップ（仮想環境）
if not exist .venv\Scripts\python.exe (
  echo 初回セットアップ中です。少し時間がかかります...
  python -m venv .venv
)
if not exist .venv\Scripts\python.exe (
  echo [エラー] セットアップに失敗しました。この画面を写真に撮って見せてください。
  pause
  exit /b 1
)

.venv\Scripts\python -c "import numpy, scipy" 2>nul
if errorlevel 1 (
  echo 必要な部品をインストールしています...
  .venv\Scripts\python -m pip install numpy scipy
)

echo.
echo 起動します。ブラウザが自動で開きます。
echo 終わったら、この黒い画面を閉じてください。
echo.
timeout /t 2 /nobreak >nul
start "" http://localhost:8765
.venv\Scripts\python app.py
pause
