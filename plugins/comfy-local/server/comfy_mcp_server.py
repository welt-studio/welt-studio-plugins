"""
comfy_mcp_server.py

Servidor MCP que expõe o ComfyUI local para clientes que não rodam shell,
como o Claude Desktop. Fala MCP por stdio.

Cada ferramenta é um wrapper fino sobre o comfy-cli, que já emite um
envelope JSON estável em todo comando. O envelope vem de volta cru para
o agente, incluindo o campo "hint" nos erros, que é onde o comfy-cli diz
exatamente o que fazer quando algo falha.

Requisitos:
    pip install "mcp[cli]" comfy-cli

Configuração (Claude Desktop, Windows):
    %APPDATA%\\Roaming\\Claude\\claude_desktop_config.json

    {
      "mcpServers": {
        "comfy": {
          "command": "C:\\\\comfy\\\\env\\\\Scripts\\\\python.exe",
          "args": ["C:\\\\comfy\\\\comfy_mcp_server.py"],
          "env": { "COMFY_BIN": "C:\\\\comfy\\\\env\\\\Scripts\\\\comfy.exe" }
        }
      }
    }

Variáveis de ambiente:
    COMFY_BIN            caminho absoluto do executável comfy (recomendado)
    COMFY_LOCAL_URL      endereço padrão do ComfyUI, se não for 127.0.0.1:8188.
                         Aceita "host:porta" ou "http://host:porta", e vale
                         para todas as chamadas. Os parâmetros host e port de
                         cada ferramenta sobrescrevem este padrão.
    COMFY_MCP_TIMEOUT    timeout padrão em segundos (default 300)
    COMFY_MCP_MAX_CHARS  teto de caracteres por resposta (default 24000)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
from typing import Any, List, Literal, Optional, Tuple
from urllib.parse import quote, urlparse

from pydantic import BaseModel, ConfigDict, Field

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

# ---------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------

COMFY_BIN: str = os.environ.get("COMFY_BIN") or shutil.which("comfy") or "comfy"
DEFAULT_TIMEOUT: int = int(os.environ.get("COMFY_MCP_TIMEOUT", "300"))
LONG_TIMEOUT: int = int(os.environ.get("COMFY_MCP_LONG_TIMEOUT", "1800"))
MAX_CHARS: int = int(os.environ.get("COMFY_MCP_MAX_CHARS", "24000"))

# Os verbos de edição de grafo (`workflow add-node`, `connect`, `set-widget`,
# …) entraram no comfy-cli 1.19.0. Pedimos 1.20.0 porque é a primeira versão
# em que a superfície inteira que embrulhamos aqui está estável — incluindo
# compose/decompose/fragment, que antes falhavam de forma obscura numa CLI
# velha, sem que nada dissesse que o problema era a versão.
MIN_COMFY_CLI: str = "1.20.0"

# Publicar cada edição no canvas aberto exige a extensão comfyui-welt-live
# instalada no ComfyUI. Sem ela a publicação falha silenciosamente e a
# edição segue valendo — por isso o padrão é ligado: quem não instalou não
# perde nada, e quem instalou não precisa configurar.
LIVE_ENABLED: bool = os.environ.get("COMFY_MCP_LIVE", "1").lower() not in ("0", "false", "no")

# O httpx registra uma linha por requisição em nível INFO. Aqui isso só
# enche o stderr, que é onde o diagnóstico do plugin aparece para quem roda
# `claude --debug` — e um log de rede por chamada esconde a mensagem que a
# pessoa está procurando.
logging.getLogger("httpx2").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)


_HOSTNAME = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?$")


def _parse_address(raw: Optional[str]) -> Tuple[Optional[str], Optional[int]]:
    """Extrai host e porta de COMFY_LOCAL_URL.

    Aceita "host", "host:porta" e "http://host:porta". Qualquer outra coisa
    é descartada e o padrão simplesmente não se aplica, deixando a CLI usar
    os defaults dela. Ser rigoroso aqui importa: um host inventado não falha
    alto, ele faz toda chamada bater numa máquina que não existe. E levantar
    exceção também não serve, porque mataria o servidor no import.
    """
    text = (raw or "").strip()
    if not text:
        return None, None
    if "${" in text:
        # Placeholder que o Claude Code não substituiu, tipicamente porque o
        # campo opcional da configuração ficou em branco. Sem isto, o literal
        # viraria um --host inválido em toda chamada.
        return None, None
    if "://" not in text:
        text = "http://" + text

    try:
        parsed = urlparse(text)
        host, port = parsed.hostname, parsed.port
    except ValueError:
        # Porta fora da faixa, entre outros.
        return None, None

    if not host or not _HOSTNAME.match(host):
        return None, None
    if host.isdigit():
        # "8188" é porta digitada no campo do endereço, não nome de máquina.
        return None, None
    return host, port


DEFAULT_HOST, DEFAULT_PORT = _parse_address(os.environ.get("COMFY_LOCAL_URL"))


def _version_tuple(raw: str) -> Optional[Tuple[int, ...]]:
    """Converte "1.20.0" em (1, 20, 0). Devolve None se não der para ler.

    Só os dígitos à frente de cada parte contam, para que "1.20.0rc1" ou
    "1.20.0.dev3" comparem como 1.20.0 em vez de virarem versão ilegível.
    """
    partes: List[int] = []
    for pedaco in (raw or "").strip().lstrip("v").split("."):
        digitos = ""
        for ch in pedaco:
            if not ch.isdigit():
                break
            digitos += ch
        if not digitos:
            break
        partes.append(int(digitos))
    return tuple(partes) if partes else None


def _cli_floor(envelope: dict) -> dict:
    """Confere a versão do comfy-cli contra o piso que este plugin exige.

    A versão vem de graça: todo envelope do comfy-cli carrega o campo
    "version". Degrada em vez de falhar — um envelope sem versão legível
    vira `known: false`, porque não saber a versão não é o mesmo que saber
    que ela é velha, e bloquear por isso quebraria quem está bem.
    """
    bruto = envelope.get("version") if isinstance(envelope, dict) else None
    atual = _version_tuple(bruto) if isinstance(bruto, str) else None
    minimo = _version_tuple(MIN_COMFY_CLI)
    if atual is None or minimo is None:
        return {
            "known": False,
            "minimum": MIN_COMFY_CLI,
            "reported": bruto,
            "hint": f"Não foi possível ler a versão do comfy-cli. Este plugin espera {MIN_COMFY_CLI} ou maior.",
        }
    # Iguala o comprimento antes de comparar: sem isso, "1.20" viraria
    # (1, 20), que é menor que (1, 20, 0) na comparação de tuplas do Python,
    # e uma CLI que atende o piso seria acusada de velha.
    largura = max(len(atual), len(minimo))
    ok = atual + (0,) * (largura - len(atual)) >= minimo + (0,) * (largura - len(minimo))
    bloco = {"known": True, "version": bruto, "minimum": MIN_COMFY_CLI, "satisfied": ok}
    if not ok:
        bloco["hint"] = (
            f"comfy-cli {bruto} é anterior a {MIN_COMFY_CLI}. As ferramentas de edição "
            "de grafo, de fragmento e de receita vão falhar com erro de uso da CLI. "
            "Atualize com `pip install --upgrade comfy-cli` no mesmo ambiente "
            "apontado por COMFY_BIN."
        )
    return bloco


mcp = MCPServer("comfy_mcp")


def _tool(
    name: str,
    title: str,
    read_only: bool = True,
    destructive: bool = False,
    idempotent: bool = True,
    open_world: bool = False,
):
    """Registra uma ferramenta com as anotações de comportamento dela."""
    annotations = ToolAnnotations(
        title=title,
        readOnlyHint=read_only,
        destructiveHint=destructive,
        idempotentHint=idempotent,
        openWorldHint=open_world,
    )
    # Toda ferramenta devolve texto já formatado. Com saída estruturada o SDK
    # embrulha esse texto em {"result": "..."}, e o cliente mostra o JSON
    # escapado dentro de outro JSON: ilegível e com mais tokens.
    return mcp.tool(
        name=name, title=title, annotations=annotations, structured_output=False
    )


# ---------------------------------------------------------------------
# Infraestrutura compartilhada
# ---------------------------------------------------------------------


class Base(BaseModel):
    """Base comum: tira espaço em branco e recusa campos desconhecidos."""

    model_config = ConfigDict(
        str_strip_whitespace=True,
        validate_assignment=True,
        extra="forbid",
    )


def _truncate(text: str, cap: Optional[int] = None) -> str:
    limit = cap or MAX_CHARS
    if len(text) <= limit:
        return text
    return (
        text[:limit]
        + f"\n\n[... resposta truncada em {limit} caracteres. "
        "Reduza o escopo com limit ou filtros mais específicos.]"
    )


def _parse_envelope(raw: str) -> Optional[dict]:
    """
    Extrai o envelope da saída.

    A CLI pode emitir NDJSON (várias linhas) dependendo do comando. O
    envelope final é o que interessa, então varremos de trás para frente.
    """
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    for line in reversed(lines):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and ("ok" in obj or obj.get("type") == "envelope"):
            return obj
    # Último recurso: a saída inteira pode ser um único JSON multi linha.
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


async def _accumulate(stream: Any, sink: List[bytes]) -> None:
    """Lê um cano até o fim, guardando os pedaços fora da tarefa.

    É por isso que não usamos communicate(): ao ser cancelado por tempo
    esgotado ele descarta o que já tinha lido, junto com a resposta que
    talvez estivesse ali inteira. Acumulando aqui, o que chegou sobrevive.
    """
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            return
        sink.append(chunk)


async def _spawn(args: List[str], timeout: int) -> Tuple[Optional[dict], str, str, int]:
    """
    Roda `comfy --json <args>` e devolve (falha, stdout, stderr, código).

    O --json vai antes do subcomando de propósito. Como flag global ele
    produz o envelope único; depois do subcomando, alguns comandos (run,
    por exemplo) passam a emitir NDJSON de eventos.

    `falha` só vem preenchido quando o processo nem chegou a produzir saída
    aproveitável: executável ausente ou tempo esgotado.
    """
    env = dict(os.environ)
    # Evita que o prompt de consentimento de telemetria trave o processo,
    # que não tem terminal interativo aqui.
    env.setdefault("DO_NOT_TRACK", "1")
    env.setdefault("COMFY_NO_TELEMETRY", "1")

    # Ao subir o servidor em segundo plano, o comfy-cli relança a si mesmo
    # pelo nome curto `comfy`. Se o venv dele não está no PATH desta sessão,
    # esse relançamento morre. Chamamos pelo caminho absoluto, então o
    # diretório precisa entrar no PATH explicitamente.
    bin_dir = os.path.dirname(COMFY_BIN)
    if bin_dir:
        env["PATH"] = os.path.abspath(bin_dir) + os.pathsep + env.get("PATH", "")

    cmd = [COMFY_BIN, "--json", *args]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except OSError as exc:
        # FileNotFoundError é o caso comum, mas o campo de configuração é um
        # seletor de arquivo: dá para apontar um diretório, um script sem
        # permissão de execução ou, no Windows, algo que não é executável
        # (WinError 193). Todos chegam aqui como OSError e merecem o mesmo
        # envelope com hint, em vez de um traceback cru.
        return (
            {
                "ok": False,
                "error": {
                    "code": "comfy_bin_not_found",
                    "message": f"Não foi possível executar {COMFY_BIN}: {exc}",
                    "hint": (
                        "Instale com `pip install comfy-cli` e aponte a variável "
                        "COMFY_BIN para o caminho absoluto do executável, por "
                        "exemplo C:\\comfy\\env\\Scripts\\comfy.exe"
                    ),
                },
            },
            "",
            "",
            -1,
        )

    out_chunks: List[bytes] = []
    err_chunks: List[bytes] = []
    leitores = [
        asyncio.ensure_future(_accumulate(proc.stdout, out_chunks)),
        asyncio.ensure_future(_accumulate(proc.stderr, err_chunks)),
    ]

    def decodificado() -> Tuple[str, str]:
        return (
            b"".join(out_chunks).decode("utf-8", errors="replace"),
            b"".join(err_chunks).decode("utf-8", errors="replace"),
        )

    try:
        await asyncio.wait_for(asyncio.gather(*leitores), timeout=timeout)
        await proc.wait()
    except asyncio.TimeoutError:
        for leitor in leitores:
            leitor.cancel()
        proc.kill()
        await proc.wait()
        # Os canos fecham no fim do último processo que os herdou, não no fim
        # do comfy. Um neto segurando o stdout estoura o tempo mesmo com a
        # resposta já entregue, então devolvemos o que chegou: quem chama
        # tenta ler o envelope daí antes de tratar como falha.
        raw_out, raw_err = decodificado()
        return (
            {
                "ok": False,
                "error": {
                    "code": "timeout",
                    "message": f"`{' '.join(cmd)}` passou de {timeout}s.",
                    "hint": (
                        "Para jobs longos, submeta sem wait e acompanhe com "
                        "comfy_job_status, ou aumente COMFY_MCP_TIMEOUT."
                    ),
                },
                "raw_stderr": raw_err[:4000],
            },
            raw_out,
            raw_err,
            -1,
        )

    raw_out, raw_err = decodificado()
    # returncode só é None enquanto o processo vive; depois do wait() ele já
    # terminou. O -1 é defensivo, para não mentir "código 0" caso a
    # implementação de subprocesso deixe o campo vazio.
    return (
        None,
        raw_out,
        raw_err,
        proc.returncode if proc.returncode is not None else -1,
    )


async def _cli_obj(args: List[str], timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Roda a CLI e devolve o envelope já desserializado, sem truncar."""
    failure, raw_out, raw_err, code = await _spawn(args, timeout)

    # O envelope vem primeiro, mesmo quando o processo foi morto por tempo
    # esgotado: se a resposta completa já tinha chegado, entregar ela é bem
    # melhor do que mandar o agente aumentar o timeout à toa.
    envelope = _parse_envelope(raw_out)
    if envelope is not None:
        return envelope

    if failure is not None:
        return failure

    # Sem envelope: devolve o que houver, com o código de saída, para o
    # agente ao menos conseguir diagnosticar.
    return {
        "ok": code == 0,
        "error": None
        if code == 0
        else {
            "code": "unparsed_output",
            "message": f"A CLI saiu com código {code}.",
            "hint": "Veja stdout e stderr abaixo.",
        },
        "raw_stdout": raw_out[:8000],
        "raw_stderr": raw_err[:4000],
    }


async def _cli(
    args: List[str],
    timeout: int = DEFAULT_TIMEOUT,
    cap: Optional[int] = None,
) -> str:
    """Roda a CLI e devolve o envelope como texto, pronto para o agente."""
    envelope = await _cli_obj(args, timeout)
    return _truncate(json.dumps(envelope, indent=2, ensure_ascii=False), cap)


async def _cli_text(
    args: List[str],
    timeout: int = DEFAULT_TIMEOUT,
    cap: Optional[int] = None,
) -> str:
    """
    Variante para comandos que imprimem texto puro em vez de envelope.

    `skills show`, por exemplo, devolve markdown. Nesses casos o stdout é a
    resposta e não faz sentido passar por json.dumps.
    """
    failure, raw_out, raw_err, _ = await _spawn(args, timeout)
    if raw_out.strip():
        return _truncate(raw_out, cap)
    if failure is not None:
        return json.dumps(failure, indent=2, ensure_ascii=False)
    return _truncate(raw_err or "(sem saída)", cap)


def _opt(args: List[str], flag: str, value: Any) -> None:
    """Acrescenta `--flag value` somente se value não for None."""
    if value is not None:
        args.extend([flag, str(value)])


def _routing(args: List[str], host: Optional[str], port: Optional[int]) -> None:
    """Endereço do servidor: o parâmetro da ferramenta vence COMFY_LOCAL_URL."""
    _opt(args, "--host", host if host is not None else DEFAULT_HOST)
    _opt(args, "--port", port if port is not None else DEFAULT_PORT)


# ---------------------------------------------------------------------
# Estado do servidor
# ---------------------------------------------------------------------


class EmptyInput(Base):
    """Sem parâmetros."""


@_tool(
    name="comfy_server_info",
    title="Estado do ComfyUI local",
    read_only=True,
    destructive=False,
    idempotent=True,
    open_world=False,
)
async def comfy_server_info(params: EmptyInput) -> str:
    """Diz se existe um ComfyUI local rodando, onde, e qual workspace está ativo.

    Chame esta ferramenta PRIMEIRO em qualquer sessão. Ela combina o
    ambiente resolvido pela CLI com o workspace selecionado, o que responde
    de uma vez: a CLI existe, o workspace está configurado, e o servidor
    está no ar.

    Args:
        params (EmptyInput): sem parâmetros.

    Returns:
        str: JSON com três chaves. "env" e "which" trazem o envelope do
            comando correspondente. "cli" confere a versão do comfy-cli
            contra o piso deste plugin: com `satisfied: false`, as
            ferramentas de edição de grafo, fragmento e receita vão falhar
            com erro de uso da CLI, e o campo hint diz como atualizar.
            Se o servidor não estiver rodando, os comandos seguintes
            retornarão o código `server_not_running`, resolvido por
            comfy_launch_server.
    """
    env_obj, which_obj = await asyncio.gather(
        _cli_obj(["env"], timeout=60),
        _cli_obj(["which"], timeout=60),
    )
    return _truncate(
        json.dumps(
            {"env": env_obj, "which": which_obj, "cli": _cli_floor(env_obj)},
            indent=2,
            ensure_ascii=False,
        )
    )


class LaunchInput(Base):
    background: bool = Field(
        default=True,
        description="Subir em segundo plano e devolver o controle na hora. "
        "Deixe True: em primeiro plano a ferramenta bloqueia até o timeout.",
    )
    extra_args: Optional[List[str]] = Field(
        default=None,
        description="Argumentos extras passados direto ao ComfyUI, por exemplo "
        "['--lowvram'] ou ['--port', '8189'].",
        max_length=12,
    )


@_tool(
    name="comfy_launch_server",
    title="Subir o ComfyUI local",
    read_only=False,
    destructive=False,
    idempotent=False,
    open_world=False,
)
async def comfy_launch_server(params: LaunchInput) -> str:
    """Sobe o ComfyUI local, por padrão em segundo plano.

    Use quando comfy_server_info ou qualquer outro comando retornar o código
    `server_not_running`. Não derruba nem reinicia uma instância que já
    esteja no ar.

    Args:
        params (LaunchInput):
            - background (bool): rodar em segundo plano, padrão True
            - extra_args (Optional[List[str]]): argumentos para o ComfyUI

    Returns:
        str: envelope JSON da CLI. O carregamento dos modelos continua
            depois do retorno, então o servidor pode levar alguns segundos
            até aceitar jobs.
    """
    args = ["launch"]
    if params.background:
        args.append("--background")
    if params.extra_args:
        args.append("--")
        args.extend(params.extra_args)
    return await _cli(args, timeout=LONG_TIMEOUT)


@_tool(
    name="comfy_stop_server",
    title="Derrubar o ComfyUI em segundo plano",
    read_only=False,
    destructive=False,
    idempotent=True,
    open_world=False,
)
async def comfy_stop_server(params: EmptyInput) -> str:
    """Derruba a instância de ComfyUI iniciada em segundo plano pela CLI.

    Encerra apenas o processo do servidor. Não apaga workspace, modelos,
    workflows nem saídas já geradas. Jobs em execução são interrompidos.

    Args:
        params (EmptyInput): sem parâmetros.

    Returns:
        str: envelope JSON da CLI.
    """
    return await _cli(["stop"], timeout=120)


# ---------------------------------------------------------------------
# Workflows
# ---------------------------------------------------------------------


class ValidateInput(Base):
    workflow_path: str = Field(
        ...,
        description="Caminho do JSON do workflow. Ex: C:\\comfy\\projeto\\txt2img.json",
        min_length=1,
    )
    host: Optional[str] = Field(default=None, description="Host do ComfyUI, padrão 127.0.0.1.")
    port: Optional[int] = Field(default=None, description="Porta do ComfyUI, padrão 8188.", ge=1, le=65535)


@_tool(
    name="comfy_validate_workflow",
    title="Validar workflow antes de rodar",
    read_only=True,
    destructive=False,
    idempotent=True,
    open_world=False,
)
async def comfy_validate_workflow(params: ValidateInput) -> str:
    """Confere um workflow contra a instalação viva sem submeter nada.

    Valida class_types, formato das entradas e valores de enum contra o
    object_info do servidor. Rode isto antes de comfy_run_workflow: pega
    no ato node ausente, checkpoint que não existe e ligação errada, em vez
    de descobrir depois de esperar a geração.

    Args:
        params (ValidateInput):
            - workflow_path (str): caminho do JSON
            - host, port (Optional): endereço do servidor

    Returns:
        str: envelope JSON. Em caso de falha, o campo error.details costuma
            trazer `close_matches` com o nome correto do node.
    """
    # `comfy validate` de nível superior está marcado [DEPRECATED] na CLI.
    # O caminho vivo é o subcomando de workflow, que recebe o caminho por
    # --workflow e não por posicional, ao contrário dos irmãos dele
    # (slots, set-slot, compose, decompose, vary).
    args = ["workflow", "validate", "--workflow", params.workflow_path]
    _routing(args, params.host, params.port)
    return await _cli(args)


class RunInput(Base):
    workflow_path: Optional[str] = Field(
        default=None,
        description="Caminho do JSON do workflow. Aceita tanto formato API quanto "
        "o exportado pela interface, a conversão é feita pela CLI. Omita "
        "somente se for usar o parâmetro prompt.",
    )
    prompt: Optional[str] = Field(
        default=None,
        description="Prompt positivo para o workflow txt2img embutido. Use apenas "
        "para um teste rápido sem workflow próprio.",
    )
    wait: bool = Field(
        default=False,
        description="True bloqueia até terminar. False, o padrão, submete e "
        "devolve o prompt_id na hora, para acompanhar com comfy_job_status. "
        "Prefira False em jobs longos, senão a ferramenta estoura o timeout.",
    )
    host: Optional[str] = Field(default=None, description="Host do ComfyUI, padrão 127.0.0.1.")
    port: Optional[int] = Field(default=None, description="Porta do ComfyUI, padrão 8188.", ge=1, le=65535)


@_tool(
    name="comfy_run_workflow",
    title="Submeter um workflow",
    read_only=False,
    destructive=False,
    idempotent=False,
    open_world=False,
)
async def comfy_run_workflow(params: RunInput) -> str:
    """Submete um workflow ao ComfyUI local.

    Assíncrono por padrão: retorna em milissegundos com um prompt_id
    enquanto a geração roda. Acompanhe com comfy_job_status ou
    comfy_job_wait, depois recolha os arquivos com comfy_fetch_outputs.

    Passe wait=True apenas em workflows curtos. Uma geração de vídeo pode
    passar do timeout da ferramenta e o job continuaria rodando sem que
    você recebesse o resultado.

    Args:
        params (RunInput):
            - workflow_path (Optional[str]): caminho do JSON
            - prompt (Optional[str]): prompt para o workflow embutido
            - wait (bool): bloquear até terminar, padrão False
            - host, port (Optional): endereço do servidor

    Returns:
        str: envelope JSON com data.prompt_id. Guarde esse id, é o que as
            demais ferramentas de job consomem.
    """
    if not params.workflow_path and not params.prompt:
        return json.dumps(
            {
                "ok": False,
                "error": {
                    "code": "missing_input",
                    "message": "Nem workflow_path nem prompt foram informados.",
                    "hint": "Passe workflow_path com o caminho do JSON, ou prompt "
                    "para usar o workflow txt2img embutido.",
                },
            },
            indent=2,
            ensure_ascii=False,
        )

    args = ["run"]
    _opt(args, "--workflow", params.workflow_path)
    _opt(args, "--prompt", params.prompt)
    if params.wait:
        args.append("--wait")
    _routing(args, params.host, params.port)
    return await _cli(args, timeout=LONG_TIMEOUT if params.wait else DEFAULT_TIMEOUT)


class JobStatusInput(Base):
    prompt_id: str = Field(..., description="O prompt_id devolvido por comfy_run_workflow.", min_length=1)
    host: Optional[str] = Field(default=None, description="Host do ComfyUI.")
    port: Optional[int] = Field(default=None, description="Porta do ComfyUI.", ge=1, le=65535)


@_tool(
    name="comfy_job_status",
    title="Status de um job",
    read_only=True,
    destructive=False,
    idempotent=True,
    open_world=False,
)
async def comfy_job_status(params: JobStatusInput) -> str:
    """Consulta o estado de um job pelo prompt_id, sem bloquear.

    Args:
        params (JobStatusInput):
            - prompt_id (str): id devolvido pela submissão
            - host, port (Optional): endereço do servidor

    Returns:
        str: envelope JSON com o estado do job. Se já terminou, use
            comfy_fetch_outputs para trazer os arquivos.
    """
    args = ["jobs", "status", params.prompt_id]
    _routing(args, params.host, params.port)
    return await _cli(args, timeout=120)


class JobWaitInput(Base):
    prompt_ids: List[str] = Field(
        ...,
        description="Um ou mais prompt_ids. Bloqueia até todos terminarem.",
        min_length=1,
        max_length=32,
    )
    timeout_seconds: Optional[float] = Field(
        default=None,
        description="Desistir depois de N segundos. Deixe abaixo do timeout do "
        "servidor MCP para receber uma resposta limpa.",
        gt=0,
    )
    poll_interval: Optional[float] = Field(
        default=None, description="Segundos entre consultas. São jobs longos, não martele.", gt=0
    )
    host: Optional[str] = Field(default=None, description="Host do ComfyUI.")
    port: Optional[int] = Field(default=None, description="Porta do ComfyUI.", ge=1, le=65535)


@_tool(
    name="comfy_job_wait",
    title="Esperar jobs terminarem",
    read_only=True,
    destructive=False,
    idempotent=True,
    open_world=False,
)
async def comfy_job_wait(params: JobWaitInput) -> str:
    """Bloqueia até todos os prompt_ids informados chegarem a um estado final.

    Útil depois de disparar um lote de variações. Para um job único e longo,
    prefere-se consultar comfy_job_status de tempos em tempos.

    Args:
        params (JobWaitInput):
            - prompt_ids (List[str]): ids a aguardar
            - timeout_seconds (Optional[float]): teto de espera
            - poll_interval (Optional[float]): intervalo entre consultas
            - host, port (Optional): endereço do servidor

    Returns:
        str: envelope JSON com o resumo de cada job.
    """
    args = ["jobs", "wait", *params.prompt_ids]
    _opt(args, "--timeout", params.timeout_seconds)
    _opt(args, "--poll-interval", params.poll_interval)
    _routing(args, params.host, params.port)
    return await _cli(args, timeout=LONG_TIMEOUT)


class JobCancelInput(Base):
    prompt_id: str = Field(..., description="Prompt_id do job a cancelar.", min_length=1)
    host: Optional[str] = Field(default=None, description="Host do ComfyUI.")
    port: Optional[int] = Field(default=None, description="Porta do ComfyUI.", ge=1, le=65535)


@_tool(
    name="comfy_job_cancel",
    title="Cancelar um job",
    read_only=False,
    destructive=True,
    idempotent=True,
    open_world=False,
)
async def comfy_job_cancel(params: JobCancelInput) -> str:
    """Cancela um job em execução ou na fila.

    O trabalho já feito por ele se perde: uma geração interrompida no meio
    não deixa saída. Use quando o job travou, quando os parâmetros estavam
    errados, ou para liberar a fila antes de submeter o que interessa.

    Cancelar duas vezes o mesmo job conhecido não é erro. Um prompt_id que o
    servidor não conhece devolve `prompt_not_found`.

    Args:
        params (JobCancelInput):
            - prompt_id (str): id do job
            - host, port (Optional): endereço do servidor

    Returns:
        str: envelope JSON com o resultado do cancelamento.
    """
    args = ["jobs", "cancel", params.prompt_id]
    _routing(args, params.host, params.port)
    return await _cli(args, timeout=120)


class JobListInput(Base):
    limit: Optional[int] = Field(default=20, description="Teto de linhas.", ge=1, le=200)
    all_targets: bool = Field(
        default=False,
        description="True inclui também os jobs de outros destinos de roteamento, "
        "como a nuvem. Numa instalação só local não muda nada — para ver mais "
        "histórico, aumente o limit.",
    )
    local_only: bool = Field(
        default=False,
        description="True lista apenas as submissões que o comfy-cli rastreou em "
        "disco, sem consultar a fila do ComfyUI. O rastro é do workspace, não "
        "desta sessão: jobs de sessões anteriores continuam aparecendo.",
    )
    orphaned: bool = Field(
        default=False,
        description="True lista só os jobs rastreados que sumiram do servidor, "
        "tipicamente porque o ComfyUI foi reiniciado no meio.",
    )
    host: Optional[str] = Field(default=None, description="Host do ComfyUI.")
    port: Optional[int] = Field(default=None, description="Porta do ComfyUI.", ge=1, le=65535)


@_tool(
    name="comfy_job_list",
    title="Listar jobs e a fila",
    read_only=True,
    destructive=False,
    idempotent=True,
    open_world=False,
)
async def comfy_job_list(params: JobListInput) -> str:
    """Mostra o que está na fila e o que já rodou.

    Responde "o que o ComfyUI está fazendo agora": junta as submissões que
    passaram por aqui com a fila e o histórico do próprio servidor. É por
    onde se acha o prompt_id de um job que você perdeu de vista.

    Args:
        params (JobListInput):
            - limit (Optional[int]): teto de linhas, padrão 20
            - all_targets (bool): incluir também outros destinos de roteamento
            - local_only (bool): só o que este servidor rastreou
            - orphaned (bool): só os que sumiram do servidor
            - host, port (Optional): endereço do servidor

    Returns:
        str: envelope JSON com os jobs e seus estados.
    """
    args = ["jobs", "ls"]
    _opt(args, "--limit", params.limit)
    if params.all_targets:
        args.append("--all")
    if params.local_only:
        args.append("--local-only")
    if params.orphaned:
        args.append("--orphaned")
    _routing(args, params.host, params.port)
    return await _cli(args, timeout=120)


class JobWatchInput(Base):
    prompt_id: str = Field(..., description="Prompt_id a acompanhar.", min_length=1)
    timeout_seconds: Optional[int] = Field(
        default=None,
        description="Espera máxima por evento recebido, em segundos inteiros. Padrão "
        "da CLI: 30. NÃO é prazo total do job: um job longo que emite eventos segue "
        "sendo acompanhado.",
        ge=1,
    )
    host: Optional[str] = Field(default=None, description="Host do ComfyUI.")
    port: Optional[int] = Field(default=None, description="Porta do ComfyUI.", ge=1, le=65535)


@_tool(
    name="comfy_job_watch",
    title="Acompanhar a execução de um job",
    read_only=True,
    destructive=False,
    idempotent=True,
    open_world=False,
)
async def comfy_job_watch(params: JobWatchInput) -> str:
    """Segue os eventos de execução de um job e devolve o rastro completo.

    Não confunda com comfy_job_wait. O wait responde "terminou?"; este aqui
    devolve o caminho percorrido — qual node executou, em que ordem, onde
    parou. É ferramenta de diagnóstico, para quando o job falhou ou demorou
    muito mais do que devia, e custa mais que o wait.

    Você não vê nada acontecendo enquanto roda: a saída chega inteira no
    fim, como todas as ferramentas. Para só aguardar, use comfy_job_wait.

    Args:
        params (JobWatchInput):
            - prompt_id (str): id do job
            - timeout_seconds (Optional[int]): espera máxima por evento
            - host, port (Optional): endereço do servidor

    Returns:
        str: envelope JSON com os eventos de execução até o estado final.
    """
    # --poll-interval e --max-wait existem no comando, mas o help da CLI os
    # declara cloud-only. Num plugin local seriam parâmetros inertes.
    args = ["jobs", "watch", params.prompt_id]
    _opt(args, "--timeout", params.timeout_seconds)
    _routing(args, params.host, params.port)
    return await _cli(args, timeout=LONG_TIMEOUT)


class FetchOutputsInput(Base):
    prompt_id: str = Field(..., description="Prompt_id de um job já concluído.", min_length=1)
    out_dir: Optional[str] = Field(
        default=None,
        description="Pasta onde salvar. Padrão: ./outputs relativo ao diretório "
        "de trabalho do servidor MCP.",
    )
    url_only: bool = Field(
        default=False,
        description="True devolve só as URLs, sem baixar arquivo. Útil para "
        "inspecionar o que saiu antes de gravar em disco.",
    )


@_tool(
    name="comfy_fetch_outputs",
    title="Recolher as saídas de um job",
    read_only=False,
    destructive=False,
    idempotent=True,
    open_world=False,
)
async def comfy_fetch_outputs(params: FetchOutputsInput) -> str:
    """Baixa os arquivos gerados por um job concluído.

    Escreve arquivos novos em out_dir. Não apaga nada, mas um arquivo de
    mesmo nome de uma execução anterior pode ser sobrescrito, então use
    uma pasta por lote quando estiver gerando variações.

    Args:
        params (FetchOutputsInput):
            - prompt_id (str): id do job concluído
            - out_dir (Optional[str]): pasta de destino
            - url_only (bool): só listar URLs, sem baixar

    Returns:
        str: envelope JSON com os caminhos ou URLs das saídas.
    """
    # `download` não aceita --host nem --port: resolve o servidor pelo
    # workspace ativo. Por isso não passa por _routing.
    args = ["download", params.prompt_id]
    _opt(args, "--out-dir", params.out_dir)
    if params.url_only:
        args.append("--url-only")
    return await _cli(args, timeout=LONG_TIMEOUT)


# ---------------------------------------------------------------------
# Diagnóstico
#
# Nenhum comando desta seção aceita --host: todos resolvem o servidor pelo
# workspace ativo, e nenhum passa por _routing. O --port de `logs` é a única
# exceção, e não é roteamento: escolhe de qual instância ler o log.
# ---------------------------------------------------------------------


@_tool(
    name="comfy_system_stats",
    title="VRAM e memória do ComfyUI",
    read_only=True,
    destructive=False,
    idempotent=True,
    open_world=False,
)
async def comfy_system_stats(params: EmptyInput) -> str:
    """Lê a VRAM de cada dispositivo e a memória do sistema.

    Chame antes de submeter algo pesado e depois de uma falha suspeita de
    falta de memória. Um job que morre sem mensagem clara costuma ser VRAM
    esgotada, e é aqui que isso aparece — em vez de ficar adivinhando pelo
    sintoma.

    Args:
        params (EmptyInput): sem parâmetros.

    Returns:
        str: envelope JSON com a memória por dispositivo e a do sistema.
    """
    return await _cli(["system-stats"], timeout=120)


class FreeMemoryInput(Base):
    free_cache: bool = Field(
        default=False,
        description="Também limpar o cache do executor. Use quando descarregar os "
        "modelos não bastou.",
    )


@_tool(
    name="comfy_free_memory",
    title="Liberar a VRAM do ComfyUI",
    read_only=False,
    destructive=False,
    idempotent=True,
    open_world=False,
)
async def comfy_free_memory(params: FreeMemoryInput) -> str:
    """Pede ao ComfyUI que descarregue os modelos da memória.

    Vale para o servidor inteiro, não para um workflow: sai da VRAM o que
    estiver carregado, sem distinguir quem carregou. O ComfyUI já gerencia
    isso sozinho a cada execução, então esta ferramenta é para quando o
    automático não bastou — tipicamente ao trocar de família de modelo ou
    antes de um job grande depois de várias gerações.

    Nada é apagado do disco: o próximo job recarrega o que precisar, ao
    custo de alguns segundos. Tente isto antes de reiniciar o servidor com
    comfy_stop_server, que leva a fila junto.

    A liberação é uma marcação na fila de execução, não um efeito imediato.
    Com um job em andamento ela só se aplica depois que ele termina, então
    medir a VRAM no instante seguinte pode não mostrar diferença nenhuma.
    (Fonte: ComfyUI, `server.py` rota POST /free, que só chama
    `prompt_queue.set_flag`, e `main.py` no laço de `prompt_worker`, que lê
    `q.get_flags()` e aí sim chama `unload_all_models()`.)

    Args:
        params (FreeMemoryInput):
            - free_cache (bool): limpar também o cache do executor

    Returns:
        str: envelope JSON com o resultado. Para ver o efeito, chame
            comfy_system_stats depois que a fila estiver vazia.
    """
    # O descarregamento dos modelos é o padrão da CLI e a única razão de
    # chamar isto, então não é exposto como opção.
    args = ["free"]
    if params.free_cache:
        args.append("--free-memory")
    return await _cli(args, timeout=120)


class ServerLogsInput(Base):
    tail: Optional[int] = Field(
        default=None,
        description="Quantas linhas finais trazer. Padrão da CLI: 200.",
        ge=1,
        le=5000,
    )
    port: Optional[int] = Field(
        default=None,
        description="Porta do ComfyUI cujo log você quer. Serve para escolher entre "
        "instâncias, não para endereçar o servidor. Sem valor, usa a porta "
        "configurada em COMFY_LOCAL_URL.",
        ge=1,
        le=65535,
    )


@_tool(
    name="comfy_server_logs",
    title="Ler o log do ComfyUI",
    read_only=True,
    destructive=False,
    idempotent=True,
    open_world=False,
)
async def comfy_server_logs(params: ServerLogsInput) -> str:
    """Traz as últimas linhas do log do ComfyUI subido em segundo plano.

    É onde mora a causa real de um job que falhou. O envelope de erro da
    execução diz que quebrou; o log diz por quê — modelo que não carregou,
    custom node que estourou no import, VRAM que acabou no meio.

    Só existe log para servidor iniciado por comfy_launch_server. Um
    ComfyUI que você subiu na mão, fora da CLI, escreve no terminal dele.

    Args:
        params (ServerLogsInput):
            - tail (Optional[int]): linhas finais, padrão 200
            - port (Optional[int]): escolher entre instâncias

    Returns:
        str: envelope JSON com as linhas do log.
    """
    # A porta cai para COMFY_LOCAL_URL quando não vem explícita: sem isso,
    # um ComfyUI configurado fora da 8188 teria o log lido da instância
    # errada justamente quando se está diagnosticando uma falha.
    args = ["logs"]
    _opt(args, "--tail", params.tail)
    _opt(args, "--port", params.port if params.port is not None else DEFAULT_PORT)
    return await _cli(args, timeout=120)


# ---------------------------------------------------------------------
# Introspecção da instalação viva
# ---------------------------------------------------------------------


class SearchNodesInput(Base):
    query: str = Field(..., description="Texto a procurar, substring sem diferenciar maiúscula.", min_length=1)
    limit: Optional[int] = Field(default=25, description="Teto de resultados.", ge=1, le=200)
    host: Optional[str] = Field(default=None, description="Host do ComfyUI.")
    port: Optional[int] = Field(default=None, description="Porta do ComfyUI.", ge=1, le=65535)


@_tool(
    name="comfy_search_nodes",
    title="Procurar classes de node",
    read_only=True,
    destructive=False,
    idempotent=True,
    open_world=False,
)
async def comfy_search_nodes(params: SearchNodesInput) -> str:
    """Busca difusa nas classes de node da instalação viva, custom nodes inclusos.

    Esta é a vantagem real de um servidor local sobre um na nuvem: os
    resultados refletem o que existe de fato nesta máquina, e não um
    catálogo genérico. Consulte aqui antes de montar qualquer workflow.

    Args:
        params (SearchNodesInput):
            - query (str): texto da busca
            - limit (Optional[int]): teto de resultados, padrão 25
            - host, port (Optional): endereço do servidor

    Returns:
        str: envelope JSON com as classes que casaram. Para o schema
            completo de uma delas, use comfy_show_node.
    """
    args = ["nodes", "search", params.query]
    _opt(args, "--limit", params.limit)
    _routing(args, params.host, params.port)
    return await _cli(args, timeout=120)


class ShowNodeInput(Base):
    name: str = Field(
        ...,
        description="Nome exato da classe, sensível a maiúscula. Ex: 'KSampler'.",
        min_length=1,
    )
    host: Optional[str] = Field(default=None, description="Host do ComfyUI.")
    port: Optional[int] = Field(default=None, description="Porta do ComfyUI.", ge=1, le=65535)


@_tool(
    name="comfy_show_node",
    title="Schema completo de um node",
    read_only=True,
    destructive=False,
    idempotent=True,
    open_world=False,
)
async def comfy_show_node(params: ShowNodeInput) -> str:
    """Mostra entradas, saídas, defaults e restrições de uma classe de node.

    Args:
        params (ShowNodeInput):
            - name (str): nome exato da classe
            - host, port (Optional): endereço do servidor

    Returns:
        str: envelope JSON com o schema. Se o nome não existir, o erro
            `node_not_found` traz `details.close_matches` com sugestões.
    """
    args = ["nodes", "show", params.name]
    _routing(args, params.host, params.port)
    return await _cli(args, timeout=120)


class SearchModelsInput(Base):
    text: Optional[str] = Field(default=None, description="Substring no nome do arquivo do modelo.")
    model_type: Optional[str] = Field(
        default=None,
        description="Tipo do modelo: lora, checkpoint, vae, controlnet, e assim por diante.",
    )
    limit: Optional[int] = Field(default=50, description="Teto de resultados.", ge=1, le=500)
    folders_only: bool = Field(
        default=False,
        description="True lista apenas as pastas de modelo que o backend enxerga, "
        "ignorando os demais filtros. Use para descobrir nomes de tipo válidos.",
    )


@_tool(
    name="comfy_search_models",
    title="Procurar modelos em disco",
    read_only=True,
    destructive=False,
    idempotent=True,
    open_world=False,
)
async def comfy_search_models(params: SearchModelsInput) -> str:
    """Lista os arquivos de modelo presentes nesta máquina.

    Chame antes de escrever um workflow que referencia checkpoint, LoRA ou
    VAE por nome. O erro mais comum em workflow gerado por agente é citar um
    arquivo que não existe no disco.

    Args:
        params (SearchModelsInput):
            - text (Optional[str]): substring do nome
            - model_type (Optional[str]): tipo do modelo
            - limit (Optional[int]): teto de resultados, padrão 50
            - folders_only (bool): listar só as pastas de modelo

    Returns:
        str: envelope JSON com os modelos encontrados.
    """
    # `models` não aceita --host nem --port: lê o workspace ativo em disco.
    # Por isso não passa por _routing.
    if params.folders_only:
        return await _cli(["models", "list-folders"], timeout=120)
    args = ["models", "search"]
    _opt(args, "--text", params.text)
    _opt(args, "--type", params.model_type)
    _opt(args, "--limit", params.limit)
    return await _cli(args, timeout=120)


class ShowModelInput(Base):
    name: str = Field(
        ...,
        description="Nome exato do arquivo, como comfy_search_models devolveu. "
        "Ex: 'sd_xl_base_1.0.safetensors'.",
        min_length=1,
    )


@_tool(
    name="comfy_show_model",
    title="Detalhes de um modelo",
    read_only=True,
    destructive=False,
    idempotent=True,
    open_world=False,
)
async def comfy_show_model(params: ShowModelInput) -> str:
    """Mostra os metadados de um modelo pelo nome exato do arquivo.

    Use depois de comfy_search_models, quando precisar confirmar qual é o
    arquivo certo entre nomes parecidos, ou saber a que família ele pertence
    antes de escolher o sampler e a resolução.

    Args:
        params (ShowModelInput):
            - name (str): nome exato do arquivo

    Returns:
        str: envelope JSON com os metadados do modelo.
    """
    # `models` não aceita --host nem --port: lê o workspace ativo em disco.
    return await _cli(["models", "show", params.name], timeout=120)


# ---------------------------------------------------------------------
# Templates e edição por slot
# ---------------------------------------------------------------------


class ListTemplatesInput(Base):
    media_type: Optional[str] = Field(default=None, description="Tipo de saída: image, video, audio, 3d.")
    name: Optional[str] = Field(default=None, description="Substring no nome do template.")
    model: Optional[str] = Field(default=None, description="Substring no nome do modelo, ex 'Flux'.")
    limit: Optional[int] = Field(default=30, description="Teto de resultados.", ge=1, le=200)


@_tool(
    name="comfy_list_templates",
    title="Navegar a galeria de templates",
    read_only=True,
    destructive=False,
    idempotent=True,
    open_world=False,
)
async def comfy_list_templates(params: ListTemplatesInput) -> str:
    """Lista os workflows da galeria curada do Comfy.

    Caminho mais rápido para um workflow que funciona: pegue um template
    próximo do que você quer e ajuste os slots, em vez de montar o grafo do
    zero.

    Args:
        params (ListTemplatesInput):
            - media_type, name, model (Optional[str]): filtros
            - limit (Optional[int]): teto de resultados, padrão 30

    Returns:
        str: envelope JSON com os templates. Baixe um com comfy_fetch_template.
    """
    args = ["templates", "ls"]
    _opt(args, "--type", params.media_type)
    _opt(args, "--name", params.name)
    _opt(args, "--model", params.model)
    _opt(args, "--limit", params.limit)
    return await _cli(args, timeout=180)


class TemplateNameInput(Base):
    name: str = Field(
        ...,
        description="Nome do template, igual ao listado por comfy_list_templates.",
        min_length=1,
    )


@_tool(
    name="comfy_check_template",
    title="Conferir se um template roda aqui",
    read_only=True,
    destructive=False,
    idempotent=True,
    open_world=True,
)
async def comfy_check_template(params: TemplateNameInput) -> str:
    """Diz se um template da galeria roda nesta instalação.

    Confronta o que o template exige com o que existe na máquina: classes de
    node e arquivos de modelo. Chame antes de comfy_fetch_template — um
    template da galeria pode depender de custom node ou checkpoint que você
    não tem, e sem esta checagem isso só aparece no erro da execução, depois
    de você já ter montado o resto em cima dele.

    Args:
        params (TemplateNameInput):
            - name (str): nome do template

    Returns:
        str: envelope JSON dizendo se roda e, quando não roda, o que falta.
    """
    return await _cli(["templates", "check", params.name], timeout=180)


@_tool(
    name="comfy_show_template",
    title="Detalhes de um template",
    read_only=True,
    destructive=False,
    idempotent=True,
    open_world=True,
)
async def comfy_show_template(params: TemplateNameInput) -> str:
    """Mostra a ficha completa de um template sem baixar o JSON.

    Serve para escolher entre candidatos que comfy_list_templates devolveu:
    que modelo ele usa, que tipo de saída produz, que tamanho tem. Baixar
    para depois descobrir que era outro custa uma gravação em disco à toa.

    Args:
        params (TemplateNameInput):
            - name (str): nome do template

    Returns:
        str: envelope JSON com os detalhes do template.
    """
    return await _cli(["templates", "show", params.name], timeout=180)


class FetchTemplateInput(TemplateNameInput):
    out_path: str = Field(
        ...,
        description="Onde gravar o JSON. Ex: C:\\comfy\\projeto\\meu.json",
        min_length=1,
    )


@_tool(
    name="comfy_fetch_template",
    title="Baixar o JSON de um template",
    read_only=False,
    destructive=False,
    idempotent=True,
    open_world=True,
)
async def comfy_fetch_template(params: FetchTemplateInput) -> str:
    """Baixa o workflow JSON de um template e grava em out_path.

    Escreve um arquivo. Se out_path já existir, o conteúdo é substituído,
    então aponte para um caminho novo se quiser preservar o anterior.

    Args:
        params (FetchTemplateInput):
            - name (str): nome do template
            - out_path (str): caminho de destino

    Returns:
        str: envelope JSON confirmando a gravação.
    """
    return await _cli(["templates", "fetch", params.name, "--out", params.out_path], timeout=180)


class SlotsInput(Base):
    workflow_path: str = Field(..., description="Caminho do workflow em formato de interface.", min_length=1)
    host: Optional[str] = Field(default=None, description="Host do ComfyUI.")
    port: Optional[int] = Field(default=None, description="Porta do ComfyUI.", ge=1, le=65535)


@_tool(
    name="comfy_workflow_slots",
    title="Listar slots editáveis de um workflow",
    read_only=True,
    destructive=False,
    idempotent=True,
    open_world=False,
)
async def comfy_workflow_slots(params: SlotsInput) -> str:
    """Lista os campos de um workflow que dão para sobrescrever por endereço.

    Chame antes de comfy_set_workflow_slots para descobrir os endereços
    válidos. Evita editar JSON na mão.

    Args:
        params (SlotsInput):
            - workflow_path (str): caminho do workflow
            - host, port (Optional): endereço do servidor

    Returns:
        str: envelope JSON com os slots e seus endereços, no formato usado
            depois em overrides, por exemplo '6.text'.
    """
    args = ["workflow", "slots", params.workflow_path]
    _routing(args, params.host, params.port)
    return await _cli(args, timeout=120)


class NotesInput(Base):
    workflow_path: str = Field(..., description="Caminho do workflow.", min_length=1)


@_tool(
    name="comfy_workflow_notes",
    title="Ler as notas de um workflow",
    read_only=True,
    destructive=False,
    idempotent=True,
    open_world=False,
)
async def comfy_workflow_notes(params: NotesInput) -> str:
    """Lê os nodes Note e MarkdownNote que o autor deixou no workflow.

    É onde mora a instrução que não está em lugar nenhum do grafo: palavra
    de disparo de LoRA, faixa de CFG que funciona, link do modelo certo,
    resolução esperada. Um workflow tecnicamente válido gera imagem ruim
    quando essas instruções são ignoradas, e isso não dá erro nenhum.

    Leia antes de editar slot de template que você não escreveu.

    Args:
        params (NotesInput):
            - workflow_path (str): caminho do workflow

    Returns:
        str: envelope JSON com as notas encontradas.
    """
    return await _cli(["workflow", "notes", params.workflow_path], timeout=120)


class SetSlotsInput(Base):
    workflow_path: str = Field(..., description="Caminho do workflow em formato de interface.", min_length=1)
    overrides: List[str] = Field(
        ...,
        description="Pares ENDEREÇO=VALOR. Ex: ['6.text=uma raposa na neve', '3.seed=42']. "
        "Os endereços vêm de comfy_workflow_slots.",
        min_length=1,
        max_length=40,
    )
    in_place: bool = Field(
        default=False,
        description="True regrava o arquivo original. False, o padrão, devolve o "
        "resultado sem tocar no arquivo. Use True apenas em cópia de trabalho.",
    )


@_tool(
    name="comfy_set_workflow_slots",
    title="Sobrescrever slots de um workflow",
    read_only=False,
    destructive=False,
    idempotent=True,
    open_world=False,
)
async def comfy_set_workflow_slots(params: SetSlotsInput) -> str:
    """Aplica sobrescritas de slot num workflow.

    Atenção ao parâmetro in_place: com True, o arquivo original é regravado
    e o conteúdo anterior se perde. O padrão é False, que devolve o
    resultado pela saída sem alterar o arquivo. Prefira False e grave numa
    cópia, principalmente se o workflow original foi trabalhoso de montar.

    Args:
        params (SetSlotsInput):
            - workflow_path (str): caminho do workflow
            - overrides (List[str]): pares ENDEREÇO=VALOR
            - in_place (bool): regravar o arquivo, padrão False

    Returns:
        str: envelope JSON com o workflow resultante ou a confirmação da
            gravação.
    """
    args = ["workflow", "set-slot", params.workflow_path, *params.overrides]
    args.append("--in-place" if params.in_place else "--stdout")
    return await _cli(args, timeout=120)


# ---------------------------------------------------------------------
# Arquivos
# ---------------------------------------------------------------------


class UploadInput(Base):
    files: List[str] = Field(
        ...,
        description="Caminhos locais a enviar para a pasta de input do ComfyUI. "
        "Necessário para workflows de imagem para imagem.",
        min_length=1,
        max_length=20,
    )
    overwrite: bool = Field(default=False, description="Sobrescrever arquivo de mesmo nome no servidor.")


@_tool(
    name="comfy_upload_files",
    title="Enviar arquivos para o input do ComfyUI",
    read_only=False,
    destructive=False,
    idempotent=False,
    open_world=False,
)
async def comfy_upload_files(params: UploadInput) -> str:
    """Envia arquivos locais para a pasta de input do servidor ComfyUI.

    Com overwrite=True, um arquivo de mesmo nome já presente no servidor é
    substituído e o anterior se perde. O padrão é False.

    Args:
        params (UploadInput):
            - files (List[str]): caminhos locais
            - overwrite (bool): sobrescrever no servidor, padrão False

    Returns:
        str: envelope JSON com os nomes gravados, que são os que devem ser
            referenciados nos nodes de carregamento de imagem.
    """
    # `upload` não aceita --host nem --port: resolve o servidor pelo
    # workspace ativo. Por isso não passa por _routing.
    args = ["upload", *params.files]
    if params.overwrite:
        args.append("--overwrite")
    return await _cli(args, timeout=LONG_TIMEOUT)


class PreviewInput(Base):
    file: str = Field(..., description="Arquivo de imagem, vídeo ou áudio.", min_length=1)
    out_path: Optional[str] = Field(
        default=None, description="PNG de saída. Padrão: <arquivo>.preview.png"
    )
    width: Optional[int] = Field(default=None, description="Largura do preview em pixels.", ge=64, le=4096)


@_tool(
    name="comfy_preview_media",
    title="Gerar PNG de prévia",
    read_only=False,
    destructive=False,
    idempotent=True,
    open_world=False,
)
async def comfy_preview_media(params: PreviewInput) -> str:
    """Gera um PNG de prévia a partir de um arquivo de mídia.

    Imagem vira miniatura, vídeo vira folha de contato com vários quadros,
    áudio vira forma de onda. Serve para o agente conseguir ver o resultado
    de um vídeo, já que não dá para reproduzir vídeo no chat.

    Args:
        params (PreviewInput):
            - file (str): arquivo de mídia
            - out_path (Optional[str]): PNG de saída
            - width (Optional[int]): largura em pixels

    Returns:
        str: envelope JSON com o caminho do PNG gerado.
    """
    args = ["preview", params.file]
    _opt(args, "--out", params.out_path)
    _opt(args, "--width", params.width)
    return await _cli(args, timeout=600)


# ---------------------------------------------------------------------
# Construção de grafo: descoberta de ligações
# ---------------------------------------------------------------------


class ListNodesInput(Base):
    produces: Optional[str] = Field(
        default=None, description="Só nodes cuja saída inclui este tipo. Ex: MODEL, IMAGE, LATENT."
    )
    accepts: Optional[str] = Field(
        default=None, description="Só nodes com ao menos uma entrada deste tipo."
    )
    category: Optional[str] = Field(
        default=None, description="Glob na categoria. Ex: 'loaders*', 'sampling/*'."
    )
    pack: Optional[str] = Field(
        default=None, description="Filtrar por pacote de custom node. Ex: 'core', 'comfyui-impact-pack'."
    )
    output_only: bool = Field(default=False, description="Só nodes terminais de saída.")
    exclude_deprecated: bool = Field(default=False, description="Excluir nodes obsoletos.")
    limit: Optional[int] = Field(default=40, description="Teto de resultados.", ge=1, le=300)
    host: Optional[str] = Field(default=None, description="Host do ComfyUI.")
    port: Optional[int] = Field(default=None, description="Porta do ComfyUI.", ge=1, le=65535)


@_tool(
    name="comfy_list_nodes",
    title="Listar nodes por tipo de entrada ou saída",
    read_only=True,
    destructive=False,
    idempotent=True,
    open_world=False,
)
async def comfy_list_nodes(params: ListNodesInput) -> str:
    """Lista classes de node filtrando por tipo produzido, tipo aceito, categoria ou pacote.

    Esta é a ferramenta de montagem de grafo. Enquanto comfy_search_nodes
    procura por nome, esta procura por forma: "quem produz LATENT",
    "quem aceita CONDITIONING". É assim que se descobre o que ligar onde.

    Args:
        params (ListNodesInput):
            - produces, accepts (Optional[str]): tipos de saída e entrada
            - category, pack (Optional[str]): filtros de origem
            - output_only, exclude_deprecated (bool): filtros booleanos
            - limit (Optional[int]): teto, padrão 40
            - host, port (Optional): endereço do servidor

    Returns:
        str: envelope JSON com as classes que casaram com os filtros.
    """
    args = ["nodes", "ls"]
    _opt(args, "--produces", params.produces)
    _opt(args, "--accepts", params.accepts)
    _opt(args, "--category", params.category)
    _opt(args, "--pack", params.pack)
    if params.output_only:
        args.append("--output-only")
    if params.exclude_deprecated:
        args.append("--exclude-deprecated")
    _opt(args, "--limit", params.limit)
    _routing(args, params.host, params.port)
    return await _cli(args, timeout=120)


class NeighborsInput(Base):
    name: str = Field(..., description="Nome exato da classe de node. Ex: 'KSampler'.", min_length=1)
    direction: str = Field(
        ...,
        description="'upstream' lista quem pode alimentar as entradas deste node. "
        "'downstream' lista quem aceita as saídas dele.",
        pattern="^(upstream|downstream)$",
    )
    limit: Optional[int] = Field(default=30, description="Teto de resultados.", ge=1, le=200)
    host: Optional[str] = Field(default=None, description="Host do ComfyUI.")
    port: Optional[int] = Field(default=None, description="Porta do ComfyUI.", ge=1, le=65535)


@_tool(
    name="comfy_node_neighbors",
    title="Vizinhos ligáveis de um node",
    read_only=True,
    destructive=False,
    idempotent=True,
    open_world=False,
)
async def comfy_node_neighbors(params: NeighborsInput) -> str:
    """Lista o que pode se conectar a um node, para cima ou para baixo.

    Use ao montar um grafo passo a passo: parta de um node conhecido e
    caminhe pelas conexões possíveis em vez de adivinhar nomes de classe.

    Args:
        params (NeighborsInput):
            - name (str): classe de node de referência
            - direction (str): 'upstream' ou 'downstream'
            - limit (Optional[int]): teto, padrão 30
            - host, port (Optional): endereço do servidor

    Returns:
        str: envelope JSON com as classes ligáveis naquela direção.
    """
    args = ["nodes", params.direction, params.name]
    _opt(args, "--limit", params.limit)
    _routing(args, params.host, params.port)
    return await _cli(args, timeout=120)


class NodePathInput(Base):
    from_type: str = Field(..., description="Tipo de partida. Ex: MODEL.", min_length=1)
    to_type: str = Field(..., description="Tipo de destino. Ex: IMAGE.", min_length=1)
    max_depth: Optional[int] = Field(default=None, description="Comprimento máximo do caminho.", ge=1, le=12)
    max_paths: Optional[int] = Field(default=8, description="Quantidade máxima de caminhos.", ge=1, le=50)
    host: Optional[str] = Field(default=None, description="Host do ComfyUI.")
    port: Optional[int] = Field(default=None, description="Porta do ComfyUI.", ge=1, le=65535)


@_tool(
    name="comfy_node_path",
    title="Caminhos entre dois tipos",
    read_only=True,
    destructive=False,
    idempotent=True,
    open_world=False,
)
async def comfy_node_path(params: NodePathInput) -> str:
    """Encontra sequências de nodes que levam de um tipo até outro.

    Responde "como saio de MODEL até IMAGE nesta instalação". É o atalho
    para esboçar a espinha de um workflow sem conhecer o ecossistema de
    nodes de cor.

    Args:
        params (NodePathInput):
            - from_type, to_type (str): tipos de partida e destino
            - max_depth, max_paths (Optional[int]): limites da busca
            - host, port (Optional): endereço do servidor

    Returns:
        str: envelope JSON com os caminhos roteados.
    """
    args = ["nodes", "path", params.from_type, params.to_type]
    _opt(args, "--max-depth", params.max_depth)
    _opt(args, "--max-paths", params.max_paths)
    _routing(args, params.host, params.port)
    return await _cli(args, timeout=180)


# ---------------------------------------------------------------------
# Construção de grafo: fragmentos e blueprints
# ---------------------------------------------------------------------


class ComposeInput(Base):
    blueprint_path: str = Field(
        ...,
        description="Arquivo YAML de blueprint que combina fragmentos.",
        min_length=1,
    )
    out_path: Optional[str] = Field(
        default=None,
        description="JSON de saída. Padrão: <blueprint>.compiled.json",
    )
    lib_dir: Optional[str] = Field(
        default=None, description="Diretório da biblioteca de fragmentos. Padrão: ./fragments"
    )


@_tool(
    name="comfy_compose_workflow",
    title="Compilar blueprint em workflow",
    read_only=False,
    destructive=False,
    idempotent=True,
    open_world=False,
)
async def comfy_compose_workflow(params: ComposeInput) -> str:
    """Compila um blueprint YAML de fragmentos num único workflow em formato API.

    Este é o caminho padrão para workflow que você vai estender, variar ou
    reutilizar. Em vez de editar JSON gigante na mão, você escreve um
    blueprint pequeno e compila.

    Grava um arquivo. Se out_path já existir, o conteúdo anterior é
    substituído, então use um caminho novo se quiser preservar a versão
    anterior.

    Args:
        params (ComposeInput):
            - blueprint_path (str): YAML de entrada
            - out_path (Optional[str]): JSON de saída
            - lib_dir (Optional[str]): biblioteca de fragmentos

    Returns:
        str: envelope JSON com o caminho do workflow compilado.
    """
    args = ["workflow", "compose", params.blueprint_path]
    _opt(args, "--out", params.out_path)
    _opt(args, "--lib", params.lib_dir)
    return await _cli(args, timeout=300)


class DecomposeInput(Base):
    workflow_path: str = Field(..., description="Workflow JSON, formato API ou de interface.", min_length=1)
    name: Optional[str] = Field(default=None, description="Nome do fragmento. Padrão: nome do arquivo.")
    out_path: Optional[str] = Field(default=None, description="Caminho de saída. Padrão: <lib>/<nome>.json")
    lib_dir: Optional[str] = Field(default=None, description="Biblioteca de fragmentos. Padrão: ./fragments")
    host: Optional[str] = Field(default=None, description="Host do ComfyUI.")
    port: Optional[int] = Field(default=None, description="Porta do ComfyUI.", ge=1, le=65535)


@_tool(
    name="comfy_decompose_workflow",
    title="Projetar workflow em fragmento",
    read_only=False,
    destructive=False,
    idempotent=True,
    open_world=False,
)
async def comfy_decompose_workflow(params: DecomposeInput) -> str:
    """Converte um workflow existente num fragmento reutilizável. Inverso de compose.

    Uso típico: baixe um template com comfy_fetch_template, projete em
    fragmento com esta ferramenta, e a partir daí monte blueprints próprios.
    É como se pega conhecimento de um workflow que funciona sem herdar o
    JSON inteiro.

    Grava um arquivo na biblioteca de fragmentos, substituindo um de mesmo
    nome se já existir.

    Args:
        params (DecomposeInput):
            - workflow_path (str): workflow de origem
            - name, out_path, lib_dir (Optional[str]): destino
            - host, port (Optional): endereço do servidor

    Returns:
        str: envelope JSON com o fragmento gerado e suas portas.
    """
    args = ["workflow", "decompose", params.workflow_path]
    _opt(args, "--name", params.name)
    _opt(args, "--out", params.out_path)
    _opt(args, "--lib", params.lib_dir)
    _routing(args, params.host, params.port)
    return await _cli(args, timeout=300)


class VaryInput(Base):
    workflow_path: str = Field(..., description="Workflow em formato de interface.", min_length=1)
    slots: List[str] = Field(
        ...,
        description="Uma entrada por slot, no formato ENDEREÇO=[\"v1\",\"v2\"]. "
        "As listas são combinadas em paralelo, então todas precisam do mesmo "
        "comprimento. Endereços vêm de comfy_workflow_slots.",
        min_length=1,
        max_length=10,
    )
    out_dir: Optional[str] = Field(
        default=None,
        description="Pasta para gravar cada variante. Sem ela, as variantes saem "
        "apenas na resposta, sem tocar em disco.",
    )
    host: Optional[str] = Field(default=None, description="Host do ComfyUI.")
    port: Optional[int] = Field(default=None, description="Porta do ComfyUI.", ge=1, le=65535)


@_tool(
    name="comfy_vary_workflow",
    title="Gerar variantes de um workflow",
    read_only=False,
    destructive=False,
    idempotent=True,
    open_world=False,
)
async def comfy_vary_workflow(params: VaryInput) -> str:
    """Produz N variantes de um workflow a partir de listas de valores por slot.

    Feito para lote: dez prompts, cinco seeds, três escalas. Gera os
    arquivos, depois submeta cada um com comfy_run_workflow e aguarde todos
    com comfy_job_wait.

    Com out_dir preenchido, grava um arquivo por variante nessa pasta.
    Arquivos de mesmo nome de um lote anterior são substituídos, então use
    uma pasta por lote.

    Args:
        params (VaryInput):
            - workflow_path (str): workflow base
            - slots (List[str]): listas de valores por endereço
            - out_dir (Optional[str]): pasta de saída
            - host, port (Optional): endereço do servidor

    Returns:
        str: envelope JSON com as variantes geradas.
    """
    args = ["workflow", "vary", params.workflow_path]
    for slot in params.slots:
        args.extend(["--slot", slot])
    _opt(args, "--out-dir", params.out_dir)
    _routing(args, params.host, params.port)
    return await _cli(args, timeout=300)


class FragmentsInput(Base):
    lib_dir: Optional[str] = Field(
        default=None, description="Diretório da biblioteca. Padrão: ./fragments"
    )


@_tool(
    name="comfy_list_fragments",
    title="Listar fragmentos da biblioteca",
    read_only=True,
    destructive=False,
    idempotent=True,
    open_world=False,
)
async def comfy_list_fragments(params: FragmentsInput) -> str:
    """Lista os fragmentos já existentes na biblioteca do projeto.

    Consulte antes de decompor um template novo: talvez o fragmento de que
    você precisa já tenha sido criado numa sessão anterior.

    Args:
        params (FragmentsInput):
            - lib_dir (Optional[str]): diretório da biblioteca

    Returns:
        str: envelope JSON com os fragmentos disponíveis.
    """
    args = ["workflow", "fragment", "ls"]
    _opt(args, "--lib", params.lib_dir)
    return await _cli(args, timeout=120)


# O que fazer diante de cada recusa da aba. O hint é o que separa uma falha
# acionável de um beco: sem ele o agente afirma sucesso ou desiste, e nos dois
# casos quem descobre o problema é a pessoa, olhando a tela.
_HINTS_LIVE = {
    "canvas_mudou": (
        "A pessoa mexeu no canvas depois da sua última leitura, então esta edição "
        "saiu de um estado velho e a aba a recusou para não apagar o trabalho dela. "
        "Releia com comfy_read_canvas, refaça a edição sobre o que veio, e publique "
        "de novo."
    ),
    "espelhamento_desligado": (
        "A opção 'Aplicar edições automaticamente' está desligada nas configurações "
        "do ComfyUI, em Welt Live. Não há aplicação manual: republicar não adianta. "
        "Grave com comfy_workflow_library e peça para abrir pela barra lateral, ou "
        "avise que a opção precisa ser religada."
    ),
    "sem_aba": (
        "Nenhuma aba do ComfyUI está aberta, então não há tela para espelhar. O "
        "arquivo em disco está correto; grave com comfy_workflow_library se quiser "
        "que fique disponível na barra lateral."
    ),
    "canvas_ausente": (
        "A aba respondeu antes de o canvas existir. Costuma ser página ainda "
        "carregando: tente publicar de novo em alguns segundos."
    ),
    "erro_ao_aplicar": (
        "A aba falhou ao aplicar o grafo; o motivo está no console do navegador. O "
        "arquivo em disco está correto."
    ),
}


async def _publish_live(workflow_path: str, host: Optional[str], port: Optional[int]) -> dict:
    """Manda o grafo recém-editado para o canvas aberto do ComfyUI.

    Depende da extensão comfyui-welt-live estar instalada no ComfyUI: é o
    único caminho para um evento chegar à aba aberta, já que o /ws do
    ComfyUI não aceita comando de fora e nenhuma rota dele transmite evento
    arbitrário.

    Nunca levanta. O canvas não acompanhar é uma decepção, não uma falha da
    edição — o arquivo já foi gravado. O resultado entra no envelope como
    bloco `live` para o agente saber se a pessoa viu acontecer ou se
    precisa abrir o workflow na mão.
    """
    if not LIVE_ENABLED:
        return {"published": False, "reason": "desligado por COMFY_MCP_LIVE"}

    try:
        import httpx2

        with open(workflow_path, "r", encoding="utf-8") as fh:
            grafo = json.load(fh)
        base = _http_base(host, port)
        # O teto acomoda a espera da rota pela confirmação da aba, com folga.
        async with httpx2.AsyncClient(base_url=base, timeout=20.0) as cliente:
            r = await cliente.post(
                "/welt-live/publish",
                json={"graph": grafo, "name": os.path.basename(workflow_path)},
            )
        if r.status_code == 404:
            return {
                "published": False,
                "reason": "extensao_ausente",
                "hint": "Instale comfyui-welt-live no custom_nodes/ do ComfyUI para o "
                "canvas acompanhar as edições. Sem ela, publique com "
                "comfy_workflow_library e abra pela barra lateral.",
            }
        if r.status_code >= 400:
            return {"published": False, "reason": f"http_{r.status_code}"}

        dados = r.json()
        # `published` agora carrega o que a aba respondeu, e não o que nós
        # conseguimos transmitir. Uma extensão antiga, sem a rota de
        # confirmação, não devolve o campo: aí cai em None, que é o valor
        # honesto — transmitimos e não sabemos o que aconteceu.
        aplicado = dados.get("applied")
        resultado = {
            "published": aplicado,
            "clients": dados.get("clients"),
            "version": dados.get("version"),
        }
        motivo = dados.get("reason")
        if motivo:
            resultado["reason"] = motivo
        if aplicado is False:
            resultado["hint"] = _HINTS_LIVE.get(
                motivo, "A aba não aplicou a edição; o arquivo em disco está correto."
            )
        elif aplicado is None:
            resultado["hint"] = (
                "A aba não confirmou a tempo, então não dá para afirmar que a edição "
                "está na tela. Confira com comfy_read_canvas antes de dizer que chegou."
            )
        return resultado
    except Exception as exc:
        return {"published": False, "reason": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------------
# Construção de grafo: edição estruturada
#
# O comfy-cli chama estes verbos de "structured, CRDT-ready edit
# primitives": cada um devolve em data.op a operação que aplicou, de forma
# replayável. São eles que permitem montar um grafo node a node em vez de
# só trocar valor de slot num workflow que já existe.
#
# Ficam agrupados sob uma ferramenta com `action` de propósito. Oito
# ferramentas avulsas custariam contexto em toda sessão sem dar nada em
# troca, já que compartilham o mesmo arquivo de trabalho e o mesmo envelope.
# ---------------------------------------------------------------------


GraphAction = Literal[
    "add_node",
    "connect",
    "set_widget",
    "delete_nodes",
    "clear",
    "reset_doc",
    "ls_nodes",
    "print",
]


class EditGraphInput(Base):
    action: GraphAction = Field(
        ...,
        description="Qual edição aplicar. add_node acrescenta um node; connect liga "
        "uma saída a uma entrada; set_widget muda um valor; delete_nodes remove; "
        "clear esvazia; reset_doc zera inclusive o histórico; ls_nodes e print "
        "apenas leem.",
    )
    workflow_path: str = Field(
        ...,
        description="Workflow em formato de interface. É o arquivo editado no lugar, "
        "salvo com stdout=True.",
        min_length=1,
    )
    class_type: Optional[str] = Field(
        default=None,
        description="add_node: classe do node, ex 'KSampler'. O nome exato sai de "
        "comfy_search_nodes.",
    )
    at: Optional[str] = Field(
        default=None,
        description="add_node: posição no canvas, no formato 'x,y'. Sem ela o node "
        "entra na posição padrão.",
    )
    allow_deprecated: bool = Field(
        default=False,
        description="add_node: adicionar mesmo que o catálogo marque a classe como "
        "obsoleta.",
    )
    source: Optional[str] = Field(
        default=None,
        description="connect: origem, '<id_do_node>.<slot_de_saída>'. O slot aceita "
        "nome ou índice.",
    )
    target: Optional[str] = Field(
        default=None,
        description="connect: destino, '<id_do_node>.<slot_de_entrada>'.",
    )
    addr: Optional[str] = Field(
        default=None,
        description="set_widget: endereço do widget, '<id_do_node>.<nome_do_widget>'. "
        "Os endereços válidos saem de comfy_workflow_slots.",
    )
    value: Optional[str] = Field(
        default=None,
        description="set_widget: valor novo. É lido como JSON e, se não for JSON "
        "válido, vira string literal.",
    )
    nodes: Optional[List[str]] = Field(
        default=None,
        description="delete_nodes: ids a remover. Vários numa chamada saem numa "
        "gravação atômica — ou todos entram, ou nenhum.",
        max_length=64,
    )
    confirm: bool = Field(
        default=False,
        description="reset_doc: obrigatório. Sem ele o comando falha fechado e não "
        "grava nada, que é a proteção contra zerar um grafo por engano.",
    )
    stdout: bool = Field(
        default=False,
        description="True devolve o workflow resultante sem tocar no arquivo. False, "
        "o padrão, regrava o original — o comfy-cli grava de forma atômica.",
    )
    host: Optional[str] = Field(default=None, description="Host do ComfyUI, para o catálogo de nodes.")
    port: Optional[int] = Field(default=None, description="Porta do ComfyUI.", ge=1, le=65535)


def _missing(action: str, campos: str, hint: str) -> str:
    """Envelope de campo faltando, no mesmo formato dos erros da CLI."""
    return json.dumps(
        {
            "ok": False,
            "error": {
                "code": "missing_input",
                "message": f"action='{action}' exige {campos}.",
                "hint": hint,
            },
        },
        indent=2,
        ensure_ascii=False,
    )


@_tool(
    name="comfy_edit_graph",
    title="Editar o grafo de um workflow",
    read_only=False,
    destructive=True,
    idempotent=False,
    open_world=False,
)
async def comfy_edit_graph(params: EditGraphInput) -> str:
    """Monta e altera o grafo de um workflow node a node.

    É a diferença entre ajustar um workflow pronto e construir um. As
    ferramentas de slot mudam valores dentro de uma estrutura existente;
    estas mudam a estrutura: acrescentam node, ligam saída em entrada,
    removem o que sobrou.

    Cada ação devolve em data.op a operação aplicada, com base_version e
    version. Guarde-as se for reconstruir a sequência depois — é o mesmo
    formato que comfy_graph_recipe consome.

    O caminho normal é: comfy_search_nodes ou comfy_list_nodes para achar a
    classe, comfy_show_node para ver os slots dela, add_node, connect,
    set_widget, e comfy_validate_workflow antes de rodar.

    Por padrão o arquivo original é regravado. Use stdout=True para receber
    o resultado sem tocar em disco quando o grafo de partida for trabalhoso.

    Args:
        params (EditGraphInput):
            - action (str): qual edição aplicar
            - workflow_path (str): o workflow a editar
            - demais campos: dependem da action, cada um descrito no schema

    Returns:
        str: envelope JSON com a operação aplicada. Em erro, error.hint diz
            o que corrigir; classe inexistente traz details.close_matches.
    """
    a = params.action
    args: List[str]

    if a == "add_node":
        if not params.class_type:
            return _missing(a, "class_type", "passe a classe do node, ex 'KSampler'. Ache o nome exato com comfy_search_nodes.")
        args = ["workflow", "add-node", params.workflow_path, params.class_type]
        _opt(args, "--at", params.at)
        if params.allow_deprecated:
            args.append("--allow-deprecated")
    elif a == "connect":
        if not params.source or not params.target:
            return _missing(a, "source e target", "use '<id_do_node>.<slot>' nos dois. comfy_show_node lista os slots de uma classe.")
        args = ["workflow", "connect", params.workflow_path, params.source, params.target]
    elif a == "set_widget":
        if not params.addr or params.value is None:
            return _missing(a, "addr e value", "addr é '<id_do_node>.<widget>'; os endereços válidos saem de comfy_workflow_slots.")
        args = ["workflow", "set-widget", params.workflow_path, params.addr, params.value]
    elif a == "delete_nodes":
        if not params.nodes:
            return _missing(a, "nodes", "passe os ids a remover. comfy_edit_graph com action='ls_nodes' lista os ids do arquivo.")
        args = ["workflow", "delete-nodes", params.workflow_path, *params.nodes]
    elif a == "clear":
        args = ["workflow", "clear", params.workflow_path]
    elif a == "reset_doc":
        if not params.confirm:
            return _missing(
                a,
                "confirm=True",
                "reset_doc apaga nodes, ids e o histórico de replay. Confirme com o "
                "usuário antes e só então passe confirm=True.",
            )
        args = ["workflow", "reset-doc", params.workflow_path, "--confirm"]
    elif a == "ls_nodes":
        # Não aceita --stdout nem roteamento: lê o arquivo e lista.
        return await _cli(["workflow", "ls-nodes", params.workflow_path], timeout=120)
    else:  # print
        args = ["workflow", "print", params.workflow_path]
        _routing(args, params.host, params.port)
        return await _cli(args, timeout=120)

    if params.stdout:
        args.append("--stdout")
    # clear e reset-doc resolvem tudo no arquivo; os demais consultam o
    # catálogo de nodes da instalação viva para validar classe e slot.
    if a not in ("clear", "reset_doc"):
        _routing(args, params.host, params.port)

    envelope = await _cli_obj(args, timeout=120)
    # Só faz sentido espelhar o que foi de fato gravado: com stdout=True o
    # arquivo não mudou, e numa edição que falhou não há nada novo para ver.
    if envelope.get("ok") and not params.stdout:
        envelope["live"] = await _publish_live(params.workflow_path, params.host, params.port)
    return _truncate(json.dumps(envelope, indent=2, ensure_ascii=False))


RecipeAction = Literal["apply", "capture", "foreach"]


class GraphRecipeInput(Base):
    action: RecipeAction = Field(
        ...,
        description="apply aplica um lote de ops num workflow; capture projeta um "
        "workflow na receita que o reconstrói; foreach instancia uma receita sobre N "
        "conjuntos de parâmetros.",
    )
    workflow_path: Optional[str] = Field(
        default=None, description="apply e capture: o workflow de trabalho."
    )
    recipe_path: Optional[str] = Field(
        default=None,
        description="apply: arquivo de ops a aplicar. foreach: a receita "
        "{params, ops} a instanciar.",
    )
    params_path: Optional[str] = Field(
        default=None,
        description="foreach: arquivo com os conjuntos de parâmetros — array JSON de "
        "objetos, um objeto só, ou JSONL.",
    )
    out_dir: Optional[str] = Field(
        default=None, description="foreach: pasta onde gravar os N workflows gerados."
    )
    out_path: Optional[str] = Field(
        default=None, description="capture: onde gravar a receita. Sem ela, volta na resposta."
    )
    name: Optional[str] = Field(default=None, description="capture: nome da receita.")
    overrides: Optional[List[str]] = Field(
        default=None,
        description="apply: pares 'chave=valor' dos params da receita. capture: pares "
        "'<id_do_node>.<widget>=<nome_do_param>' para promover um widget a parâmetro.",
        max_length=40,
    )
    stdout: bool = Field(
        default=False, description="apply: devolver o resultado sem regravar o arquivo."
    )
    host: Optional[str] = Field(default=None, description="Host do ComfyUI, para o catálogo de nodes.")
    port: Optional[int] = Field(default=None, description="Porta do ComfyUI.", ge=1, le=65535)


@_tool(
    name="comfy_graph_recipe",
    title="Receitas de grafo: aplicar, capturar, multiplicar",
    read_only=False,
    destructive=False,
    idempotent=True,
    open_world=False,
)
async def comfy_graph_recipe(params: GraphRecipeInput) -> str:
    """Trabalha com a sequência de edições que constrói um grafo, e não com o grafo.

    Uma receita é o lote de operações que reconstrói um workflow do zero,
    com alguns widgets promovidos a parâmetro. É o que separa "tenho este
    JSON" de "sei montar este tipo de grafo".

    - capture: parte de um workflow que funciona e devolve a receita dele.
    - apply: aplica essa receita, com outros parâmetros, num workflow.
    - foreach: instancia a receita sobre N conjuntos de parâmetros de uma
      vez, gerando N workflows — é o caminho de lote quando a variação
      muda a estrutura, e não só valores. Para variar só valores,
      comfy_vary_workflow é mais direto.

    Args:
        params (GraphRecipeInput):
            - action (str): apply, capture ou foreach
            - demais campos: dependem da action, descritos no schema

    Returns:
        str: envelope JSON com a receita, o resultado da aplicação, ou os
            caminhos dos workflows gerados. O apply traz também o bloco
            `live` com o resultado do espelhamento no canvas, pela mesma
            regra do comfy_edit_graph; capture e foreach não espelham, por
            não terem um grafo único a mostrar.
    """
    a = params.action
    if a == "apply":
        if not params.workflow_path or not params.recipe_path:
            return _missing(a, "workflow_path e recipe_path", "recipe_path é o arquivo de ops; gere um com action='capture'.")
        args = ["workflow", "apply", params.workflow_path, "--ops", params.recipe_path]
        for par in params.overrides or []:
            args.extend(["--param", par])
        if params.stdout:
            args.append("--stdout")
    elif a == "capture":
        if not params.workflow_path:
            return _missing(a, "workflow_path", "aponte o workflow que já funciona e que você quer transformar em receita.")
        args = ["workflow", "capture", params.workflow_path]
        _opt(args, "--name", params.name)
        _opt(args, "--out", params.out_path)
        for par in params.overrides or []:
            args.extend(["--param", par])
    else:  # foreach
        if not params.recipe_path or not params.params_path or not params.out_dir:
            return _missing(
                a,
                "recipe_path, params_path e out_dir",
                "foreach grava um arquivo por conjunto de parâmetros, então precisa da pasta de destino.",
            )
        args = [
            "workflow",
            "foreach",
            params.recipe_path,
            "--params",
            params.params_path,
            "--out-dir",
            params.out_dir,
        ]

    _routing(args, params.host, params.port)
    envelope = await _cli_obj(args, timeout=300)

    # apply espelha no canvas pela mesma regra do comfy_edit_graph: só o que
    # foi gravado, e só quando deu certo. A simetria é o ponto — os dois
    # terminam carregando o grafo inteiro na tela, então não há lote mais
    # perigoso que uma op solta, e a guarda de canvas sujo protege os dois
    # igual. Quem não quer tocar no arquivo usa stdout=True, e aí não há o
    # que publicar.
    #
    # capture e foreach ficam de fora por não terem grafo a mostrar: o
    # primeiro escreve uma receita, o segundo gera N workflows.
    if a == "apply" and envelope.get("ok") and not params.stdout and params.workflow_path:
        envelope["live"] = await _publish_live(params.workflow_path, params.host, params.port)
    return _truncate(json.dumps(envelope, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------
# Biblioteca de workflows do ComfyUI
#
# Esta seção fala HTTP direto com o ComfyUI, não pelo comfy-cli. É de
# propósito: a rota /userdata é o que alimenta a lista de workflows da
# barra lateral da interface, então gravar ali faz o grafo aparecer no
# ComfyUI no mesmo instante. Pelo CLI seria um subprocesso a mais para
# chegar no mesmo lugar.
#
# O cliente HTTP vem de graça: httpx2 já é dependência do SDK do MCP.
# ---------------------------------------------------------------------

LibraryAction = Literal["list", "get", "save", "delete"]

# Onde a interface do ComfyUI procura os workflows da barra lateral.
LIBRARY_DIR = "workflows"


class WorkflowLibraryInput(Base):
    action: LibraryAction = Field(
        ...,
        description="list mostra o que está na biblioteca; get lê um workflow; save "
        "grava (é o que faz aparecer no ComfyUI); delete remove.",
    )
    name: Optional[str] = Field(
        default=None,
        description="Nome na biblioteca, ex 'retrato.json'. Subpasta é aceita: "
        "'projeto/retrato.json'. Obrigatório em get, save e delete.",
    )
    workflow_path: Optional[str] = Field(
        default=None,
        description="save: arquivo local a enviar. É o workflow que você acabou de "
        "montar com comfy_edit_graph.",
    )
    overwrite: bool = Field(
        default=False,
        description="save: True substitui um workflow de mesmo nome já na biblioteca. "
        "O padrão recusa, para não apagar trabalho do usuário sem ele pedir.",
    )
    host: Optional[str] = Field(default=None, description="Host do ComfyUI, padrão 127.0.0.1.")
    port: Optional[int] = Field(default=None, description="Porta do ComfyUI, padrão 8188.", ge=1, le=65535)


def _http_base(host: Optional[str], port: Optional[int]) -> str:
    """Endereço HTTP do ComfyUI: parâmetro da chamada vence COMFY_LOCAL_URL."""
    h = host or DEFAULT_HOST or "127.0.0.1"
    p = port or DEFAULT_PORT or 8188
    return f"http://{h}:{p}"


def _http_error(code: str, message: str, hint: str, **extra: Any) -> str:
    """Erro HTTP no mesmo formato de envelope que a CLI devolve.

    Quem chama trata os dois caminhos igual, e a regra de sempre ler o
    campo hint continua valendo mesmo nas ferramentas que não passam pela
    CLI.
    """
    erro = {"ok": False, "error": {"code": code, "message": message, "hint": hint}}
    erro.update(extra)
    return json.dumps(erro, indent=2, ensure_ascii=False)


@_tool(
    name="comfy_workflow_library",
    title="Biblioteca de workflows do ComfyUI",
    read_only=False,
    destructive=True,
    idempotent=True,
    open_world=False,
)
async def comfy_workflow_library(params: WorkflowLibraryInput) -> str:
    """Põe um workflow na biblioteca do ComfyUI, onde o usuário consegue abrir.

    Esta é a ponte entre o que o agente monta e o que a pessoa vê. Um
    workflow num arquivo solto no disco não existe para quem está olhando a
    interface; gravado aqui, ele aparece na barra lateral de Workflows do
    ComfyUI na hora, a um clique de ser aberto no canvas.

    Feche o ciclo com ela: monte o grafo com comfy_edit_graph, confira com
    comfy_validate_workflow, e então save — em vez de entregar um caminho
    de arquivo e pedir para a pessoa importar na mão.

    Fala HTTP direto com o ComfyUI, então não depende do comfy-cli, mas
    exige o servidor no ar. Se ele estiver fora, suba com
    comfy_launch_server.

    delete apaga da biblioteca do usuário e não tem desfazer; save com
    overwrite=True substitui. Confirme antes em workflow que você não criou.

    Args:
        params (WorkflowLibraryInput):
            - action (str): list, get, save ou delete
            - name (Optional[str]): nome na biblioteca
            - workflow_path (Optional[str]): arquivo local, para save
            - overwrite (bool): substituir no save, padrão False
            - host, port (Optional): endereço do servidor

    Returns:
        str: envelope JSON. Em erro, error.hint diz o que corrigir.
    """
    import httpx2

    base = _http_base(params.host, params.port)
    a = params.action

    if a != "list" and not params.name:
        return _missing(a, "name", "passe o nome do workflow na biblioteca, ex 'retrato.json'.")

    corpo: Optional[bytes] = None
    if a == "save":
        if not params.workflow_path:
            return _missing(a, "workflow_path", "aponte o arquivo local do workflow a enviar.")
        try:
            with open(params.workflow_path, "rb") as fh:
                corpo = fh.read()
        except OSError as exc:
            return _http_error(
                "workflow_not_readable",
                f"Não foi possível ler {params.workflow_path}: {exc}",
                "Confira o caminho. É o mesmo arquivo que comfy_edit_graph gravou.",
            )

    alvo = f"{LIBRARY_DIR}/{params.name}" if params.name else LIBRARY_DIR
    # safe="" para a barra da subpasta virar %2F: a rota recebe o caminho
    # inteiro num único segmento e o decodifica do outro lado.
    codificado = quote(alvo, safe="")

    try:
        async with httpx2.AsyncClient(base_url=base, timeout=30.0) as cliente:
            if a == "list":
                r = await cliente.get(
                    "/userdata", params={"dir": LIBRARY_DIR, "recurse": "true", "full_info": "true"}
                )
            elif a == "get":
                r = await cliente.get(f"/userdata/{codificado}")
            elif a == "save":
                r = await cliente.post(
                    f"/userdata/{codificado}",
                    params={"overwrite": "true" if params.overwrite else "false", "full_info": "true"},
                    content=corpo,
                )
            else:  # delete
                r = await cliente.delete(f"/userdata/{codificado}")
    except Exception as exc:
        # httpx2 levanta várias formas de erro de rede; todas significam a
        # mesma coisa para quem chama, e nenhuma deve virar traceback.
        return _http_error(
            "server_not_running",
            f"Não foi possível falar com o ComfyUI em {base}: {exc}",
            "Suba o servidor com comfy_launch_server, ou passe host e port se ele "
            "estiver em outro endereço.",
        )

    if r.status_code == 404:
        return _http_error(
            "workflow_not_found",
            f"'{alvo}' não existe na biblioteca.",
            "Liste o que existe com action='list'.",
        )
    if r.status_code == 409:
        return _http_error(
            "workflow_exists",
            f"'{alvo}' já existe na biblioteca.",
            "Escolha outro nome, ou passe overwrite=True depois de confirmar com o "
            "usuário que o workflow anterior pode ser substituído.",
        )
    if r.status_code == 403:
        return _http_error(
            "path_rejected",
            f"O ComfyUI recusou o caminho '{alvo}'.",
            "Use um nome simples, sem '..' nem caminho absoluto.",
        )
    if r.status_code >= 400:
        return _http_error(
            "userdata_error",
            f"O ComfyUI respondeu {r.status_code}.",
            "Veja raw_body abaixo.",
            raw_body=r.text[:2000],
        )

    if a == "delete":
        dados: Any = {"deleted": alvo}
    elif a == "get":
        try:
            dados = json.loads(r.text)
        except json.JSONDecodeError:
            dados = {"raw": r.text[:8000]}
    else:
        try:
            dados = r.json()
        except Exception:
            dados = {"raw": r.text[:8000]}

    return _truncate(
        json.dumps({"ok": True, "command": f"library {a}", "data": dados}, indent=2, ensure_ascii=False)
    )


# ---------------------------------------------------------------------
# Leitura do canvas aberto
#
# O par de _publish_live, no sentido contrário. O grafo que a pessoa está
# vendo vive no navegador, não no processo do ComfyUI nem em disco — sem
# isto, editar o que ela abriu exigiria que ela salvasse antes.
# ---------------------------------------------------------------------


class ReadCanvasInput(Base):
    out_path: str = Field(
        ...,
        description="Onde gravar o grafo lido. É o arquivo que você passa depois "
        "para comfy_edit_graph em workflow_path.",
        min_length=1,
    )
    host: Optional[str] = Field(default=None, description="Host do ComfyUI, padrão 127.0.0.1.")
    port: Optional[int] = Field(default=None, description="Porta do ComfyUI, padrão 8188.", ge=1, le=65535)


@_tool(
    name="comfy_read_canvas",
    title="Ler o grafo que está aberto no ComfyUI",
    read_only=False,
    destructive=False,
    idempotent=True,
    open_world=False,
)
async def comfy_read_canvas(params: ReadCanvasInput) -> str:
    """Traz para um arquivo o grafo que a pessoa está vendo no canvas agora.

    É o começo do ciclo de mão dupla: leia o canvas, edite com
    comfy_edit_graph, e cada edição volta para a tela dela. Sem isto, você
    só alcança o que já foi salvo em disco ou na biblioteca — e pedir para
    a pessoa salvar antes de cada conversa é atrito que não precisa existir.

    Use quando ela disser "edita o workflow que está aberto", "muda esse
    prompt aí", ou qualquer coisa que se refira ao que está na tela. Para um
    workflow salvo e não aberto, comfy_workflow_library com action="get".

    Grava o arquivo em out_path, substituindo o que houver lá. Exige a
    extensão comfyui-welt-live instalada e ao menos uma aba aberta.

    Ao responder, a extensão registra este estado como conhecido, então as
    edições que você fizer em cima dele entram no canvas sem pedir
    confirmação. Se a pessoa mexer na tela nesse meio tempo, a guarda volta
    a valer e ela decide.

    Args:
        params (ReadCanvasInput):
            - out_path (str): onde gravar o grafo
            - host, port (Optional): endereço do servidor

    Returns:
        str: envelope JSON com o caminho gravado, o nome do workflow quando
            conhecido, e a contagem de nodes e ligações.
    """
    import httpx2

    base = _http_base(params.host, params.port)

    async def pedir():
        async with httpx2.AsyncClient(base_url=base, timeout=60.0) as cliente:
            return await cliente.post("/welt-live/pull")

    # Duas tentativas de propósito: o navegador adormece a aba que não está
    # em foco, e a do ComfyUI quase sempre está atrás da janela onde a pessoa
    # conversa com o agente. Uma aba adormecida costuma acordar no primeiro
    # pedido e responder no segundo — e uma leitura falhar por isso, com o
    # ComfyUI no ar e tudo certo, é o tipo de erro que faz o plugin parecer
    # quebrado quando não está.
    try:
        r = await pedir()
        if r.status_code == 504:
            r = await pedir()
    except Exception as exc:
        return _http_error(
            "server_not_running",
            f"Não foi possível falar com o ComfyUI em {base}: {exc}",
            "Suba o servidor com comfy_launch_server, ou passe host e port se ele "
            "estiver em outro endereço.",
        )

    if r.status_code == 404:
        return _http_error(
            "extensao_ausente",
            "A rota de leitura não existe neste ComfyUI.",
            "Instale comfyui-welt-live no custom_nodes/ do ComfyUI e reinicie. Sem "
            "ela, leia um workflow salvo com comfy_workflow_library action='get'.",
        )
    if r.status_code == 409:
        return _http_error(
            "sem_aba",
            "Nenhuma aba do ComfyUI está aberta.",
            "Peça para a pessoa abrir o ComfyUI no navegador; sem aba não há canvas "
            "para ler.",
        )
    if r.status_code == 504:
        return _http_error(
            "sem_resposta",
            "A aba não respondeu ao pedido a tempo, nem na segunda tentativa.",
            "Traga a janela do ComfyUI para a frente e tente de novo: o navegador "
            "adormece aba fora de foco. Se persistir, recarregue a página — é o "
            "sintoma de a extensão não ter carregado nela.",
        )
    if r.status_code >= 400:
        return _http_error(
            "pull_error",
            f"O ComfyUI respondeu {r.status_code}.",
            "Veja raw_body abaixo.",
            raw_body=r.text[:2000],
        )

    try:
        corpo = r.json()
    except Exception:
        return _http_error(
            "resposta_invalida",
            "A resposta da leitura não é JSON.",
            "Veja raw_body abaixo.",
            raw_body=r.text[:2000],
        )

    grafo = corpo.get("graph")
    if not isinstance(grafo, dict):
        return _http_error(
            "canvas_vazio",
            "A aba respondeu sem grafo.",
            "O canvas pode estar vazio, ou a serialização falhou; o console do "
            "navegador traz o motivo.",
        )

    try:
        with open(params.out_path, "w", encoding="utf-8") as fh:
            json.dump(grafo, fh, indent=2, ensure_ascii=False)
    except OSError as exc:
        return _http_error(
            "out_path_not_writable",
            f"Não foi possível gravar em {params.out_path}: {exc}",
            "Escolha um caminho em que você consiga escrever.",
        )

    return _truncate(
        json.dumps(
            {
                "ok": True,
                "command": "read canvas",
                "data": {
                    "wrote": params.out_path,
                    "name": corpo.get("name"),
                    "nodes": len(grafo.get("nodes") or []),
                    "links": len(grafo.get("links") or []),
                },
            },
            indent=2,
            ensure_ascii=False,
        )
    )


# ---------------------------------------------------------------------
# Dependências da instalação
#
# As três ferramentas desta seção mudam o que existe na máquina. Confirme
# com o usuário antes: baixar modelo consome banda e disco, e instalar
# custom node altera o ambiente do ComfyUI.
# ---------------------------------------------------------------------


class DownloadModelInput(Base):
    url: str = Field(
        ...,
        description="URL direta do arquivo. Modelo em host que exige login precisa "
        "de token configurado antes, fora daqui.",
        min_length=1,
    )
    relative_path: Optional[str] = Field(
        default=None,
        description="Pasta de destino relativa ao workspace, ex 'models/checkpoints'. "
        "Os nomes válidos saem de comfy_search_models com folders_only=true.",
    )
    filename: Optional[str] = Field(
        default=None, description="Nome do arquivo a gravar. Padrão: o nome que vier da URL."
    )
    background: bool = Field(
        default=True,
        description="True baixa em segundo plano e devolve um id na hora, para "
        "acompanhar com comfy_download_status. Deixe True: um checkpoint leva mais "
        "tempo que o teto desta ferramenta e a chamada morreria no meio.",
    )


@_tool(
    name="comfy_download_model",
    title="Baixar um modelo",
    read_only=False,
    destructive=True,
    idempotent=False,
    open_world=True,
)
async def comfy_download_model(params: DownloadModelInput) -> str:
    """Baixa um arquivo de modelo para a instalação local.

    Grava vários gigabytes em disco e consome banda. Um arquivo de mesmo
    nome no destino é substituído. Confirme com o usuário antes de chamar.

    Com background=True, o padrão, devolve um id de download e volta na
    hora; acompanhe com comfy_download_status. Só use background=False para
    arquivo pequeno, porque a chamada bloqueia até terminar.

    Args:
        params (DownloadModelInput):
            - url (str): URL direta do arquivo
            - relative_path (Optional[str]): pasta de destino no workspace
            - filename (Optional[str]): nome a gravar
            - background (bool): baixar em segundo plano, padrão True

    Returns:
        str: envelope JSON com o id do download, ou o resultado final quando
            background=False.
    """
    args = ["model", "download", "--url", params.url]
    _opt(args, "--relative-path", params.relative_path)
    _opt(args, "--filename", params.filename)
    if params.background:
        args.append("--background")
    return await _cli(args, timeout=120 if params.background else LONG_TIMEOUT)


class DownloadStatusInput(Base):
    download_id: str = Field(
        ..., description="Id devolvido por comfy_download_model.", min_length=1
    )


@_tool(
    name="comfy_download_status",
    title="Progresso de um download",
    read_only=True,
    destructive=False,
    idempotent=True,
    open_world=False,
)
async def comfy_download_status(params: DownloadStatusInput) -> str:
    """Consulta o progresso de um download de modelo.

    É o par de comfy_download_model com background=True. Sem isto o
    download em segundo plano não teria como ser acompanhado.

    Args:
        params (DownloadStatusInput):
            - download_id (str): id do download

    Returns:
        str: envelope JSON com o progresso ou o estado final.
    """
    return await _cli(["model", "download-status", params.download_id], timeout=120)


class InstallNodeInput(Base):
    name: str = Field(
        ...,
        description="Nome do pacote de custom node, como aparece no registro do "
        "ComfyUI-Manager.",
        min_length=1,
    )


@_tool(
    name="comfy_install_node",
    title="Instalar um pacote de custom node",
    read_only=False,
    destructive=True,
    idempotent=False,
    open_world=True,
)
async def comfy_install_node(params: InstallNodeInput) -> str:
    """Instala um pacote de custom node no ComfyUI local.

    Altera o ambiente: baixa código de terceiro e instala as dependências
    Python dele no mesmo interpretador do ComfyUI, o que pode conflitar com
    o que já estava lá. Confirme com o usuário antes de chamar.

    O ComfyUI precisa ser reiniciado depois para enxergar as classes novas —
    comfy_stop_server seguido de comfy_launch_server.

    Args:
        params (InstallNodeInput):
            - name (str): nome do pacote

    Returns:
        str: envelope JSON com o resultado da instalação.
    """
    return await _cli(["node", "install", params.name], timeout=LONG_TIMEOUT)


# ---------------------------------------------------------------------
# Metodologia
# ---------------------------------------------------------------------


class ComfySkillInput(Base):
    name: str = Field(
        default="comfy",
        description="Qual skill imprimir: comfy (geral), comfy-fragments (montar "
        "grafos grandes), comfy-debug (job que falhou), comfy-relay (apresentar "
        "resultado), comfy-director (vídeo narrativo multi cena).",
        pattern="^(comfy|comfy-fragments|comfy-debug|comfy-relay|comfy-director)$",
    )


@_tool(
    name="comfy_read_methodology",
    title="Ler a metodologia oficial do Comfy",
    read_only=True,
    destructive=False,
    idempotent=True,
    open_world=False,
)
async def comfy_read_methodology(params: ComfySkillInput) -> str:
    """Imprime uma das skills de metodologia que vêm embutidas no comfy-cli.

    Conteúdo extenso, milhares de palavras. Carregue sob demanda, não por
    precaução. Quando usar cada uma:

    - comfy: visão geral, contrato de saída, como escolher entre template,
      fragmento e JSON cru. Leia antes de um trabalho grande.
    - comfy-fragments: montar grafos grandes a partir de peças validadas.
    - comfy-debug: um job falhou e você precisa do mapa de erro para correção.
    - comfy-relay: como apresentar imagem e vídeo no chat.
    - comfy-director: vídeo narrativo de várias cenas, continuidade.

    Args:
        params (ComfySkillInput):
            - name (str): qual skill imprimir, padrão 'comfy'

    Returns:
        str: o texto da skill. Não é envelope JSON, é markdown cru.
    """
    out = await _cli_text(["skills", "show", params.name], timeout=120, cap=200000)
    # A CLI devolve o markdown dentro de data.content. Entregar o texto
    # limpo, e não o envelope com quebras de linha escapadas, torna o
    # conteúdo muito mais legível para quem vai seguir a metodologia.
    envelope = _parse_envelope(out)
    if envelope and isinstance(envelope.get("data"), dict):
        content = envelope["data"].get("content")
        if isinstance(content, str) and content.strip():
            return _truncate(content, 120000)
    return _truncate(out, 120000)


# ---------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
