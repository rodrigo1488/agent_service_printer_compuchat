#!/bin/bash

echo "========================================"
echo "Print Agent - WebSocket"
echo "========================================"
echo ""

# Verificar se Python está instalado
if ! command -v python3 &> /dev/null; then
    echo "ERRO: Python 3 não encontrado!"
    echo "Por favor, instale o Python 3.7 ou superior."
    exit 1
fi

# Verificar se as dependências estão instaladas
echo "Verificando dependências..."
if ! python3 -c "import flask" &> /dev/null; then
    echo "Instalando dependências..."
    pip3 install -r requirements.txt
    if [ $? -ne 0 ]; then
        echo "ERRO: Falha ao instalar dependências!"
        exit 1
    fi
fi

echo ""
echo "Iniciando servidor..."
echo ""
python3 app.py
