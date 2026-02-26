@echo off
setlocal

cd /d "%~dp0"

echo ========================================
echo Print Agent - Build EXE (PyInstaller)
echo ========================================

echo [1/3] Verificando PyInstaller...
pyinstaller --version >nul 2>&1
if errorlevel 1 (
  echo PyInstaller nao encontrado. Instalando...
  python -m pip install pyinstaller
  if errorlevel 1 (
    echo Erro ao instalar PyInstaller.
    exit /b 1
  )
)

echo [2/3] Gerando executavel com PrintAgent.spec...
pyinstaller --noconfirm PrintAgent.spec
if errorlevel 1 (
  echo Build falhou.
  exit /b 1
)

echo [3/3] Build concluida com sucesso!
echo Saida: %CD%\dist\PrintAgent.exe

endlocal
