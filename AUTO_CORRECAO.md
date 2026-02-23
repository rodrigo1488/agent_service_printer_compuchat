# Sistema de Auto-Correção e Prevenção de Falhas

Este documento descreve as melhorias implementadas no Print Agent para prevenir e corrigir automaticamente falhas.

## 📋 Resumo das Melhorias

### 1. Sistema de Retry com Backoff Exponencial
- **Arquivo**: `error_recovery.py`
- **Funcionalidade**: Retry automático com delay exponencial para operações que podem falhar temporariamente
- **Aplicado em**:
  - Impressão (RAW e Local)
  - Operações de banco de dados
  - Conexões WebSocket

### 2. Monitoramento e Auto-Restart de Threads
- **Arquivo**: `error_recovery.py` → `ThreadMonitor`
- **Funcionalidade**: Monitora threads do WebSocket e reinicia automaticamente se morrerem
- **Configuração**:
  - Verificação a cada 30 segundos
  - Máximo de 5 reinícios por thread
  - Delay de 5 segundos entre reinícios

### 3. Validação de Dados
- **Arquivo**: `error_recovery.py` → `DataValidator`
- **Funcionalidade**: 
  - Validação de jobs de impressão recebidos
  - Sanitização de configurações de impressoras
  - Prevenção de dados malformados

### 4. Health Checks de Conexão
- **Arquivo**: `error_recovery.py` → `ConnectionHealthChecker`
- **Funcionalidade**:
  - Verifica conectividade com impressoras antes de imprimir
  - Valida acessibilidade de URLs WebSocket
  - Evita tentativas de impressão em impressoras offline

### 5. Recuperação de Banco de Dados
- **Arquivo**: `error_recovery.py` → `DatabaseRecovery`
- **Funcionalidade**:
  - Validação de conexão com banco
  - Backup automático em caso de problemas
  - Retry automático para operações de escrita
  - Modo WAL (Write-Ahead Logging) para melhor concorrência

### 6. Fallback de Encoding
- **Arquivo**: `error_recovery.py` → `EncodingFallback`
- **Funcionalidade**: 
  - Tenta múltiplos encodings se o preferido falhar
  - Ordem: cp850 → cp860 → cp1252 → utf-8 → latin1 → ascii
  - Garante que caracteres especiais sejam impressos corretamente

## 🔧 Melhorias por Arquivo

### `agent.py`
- ✅ Validação de dados de jobs antes de processar
- ✅ Health check de impressoras antes de imprimir
- ✅ Retry automático para impressão
- ✅ Monitoramento de threads WebSocket
- ✅ Tratamento melhorado de erros de conexão
- ✅ Contador de falhas consecutivas com ajuste de delay

### `printer_service.py`
- ✅ Retry automático para impressão RAW e Local
- ✅ Fallback de encoding automático
- ✅ Timeout aumentado de 5s para 10s
- ✅ Melhor tratamento de exceções

### `db.py`
- ✅ Retry automático para operações de escrita
- ✅ Validação de banco na inicialização
- ✅ Backup automático em caso de problemas
- ✅ Modo WAL para melhor concorrência
- ✅ Timeout aumentado para 10 segundos
- ✅ Sanitização de configurações antes de salvar

### `app.py`
- ✅ Validação de banco na inicialização
- ✅ Sanitização de dados de configuração
- ✅ Health check endpoint melhorado com status detalhado

## 🚨 Pontos de Falha Identificados e Corrigidos

### 1. Conexão WebSocket Perdida
**Problema**: Threads WebSocket podem morrer sem reiniciar
**Solução**: 
- Monitor de threads que detecta e reinicia automaticamente
- Reconexão exponencial com limite de falhas consecutivas

### 2. Impressora Offline
**Problema**: Tentativas de impressão em impressoras offline causam timeouts
**Solução**: 
- Health check antes de imprimir
- Retry automático com backoff exponencial

### 3. Erros de Encoding
**Problema**: Caracteres especiais (ç, ã, é) podem não imprimir corretamente
**Solução**: 
- Fallback automático de encoding
- Múltiplos encodings tentados em ordem

### 4. Locks de Banco de Dados
**Problema**: Operações simultâneas podem causar locks
**Solução**: 
- Modo WAL (Write-Ahead Logging)
- Retry automático para operações que falham por lock
- Timeout aumentado

### 5. Dados Malformados
**Problema**: Dados inválidos podem causar crashes
**Solução**: 
- Validação rigorosa de dados recebidos
- Sanitização de configurações
- Tratamento de exceções em todos os pontos críticos

### 6. Threads Mortas
**Problema**: Threads podem morrer sem detecção
**Solução**: 
- Monitor de threads com verificação periódica
- Auto-restart com limite de tentativas

## 📊 Endpoint de Health Check

O endpoint `/health` agora retorna informações detalhadas:

```json
{
  "status": "ok",
  "message": "Print Agent is running",
  "timestamp": "2024-01-15T10:30:00",
  "database": {
    "status": "ok"
  },
  "threads": {
    "total": 2,
    "alive": 2,
    "monitored": 2
  },
  "printers": {
    "configured": 2,
    "active": 2,
    "health": [
      {
        "device_id": "printer-001",
        "connection_type": "network",
        "accessible": true
      }
    ]
  }
}
```

## 🔄 Fluxo de Auto-Correção

### Impressão
1. Valida dados do job
2. Verifica conectividade da impressora
3. Tenta imprimir com retry (até 3 tentativas)
4. Usa fallback de encoding se necessário
5. Registra resultado no banco (com retry)

### WebSocket
1. Valida URL antes de conectar
2. Conecta com headers de autenticação
3. Monitor detecta se thread morre
4. Reinicia automaticamente (até 5 vezes)
5. Aumenta delay após falhas consecutivas

### Banco de Dados
1. Valida conexão na inicialização
2. Cria backup se houver problemas
3. Usa modo WAL para concorrência
4. Retry automático em caso de erro
5. Timeout aumentado para evitar locks

## 🛡️ Prevenção de Falhas

### Validações Implementadas
- ✅ Dados de jobs de impressão
- ✅ Configurações de impressoras
- ✅ Conectividade de rede
- ✅ Estado do banco de dados
- ✅ Estado das threads

### Retries Configurados
- ✅ Impressão: 3 tentativas, delay 1-10s
- ✅ Banco de dados: 3 tentativas, delay 0.5-5s
- ✅ WebSocket: Reconexão exponencial, delay 1-60s

### Monitoramento Ativo
- ✅ Threads WebSocket (verificação a cada 30s)
- ✅ Saúde de impressoras (antes de cada impressão)
- ✅ Estado do banco de dados (na inicialização e operações)

## 📝 Notas de Uso

1. **Backups Automáticos**: Backups são criados automaticamente em `backup/` quando problemas são detectados
2. **Logs**: Todas as operações de auto-correção são logadas
3. **Performance**: Retries e health checks são otimizados para não impactar performance
4. **Configuração**: Parâmetros de retry podem ser ajustados em `error_recovery.py`

## 🔍 Monitoramento

Para verificar o status do sistema:
```bash
curl http://localhost:5000/health
```

Para ver logs de auto-correção, verifique o arquivo `agent_console.log` ou a interface web em `http://localhost:5000/logs`.
