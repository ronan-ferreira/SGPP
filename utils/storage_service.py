"""
Serviço de armazenamento persistente de arquivos no Supabase Storage.

Em produção no Render o sistema de arquivos local é efêmero (reinícios/deploys
apagam pastas locais). Este módulo espelha logos institucionais e arquivos
gerados de planejamentos no Supabase Storage via REST API oficial, com fallback
transparente para o disco local durante o desenvolvimento.
"""

import mimetypes
import os
from urllib.parse import quote
import requests


def _obter_config() -> tuple[str, str, str]:
    url = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    bucket = os.getenv("SUPABASE_STORAGE_BUCKET", "sgpp-arquivos").strip() or "sgpp-arquivos"
    return url, key, bucket


def storage_configurado() -> bool:
    """Retorna True se SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY estiverem configurados."""
    url, key, _ = _obter_config()
    return bool(url and key)


def _headers(content_type: str | None = None, upsert: bool = False) -> dict:
    _, key, _ = _obter_config()
    headers = {
        "Authorization": f"Bearer {key}",
        "apikey": key,
    }
    if content_type:
        headers["Content-Type"] = content_type
    if upsert:
        headers["x-upsert"] = "true"
    return headers


def _normalizar_caminho(caminho_remoto: str) -> str:
    return caminho_remoto.replace("\\", "/").lstrip("/")


def upload_bytes(conteudo: bytes, caminho_remoto: str, content_type: str | None = None) -> bool:
    """Envia bytes para o Supabase Storage (com upsert)."""
    if not storage_configurado() or not conteudo or not caminho_remoto:
        return False

    url, _, bucket = _obter_config()
    caminho = _normalizar_caminho(caminho_remoto)
    ctype = content_type or mimetypes.guess_type(caminho)[0] or "application/octet-stream"
    endpoint = f"{url}/storage/v1/object/{quote(bucket, safe='')}/{quote(caminho, safe='/')}"

    try:
        resp = requests.post(
            endpoint,
            headers=_headers(content_type=ctype, upsert=True),
            data=conteudo,
            timeout=25,
        )
        if resp.status_code in (200, 201):
            print(f"[STORAGE] Arquivo enviado para Supabase: {bucket}/{caminho}")
            return True
        print(f"[STORAGE] Falha no upload ({resp.status_code}): {resp.text[:250]}")
        return False
    except Exception as e:
        print(f"[STORAGE] Erro ao enviar arquivo '{caminho}' ao Supabase: {e}")
        return False


def upload_arquivo_local(caminho_local: str, caminho_remoto: str, content_type: str | None = None) -> bool:
    """Lê um arquivo do disco local e envia para o Supabase Storage."""
    if not storage_configurado() or not caminho_local or not os.path.exists(caminho_local):
        return False
    try:
        with open(caminho_local, "rb") as f:
            conteudo = f.read()
        return upload_bytes(conteudo, caminho_remoto, content_type=content_type)
    except Exception as e:
        print(f"[STORAGE] Erro ao ler arquivo local '{caminho_local}': {e}")
        return False


def baixar_bytes(caminho_remoto: str) -> bytes | None:
    """Baixa o conteúdo bruto de um arquivo armazenado no Supabase Storage."""
    if not storage_configurado() or not caminho_remoto:
        return None

    url, _, bucket = _obter_config()
    caminho = _normalizar_caminho(caminho_remoto)
    endpoint = f"{url}/storage/v1/object/{quote(bucket, safe='')}/{quote(caminho, safe='/')}"

    try:
        resp = requests.get(endpoint, headers=_headers(), timeout=25)
        if resp.status_code == 200:
            return resp.content
        if resp.status_code != 404:
            print(f"[STORAGE] Aviso ao baixar '{caminho}' ({resp.status_code}): {resp.text[:200]}")
        return None
    except Exception as e:
        print(f"[STORAGE] Erro ao baixar '{caminho}' do Supabase: {e}")
        return None


def garantir_arquivo_local(caminho_local: str, caminho_remoto: str) -> str | None:
    """
    Garante que o arquivo exista em `caminho_local`.
    Se não existir no disco (ex.: após reinício do Render), baixa do Supabase Storage
    e grava no cache local antes de devolver o caminho.
    """
    if caminho_local and os.path.exists(caminho_local):
        return caminho_local

    if not storage_configurado() or not caminho_remoto:
        return None

    conteudo = baixar_bytes(caminho_remoto)
    if not conteudo:
        return None

    try:
        pasta = os.path.dirname(caminho_local)
        if pasta:
            os.makedirs(pasta, exist_ok=True)
        with open(caminho_local, "wb") as f:
            f.write(conteudo)
        return caminho_local
    except Exception as e:
        print(f"[STORAGE] Erro ao gravar cache local '{caminho_local}': {e}")
        return None


def deletar_arquivo(caminho_remoto: str) -> bool:
    """Remove um arquivo do Supabase Storage (silencioso se não existir)."""
    if not storage_configurado() or not caminho_remoto:
        return False

    url, _, bucket = _obter_config()
    caminho = _normalizar_caminho(caminho_remoto)
    endpoint = f"{url}/storage/v1/object/{quote(bucket, safe='')}"

    try:
        resp = requests.delete(
            endpoint,
            headers=_headers(content_type="application/json"),
            json={"prefixes": [caminho]},
            timeout=15,
        )
        return resp.status_code in (200, 204)
    except Exception as e:
        print(f"[STORAGE] Aviso ao remover '{caminho}' do Supabase: {e}")
        return False
