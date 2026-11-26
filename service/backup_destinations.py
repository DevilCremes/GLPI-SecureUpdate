"""Cadastro de destinos pelo Super-Admin. Cada perfil publicado é imutável."""
import configparser
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from urllib.parse import urlsplit

from common import run, syncdir, write
from engine import trusted


class RegistrationError(ValueError):
    """Validation message safe to show in the panel and the notification."""


def require(condition, message):
    if not condition:
        raise RegistrationError(message)


PROFILE = re.compile(r"dest-[0-9a-f]{16}")
DIRECTORY = "destinos"
LOCAL_ROOT = "pastas-locais"
HOST = re.compile(r"[a-zA-Z0-9](?:[a-zA-Z0-9.-]{0,251}[a-zA-Z0-9])?")
FIELDS = {
    "local": {"base"},
    "nas": {"server", "share", "user", "password", "domain"},
    "cloud": {"endpoint", "bucket", "region", "access_key", "secret_key"},
}


def local_bases(state, presets):
    return {"local": state / LOCAL_ROOT, **{
        alias: Path(spec["target"]) for alias, spec in presets.items()
        if spec["type"] == "local" and "target" in spec
    }}


def folder_parts(folder):
    if not folder:
        return []
    parts = folder.split("/")
    require(len(folder) <= 400 and all(re.fullmatch(r"[\w-][\w .-]{0,99}", part)
            and not part.endswith((" ", ".")) for part in parts),
            "Informe uma pasta relativa válida, sem barras nas extremidades ou componentes vazios.")
    return parts


def overlap(path, other):
    return path == other or path in other.parents or other in path.parents


def local_target(state, host, root, parts):
    require(parts, "Informe a subpasta do destino local.")
    default = state / LOCAL_ROOT
    require(not any(overlap(root, p) for p in (host.app, *host.ext))
            and (root == default or not overlap(root, state)), "A base se sobrepõe aos dados do GLPI ou do executor.")
    if root == default:
        root.mkdir(mode=0o700, exist_ok=True)
    # Check every existing ancestor before creating anything under it.
    path = root
    for part in [*parts, None]:
        trusted(path)
        require(not path.stat().st_mode & 0o077, "A pasta de backup precisa ser privada de root (0700).")
        if part is not None:
            path = path / part
            path.mkdir(mode=0o700, exist_ok=True)
    return str(path)


def remote_profile(data):
    kind = data["kind"]
    if kind == "nas":
        require(HOST.fullmatch(data["server"]), "Informe o nome ou IPv4 do servidor SMB.")
        require(re.fullmatch(r"[\w-][\w .$-]{0,79}", data["share"]) and not data["share"].endswith((" ", ".")),
                "Nome de compartilhamento SMB inválido.")
        require(all(re.fullmatch(r"[\w@. -]{1,128}", value) and value == value.strip()
                    for value in (data["user"], data["domain"] or "WORKGROUP")), "Usuário ou domínio SMB inválido.")
        require(data["password"], "Informe a senha do NAS.")
        password = run(["rclone", "obscure", "-"], input=data["password"].encode() + b"\n",
                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout.decode("ascii").strip()
        return data["share"], {"type": "smb", "host": data["server"], "user": data["user"],
                               "domain": data["domain"] or "WORKGROUP", "pass": password}
    endpoint = data["endpoint"]
    if endpoint:
        parsed = urlsplit(endpoint)
        try:
            port = parsed.port
        except ValueError:
            port = None
            require(False, "A porta do endpoint HTTPS é inválida.")
        require(re.fullmatch(r"https://[A-Za-z0-9.-]+(?::[0-9]{1,5})?/?", endpoint)
                and HOST.fullmatch(parsed.hostname or "") and (port is None or port > 0),
                "Informe um endpoint HTTPS com servidor e porta opcional.")
    require(re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", data["bucket"]), "Bucket S3 inválido.")
    require(re.fullmatch(r"[a-z0-9-]{1,64}", data["region"]), "Informe a região S3.")
    require(all(re.fullmatch(r"[A-Za-z0-9/+=_.-]{1,256}", data[key]) for key in ("access_key", "secret_key")),
            "Chave de acesso ou segredo S3 inválido.")
    config = {"type": "s3", "provider": "Other" if endpoint else "AWS", "env_auth": "false",
              "access_key_id": data["access_key"], "secret_access_key": data["secret_key"],
              "region": data["region"], "no_check_bucket": "true"}
    if endpoint:
        config["endpoint"] = endpoint
    return data["bucket"], config


def register(state, host, alias, data, presets, existing, probe):
    require(PROFILE.fullmatch(alias) and alias not in existing, "Identificador de destino indisponível.")
    require(isinstance(data, dict) and isinstance(data.get("kind"), str) and data["kind"] in FIELDS,
            "Escolha o tipo de destino.")
    kind = data["kind"]
    require(set(data) == {"kind", "label", "folder"} | FIELDS[kind]
            and all(isinstance(x, str) and len(x) <= 512 and (not x or x.isprintable())
                    for x in data.values()), "Campos de cadastro inválidos.")
    label = data["label"].strip()
    require(1 <= len(label) <= 80 and all(s["label"].casefold() != label.casefold() for s in existing.values()),
            "Escolha um nome de destino exclusivo, com até 80 caracteres.")
    parts = folder_parts(data["folder"])
    configs = state / DIRECTORY
    configs.mkdir(mode=0o700, exist_ok=True)
    trusted(configs)
    stage = Path(tempfile.mkdtemp(prefix=".cadastro-", dir=configs))
    try:
        spec = {"label": label, "type": kind, "profile": alias}
        profile_path = None
        if kind == "local":
            bases = local_bases(state, presets)
            require(data["base"] in bases, "Selecione uma base local cadastrada.")
            spec["target"] = local_target(state, host, bases[data["base"]], parts)
        else:
            container, settings = remote_profile(data)
            spec["target"] = "vault:" + "/".join([container, *parts])
            spec["server"] = data["server"] if kind == "nas" else (data["endpoint"] or "Amazon S3")
            parser = configparser.ConfigParser(interpolation=None)
            parser["vault"] = settings
            profile_path = stage / "rclone.conf"
            with os.fdopen(os.open(profile_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
                parser.write(f)
                f.flush()
                os.fsync(f.fileno())
        probe(spec, state, profile_path=profile_path)
        write(stage / "spec.json", spec)
        stage.rename(configs / alias)
        syncdir(configs)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
