@echo off
chcp 65001 > nul

echo === Wakaru Build ===
echo.

set PYTHON=C:\Users\user\AppData\Local\Python\bin\python.exe
%PYTHON% -m pip install -r requirements.txt
if %ERRORLEVEL% neq 0 (
    echo [ERROR] pip install failed.
    pause
    exit /b 1
)

echo.
echo [1/2] Building teacher tool...
%PYTHON% -m PyInstaller --onefile --windowed --collect-all websockets --name Wakaru_Teacher teacher_tool.py
if %ERRORLEVEL% neq 0 (
    echo [ERROR] Build failed: teacher_tool
    pause
    exit /b 1
)

echo.
echo [2/2] Building student tool...
%PYTHON% -m PyInstaller --onefile --windowed --collect-all websockets --name Wakaru_Student student_tool.py
if %ERRORLEVEL% neq 0 (
    echo [ERROR] Build failed: student_tool
    pause
    exit /b 1
)

echo.
echo ========================================
echo  Build complete!
echo   dist\Wakaru_Teacher.exe  (for teacher)
echo   dist\Wakaru_Student.exe  (for students)
echo ========================================
pause
