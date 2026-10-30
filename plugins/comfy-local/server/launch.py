"""
launch.py

Ponto de entrada do servidor MCP do plugin comfy-local.

Este arquivo usa apenas a biblioteca padrão, de propósito. Ele roda com o
Python que o usuário apontou na configuração do plugin, que pode não ter
dependência nenhuma instalada. A tarefa dele é garantir o ambiente e só
então executar o servidor de verdade.

O que faz:
  1. Localiza o diretório persistente do plugin (CLAUDE_PLUGIN_DATA), que
     sobrevive a atualizações do plugin.
  2. Cria um venv lá dentro se não existir.
  3. Instala as dependências se o requirements.txt empacotado for diferente
     da cópia guardada, o que cobre tanto a primeira execução quanto uma
     atualização que mude dependências.
  4. Substitui o processo pelo servidor rodando com o Python do venv.

Não apaga nem sobrescreve nada fora do próprio diretório de dados do
plugin, que o Claude Code cria e gerencia.

Regra crítica: stdout pertence ao protocolo MCP. Todo diagnóstico daqui vai
para stderr. Uma única linha solta no stdout quebra o handshake.
"""

from __future__ import annotations

import errno
import os
import subprocess
import sys
import time
from pathlib import Path

# Byte travado no Windows. Fica depois do PID gravado no início do arquivo,
# porque uma região travada não pode ser lida por outro processo — e quem
# espera precisa ler o PID do dono. No POSIX o flock trava o arquivo inteiro
# e não impede leitura.
LOCK_OFFSET = 1024

if os.name == "nt":
    import msvcrt

    LOCK_BUSY = {errno.EACCES, errno.EDEADLOCK}

    def os_lock(fd: int) -> None:
        os.lseek(fd, LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def os_unlock(fd: int) -> None:
        # O Windows solta a região de forma assíncrona ao fechar o arquivo;
        # a documentação manda destravar antes.
        os.lseek(fd, LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

    def child_fds(fd: int) -> tuple[int, ...]:
        # A trava do Windows é do processo e não passa aos filhos: se o
        # Claude Code encerrar o launch.py no meio, o pip órfão segue
        # escrevendo no venv sem trava. Limitação conhecida.
        return ()

else:
    import fcntl

    LOCK_BUSY = {errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES}

    def os_lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def os_unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)

    def child_fds(fd: int) -> tuple[int, ...]:
        # O flock pertence à descrição de arquivo aberta, compartilhada com
        # quem herda o descritor. Passá-lo ao venv e ao pip mantém a trava
        # enquanto eles vivem, mesmo que o Claude Code mate o launch.py no
        # meio: o pip órfão segue escrevendo, e ninguém entra junto.
        return (fd,)

HERE = Path(__file__).resolve().parent
PLUGIN_ROOT = HERE.parent
REQUIREMENTS = PLUGIN_ROOT / "requirements.txt"
SERVER = HERE / "comfy_mcp_server.py"


def log(msg: str) -> None:
    """Diagnóstico vai para stderr. Nunca para stdout."""
    print(f"[comfy-local] {msg}", file=sys.stderr, flush=True)


def data_dir() -> Path:
    """Diretório persistente do plugin, com alternativa local se ausente.

    O Claude Code exporta CLAUDE_PLUGIN_DATA para o processo de todo servidor
    MCP stdio de plugin; o .mcp.json não a declara em `env`, porque uma
    entrada ali sobrescreve a exportada.
    """
    env = (os.environ.get("CLAUDE_PLUGIN_DATA") or "").strip()
    # Um "${...}" que chegou até aqui é placeholder que ninguém substituiu, e
    # usá-lo criaria um diretório com esse nome literal no diretório de
    # trabalho — um venv fora do lugar, invisível para a próxima sessão.
    if env and "${" not in env:
        d = Path(env)
    else:
        # Fora do Claude Code, por exemplo em teste manual. Dentro dele, cair
        # aqui põe o venv na pasta do plugin, que some a cada atualização e
        # obriga a reinstalar tudo dentro do tempo de conexão do MCP.
        if os.environ.get("CLAUDE_PLUGIN_ROOT"):
            log(
                f"AVISO: CLAUDE_PLUGIN_DATA ausente ou não substituída ({env!r}); "
                "o ambiente vai para a pasta do plugin e será refeito a cada atualização"
            )
        d = PLUGIN_ROOT / ".local-data"
    d.mkdir(parents=True, exist_ok=True)
    return d


def venv_python(venv: Path) -> Path:
    if os.name == "nt":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def needs_install(venv: Path, stamp: Path) -> bool:
    """True se o venv não existe ou se as dependências mudaram."""
    if not venv_python(venv).exists():
        return True
    if not stamp.exists():
        return True
    try:
        return stamp.read_text(encoding="utf-8") != REQUIREMENTS.read_text(encoding="utf-8")
    except OSError:
        return True


def try_lock(fd: int) -> bool:
    """Tenta a trava exclusiva do sistema operacional sobre `fd`, sem esperar.

    Só "ocupada" vira False. Qualquer outro erro (sistema de arquivos sem
    suporte a trava, por exemplo) sobe: tratá-lo como ocupada faria esperar
    para sempre por uma sessão que não existe.
    """
    try:
        os_lock(fd)
    except OSError as exc:
        if exc.errno in LOCK_BUSY:
            return False
        raise
    return True


def lock_owner(lock: Path) -> str:
    """PID que o dono da trava gravou nela, para o log de espera."""
    try:
        return lock.read_text(encoding="utf-8").strip() or "desconhecido"
    except OSError:
        return "desconhecido"


def acquire_lock(lock: Path, venv: Path, stamp: Path) -> int | None:
    """Toma a trava de provisionamento, ou espera quem já a tem.

    Duas janelas do Claude Code abertas logo depois de habilitar o plugin
    disparam dois provisionamentos no mesmo diretório de dados, e o perdedor
    pode deixar uma instalação pela metade que o carimbo depois declara boa.

    A trava é do sistema operacional (flock no POSIX, locking no Windows), não
    a existência do arquivo: o kernel a solta quando o processo dono morre,
    inclusive quando o Claude Code o encerra por estourar o tempo de conexão
    no meio da instalação. Por isso uma trava ocupada sempre tem dono vivo, e
    esperar por ela é seguro; o próprio tempo de conexão do Claude Code limita
    a espera. O arquivo fica no disco de propósito; apagá-lo deixaria duas
    sessões com travas em arquivos diferentes.

    Devolve o descritor com a trava na mão se esta sessão deve provisionar, e
    None se outra terminou o serviço enquanto esperávamos.
    """
    fd = os.open(str(lock), os.O_CREAT | os.O_RDWR)
    try:
        avisou = False
        while not try_lock(fd):
            if not needs_install(venv, stamp):
                os.close(fd)
                return None  # a outra sessão terminou; nada a fazer
            if not avisou:
                log(
                    f"outra sessão (PID {lock_owner(lock)}) está preparando o "
                    "ambiente, aguardando"
                )
                avisou = True
            time.sleep(1)
    except OSError:
        os.close(fd)
        raise

    # Com a trava na mão, confere de novo: entre a última checagem e agora,
    # quem a soltou pode ter acabado de deixar tudo pronto.
    if needs_install(venv, stamp):
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, str(os.getpid()).encode("utf-8"))
        return fd
    release_lock(fd)
    return None


def release_lock(fd: int) -> None:
    """Destrava e fecha o descritor da trava."""
    try:
        os_unlock(fd)
    finally:
        os.close(fd)


def venv_ready(py: Path) -> bool:
    """True se o venv existe e o pip dele roda.

    O binário sozinho não basta: o Claude Code pode encerrar o `python -m
    venv` depois de criar o executável e antes de o ensurepip terminar, e um
    venv sem pip nunca instalaria nada.
    """
    if not py.exists():
        return False
    result = subprocess.run(
        [str(py), "-m", "pip", "--version"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def provision(venv: Path, stamp: Path, lock_fd: int) -> None:
    """Cria o venv e instala as dependências."""
    py = venv_python(venv)
    pass_fds = child_fds(lock_fd)

    if not venv_ready(py):
        log(f"criando venv em {venv}")
        # --clear refaz do zero um venv que ficou pela metade.
        subprocess.run(
            [sys.executable, "-m", "venv", "--clear", str(venv)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            pass_fds=pass_fds,
        )

    log("instalando dependências")
    subprocess.run(
        [str(py), "-m", "pip", "install", "--quiet", "--upgrade", "pip"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        pass_fds=pass_fds,
    )
    subprocess.run(
        [str(py), "-m", "pip", "install", "--quiet", "-r", str(REQUIREMENTS)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        pass_fds=pass_fds,
    )

    # A marca só é gravada depois do sucesso. Se a instalação falhar, a
    # próxima sessão tenta de novo em vez de assumir ambiente pronto.
    stamp.write_text(REQUIREMENTS.read_text(encoding="utf-8"), encoding="utf-8")
    log("ambiente pronto")


def main() -> None:
    if not SERVER.exists():
        log(f"ERRO: servidor não encontrado em {SERVER}")
        sys.exit(1)
    if not REQUIREMENTS.exists():
        log(f"ERRO: requirements.txt não encontrado em {REQUIREMENTS}")
        sys.exit(1)

    data = data_dir()
    venv = data / "venv"
    stamp = data / "requirements.installed.txt"
    lock = data / "provision.lock"

    if needs_install(venv, stamp):
        try:
            lock_fd = acquire_lock(lock, venv, stamp)
        except OSError as exc:
            log(f"ERRO: não foi possível travar {lock}: {exc}")
            log(
                "O diretório de dados do plugin precisa estar num sistema de "
                "arquivos com suporte a trava de arquivo."
            )
            sys.exit(1)
        if lock_fd is not None:
            try:
                provision(venv, stamp, lock_fd)
            except subprocess.CalledProcessError as exc:
                detail = (exc.stderr or b"").decode("utf-8", errors="replace")[:1500]
                log("ERRO: falha ao preparar o ambiente Python.")
                log(f"comando: {' '.join(str(a) for a in exc.cmd)}")
                log(detail)
                log(
                    "Verifique se o Python configurado no plugin consegue criar venv "
                    "e alcançar o PyPI."
                )
                sys.exit(1)
            finally:
                release_lock(lock_fd)

    py = venv_python(venv)
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    env.pop("PYTHONPATH", None)

    # No Windows não existe execv real; usa subprocesso e propaga o código.
    if os.name == "nt":
        proc = subprocess.run([str(py), str(SERVER)], env=env)
        sys.exit(proc.returncode)

    os.execve(str(py), [str(py), str(SERVER)], env)


if __name__ == "__main__":
    main()
