# Instalar CodeBridge em outro computador

Versao: **2.0.12-prealpha**. Destino: **Windows 10/11 x64**.

1. Baixe o instalador `dist/CodeBridge-Setup-2.0.12-prealpha.exe` neste repositorio.
2. Confira o SHA-256 usando `dist/CodeBridge-Setup-2.0.12-prealpha.sha256`.
3. Execute-o em um computador Windows x64 com internet, em uma pasta que nao tenha CodeBridge instalado.
4. O Setup copia o aplicativo, as ferramentas de tunel e baixa o Python oficial e as dependencias. O log fica em `%LOCALAPPDATA%\CodeBridge-MCP-Bridge\installer.log`.
5. Abra **Configurar CodeBridge** no menu Iniciar para autorizar a propria conta e configurar ChatGPT/tunel e extensao.
6. Para utilizar RAG e IA locais da mesma maneira, baixe e configure os modelos nesse novo computador.

**Incluido:** codigo-fonte desktop PySide6, Multi Agents, RAG, MCP, extensao, imagens, ferramentas `cloudflared.exe` e `tunnel-client.exe`.

**Nao incluido:** senhas, tokens, contas autenticadas, projetos, bancos pessoais, indices existentes, modelos locais da maquina original e configuracoes do tunel antigo.

**Aviso:** O Setup nao e assinado digitalmente e o SmartScreen pode apresentar alerta. Baixe somente deste repositorio. A instalacao nao reproduz automaticamente a configuracao da maquina original e nao foi homologada em outro PC.

O Setup impede a substituicao automatica de instalacoes existentes. O aplicativo pode executar comandos locais; utilize-o apenas em computadores confiaveis.

## Compilar
Instale NSIS 3, tenha os dois executaveis de tunel de uma instalacao confiavel e execute:

```powershell
powershell -ExecutionPolicy Bypass -File .\installer\build_installer.ps1
```

Os arquivos gerados aparecerao em `dist/`.
