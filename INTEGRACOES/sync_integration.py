# -*- coding: utf-8 -*-
"""Integracao com o SYNC (publicacoes, intimacoes e autos digitais). Bearer token.

Liga sozinho quando SYNC_API_TOKEN esta no ambiente ou no config/.env. Base em SYNC_API_BASE
(padrao https://api.sync.atendedireito.app/v1). Uso fora do FastAPI: robos, CLI e conferencias.
O router SISTEMA/backend/app/routers/controladoria.py continua sendo a porta da tela.

LEITURA:
    autos(numero, pagina=1, itens=300)      GET /processos/{n}/autos  -> {capa, total, autos:[...]}
                                            404 "nao sincronizado" -> None
    documentos_com_texto(numero)            itens de autos com tem_markdown (o teor ja baixado)
    documento_texto(doc_id)                 GET /documentos/{id}/markdown -> str (sem o cabecalho)
    documento_arquivo(doc_id)               GET /documentos/{id}/arquivo  -> {url, mime, nome, ...}
    intimacoes(status='todas')              GET /intimacoes (pagina tudo)
    intimacao(intimacao_id)                 GET /intimacoes/{id}  (teor completo do DJEN)
    intimacoes_do_processo(numero)          filtro local por numero (o SYNC guarda ~1 mes)

ACAO QUE CUSTA PLANO:
    sincronizar(numero)                     POST /processos/{n}/sincronizar
  Coloca o processo na fila e passa a monitora-lo: ele CONTA no plano do escritorio (a resposta
  traz `cobranca`). Chame so para processo do escritorio que ainda nao esta no SYNC.

Documento com conteudo_status "indisponivel_origem" (ex.: TST, que exige certificado) nao tem
teor nem arquivo: so o PJe com certificado resolve.
"""
import os
import re
import sys
import time

TIMEOUT = 60


class SyncErro(Exception):
    def __init__(self, status, corpo, caminho):
        self.status, self.corpo = status, corpo
        super().__init__('SYNC %s -> HTTP %s: %s' % (caminho, status, str(corpo)[:300]))


def _token():
    return (os.getenv('SYNC_API_TOKEN') or '').strip()


def ativo():
    return bool(_token())


def _base():
    return (os.getenv('SYNC_API_BASE') or 'https://api.sync.atendedireito.app/v1').rstrip('/')


def _so_digitos(txt):
    return re.sub(r'\D', '', str(txt or ''))


def requisitar(metodo, caminho, params=None, tentativas=3):
    import requests
    h = {'Authorization': 'Bearer %s' % _token(), 'Accept': 'application/json'}
    for i in range(tentativas):
        r = requests.request(metodo, _base() + caminho, headers=h, params=params or {}, timeout=TIMEOUT)
        if r.status_code == 429 and i < tentativas - 1:
            time.sleep(int(r.headers.get('Retry-After') or 0) or 10 * (i + 1))
            continue
        if r.status_code >= 400:
            raise SyncErro(r.status_code, r.text, caminho)
        try:
            return r.json()
        except ValueError:
            return {'markdown': r.text}
    raise SyncErro(429, 'limite de requisicoes', caminho)


# ============================================================
# LEITURA
# ============================================================

def autos(numero, pagina=1, itens=300):
    """Linha do tempo do processo. None se o processo ainda nao foi sincronizado."""
    if not ativo():
        return None
    try:
        return requisitar('GET', '/processos/%s/autos' % numero, {'pagina': pagina, 'itens': itens})
    except SyncErro as e:
        if e.status == 404:
            return None
        raise


def documentos_com_texto(numero):
    a = autos(numero) or {}
    return [x for x in a.get('autos') or [] if x.get('tipo') == 'documento' and x.get('tem_markdown')]


def documento_texto(doc_id):
    """Teor do documento em texto. '' quando o SYNC nao o tem."""
    if not ativo():
        return ''
    md = requisitar('GET', '/documentos/%s/markdown' % doc_id).get('markdown') or ''
    if md.startswith('---'):  # cabecalho YAML (processo, tribunal, tipo_documento...)
        partes = md.split('---', 2)
        md = partes[2] if len(partes) == 3 else md
    return md.strip()


def documento_arquivo(doc_id):
    """{url, mime, nome, expira_em_segundos}: URL assinada (~1h) do original no tribunal."""
    return requisitar('GET', '/documentos/%s/arquivo' % doc_id) if ativo() else {}


def intimacoes(status='todas', max_paginas=50):
    if not ativo():
        return []
    todas = []
    for pag in range(1, max_paginas + 1):
        d = requisitar('GET', '/intimacoes', {'status': status, 'pagina': pag, 'itens': 200}) or {}
        lote = d.get('intimacoes') or []
        todas += lote
        if not lote or len(todas) >= (d.get('total') or 0):
            break
    return todas


def intimacao(intimacao_id):
    return requisitar('GET', '/intimacoes/%d' % int(intimacao_id)) if ativo() else {}


def intimacoes_do_processo(numero, status='todas'):
    """[(data, texto)] das intimacoes que o SYNC ainda guarda; texto completo mesmo se cortado."""
    alvo = _so_digitos(numero)
    out = []
    for it in intimacoes(status):
        if _so_digitos(it.get('processo')) != alvo:
            continue
        txt = it.get('texto') or ''
        if txt.rstrip().endswith(('…', '...')):
            txt = intimacao(it['id']).get('texto') or txt
        out.append((str(it.get('data') or '')[:10], txt))
    return out


# ============================================================
# ACAO QUE CUSTA PLANO
# ============================================================

def sincronizar(numero):
    """Poe o processo na fila do SYNC e liga o monitoramento (conta no plano)."""
    if not ativo():
        raise SyncErro(0, 'SYNC_API_TOKEN nao definido', '/processos/%s/sincronizar' % numero)
    return requisitar('POST', '/processos/%s/sincronizar' % numero)


# ============================================================
# CLI (leitura, salvo "sincronizar")
#   python sync_integration.py autos <CNJ>
#   python sync_integration.py doc <documento_id>
#   python sync_integration.py intimacoes <CNJ>
#   python sync_integration.py sincronizar <CNJ>     (conta no plano)
# ============================================================

if __name__ == '__main__':
    import json
    if not ativo():
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import escritorio_config
        escritorio_config.carregar_env()
    if not ativo():
        sys.exit('SYNC_API_TOKEN nao definido (nem no ambiente nem em config/.env)')
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    cmd, arg = sys.argv[1], sys.argv[2]
    if cmd == 'autos':
        a = autos(arg)
        if a is None:
            sys.exit('processo nao sincronizado no SYNC; use: sincronizar %s' % arg)
        c = a.get('capa') or {}
        print('%s | %s x %s | %s itens' % (c.get('classe'), c.get('polo_ativo'), c.get('polo_passivo'), a.get('total')))
        for x in a.get('autos') or []:
            if x.get('tipo') != 'documento':  # movimento, audiencia e demais eventos
                print('%s %s %s' % ((x.get('tipo') or '?')[:1].upper(), x['data'][:10],
                                    (x.get('descricao') or x.get('titulo') or '').replace('\n', ' ')[:110]))
            else:
                marca = 'TEXTO' if x.get('tem_markdown') else (x.get('conteudo_status') or '')
                print('D %s [%s] %s %s' % (x['data'][:10], x.get('id'), x.get('nome'), marca))
    elif cmd == 'doc':
        print(documento_texto(arg) or '(sem teor no SYNC)')
    elif cmd == 'intimacoes':
        for data, txt in intimacoes_do_processo(arg):
            print('#', data); print(txt); print()
    elif cmd == 'sincronizar':
        print(json.dumps(sincronizar(arg), ensure_ascii=False, indent=2))
    else:
        sys.exit(__doc__)
