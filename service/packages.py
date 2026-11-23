"""Preparação isolada do .tgz e recibo para aprovação posterior."""
import contextlib
import hashlib
import os
import shutil
import socket
import subprocess
import time

from common import api, official, require, syncdir, validate_update, write

MAX_BYTES = 512 * 1024**2
CHUNK = 1024**2
TTL = 24 * 3600


def current_asset(plan):
    validate_update(plan)
    data = api("/releases/tags/" + plan["version"])
    asset = official(data)
    require(all(asset[k] == plan[k] for k in ("sha", "url", "version")),
            "A release mudou. Consulte as versões e prepare o novo pacote.")
    entry = next(a for a in data["assets"] if a["browser_download_url"] == asset["url"])
    size = entry.get("size")
    require(type(size) is int and 0 < size <= MAX_BYTES, "Tamanho do pacote oficial incompatível.")
    return {**asset, "size": size}


@contextlib.contextmanager
def downloaded(url):
    command = ["curl", "-q", "--proto", "=https", "--proto-redir", "=https", "-fsSL",
               "--connect-timeout", "15", "--max-time", "900", "--", url]
    with subprocess.Popen(command, stdout=subprocess.PIPE, stdin=subprocess.DEVNULL) as process:
        try:
            yield process.stdout
            require(process.wait() == 0, "Falha no download oficial.")
        finally:
            if process.poll() is None:
                process.kill()
            process.stdout.close()


@contextlib.contextmanager
def uploaded():
    # O descritor é a conexão já autenticada pelo serviço, não um caminho enviado pelo PHP.
    with socket.socket(fileno=os.dup(0)) as channel:
        channel.settimeout(60)
        with channel.makefile("rb") as stream:
            yield stream


def fresh(timestamp):
    return type(timestamp) in (int, float) and 0 <= time.time() - timestamp < TTL


def matches(receipt, plan):
    return (bool(receipt) and bool(plan.get("id")) and receipt.get("plan") == plan["id"]
            and receipt.get("sha") == plan.get("sha") and fresh(receipt.get("checked"))
            and fresh(plan.get("checked")))


def prepare(directory, request):
    temporary = directory / "candidate.part"
    cached = directory / "verified.tgz"
    temporary.unlink(missing_ok=True)
    cached.unlink(missing_ok=True)
    (directory / "verified.json").unlink(missing_ok=True)
    plan = request["plan"]
    require(fresh(plan.get("checked")), "Consulte novamente as versões.")
    asset = current_asset(plan)
    source = request["op"]
    if source == "upload":
        require(request["size"] == asset["size"], "Tamanho diferente do pacote oficial.")
    else:
        require(source == "download", "Origem de pacote inválida.")
    require(shutil.disk_usage(directory).free >= asset["size"] + 64 * CHUNK,
            "Espaço insuficiente para verificar o pacote.")
    digest = hashlib.sha256()
    remaining = asset["size"]
    deadline = time.monotonic() + 900
    try:
        source_stream = uploaded() if source == "upload" else downloaded(asset["url"])
        with source_stream as stream, temporary.open("xb") as output:
            while remaining:
                require(time.monotonic() < deadline, "Tempo de transferência excedido.")
                chunk = stream.read(min(CHUNK, remaining))
                require(bool(chunk), "O pacote recebido está incompleto.")
                digest.update(chunk)
                output.write(chunk)
                remaining -= len(chunk)
            require(not stream.read(1), "O pacote excede o tamanho oficial.")
            require(digest.hexdigest() == asset["sha"], "SHA-256 diferente do oficial.")
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(cached)
        syncdir(directory)
        receipt = {"id": request["id"], "plan": plan["id"], "sha": asset["sha"],
                   "size": asset["size"], "source": source, "actor": request["actor"],
                   "checked": time.time()}
        write(directory / "verified.json", receipt)
        return receipt
    finally:
        temporary.unlink(missing_ok=True)
