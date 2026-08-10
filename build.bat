@echo off
setlocal

cd /d "%~dp0"

echo ========================================
echo Print Agent - Build EXE (PyInstaller)
echo ========================================

if not exist "PrintAgent.spec" (
  echo ERRO: PrintAgent.spec nao encontrado neste diretorio.
  echo O arquivo deve estar versionado no repositorio.
  exit /b 1
)

if not exist "templates\" (
  echo ERRO: pasta templates\ nao encontrada.
  exit /b 1
)

echo [1/4] Verificando dependencias...
python -m pip install -r requirements.txt pyinstaller
if errorlevel 1 (
  echo Erro ao instalar dependencias.
  exit /b 1
)

echo [2/4] Verificando PyInstaller...
pyinstaller --version
if errorlevel 1 (
  echo PyInstaller nao encontrado apos instalacao.
  exit /b 1
)

echo [3/4] Gerando executavel com PrintAgent.spec...
pyinstaller --noconfirm PrintAgent.spec
if errorlevel 1 (
  echo Build falhou.
  exit /b 1
)

if not exist "dist\PrintAgent.exe" (
  echo ERRO: dist\PrintAgent.exe nao foi gerado.
  exit /b 1
)

echo [4/4] Build concluida com sucesso!
echo Saida: %CD%\dist\PrintAgent.exe
dir "dist\PrintAgent.exe"

endlocal
