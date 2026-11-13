# -*- coding: utf-8 -*-
"""Integracao com o ADVBOX (API v1: https://app.advbox.com.br/api/v1, Bearer token).

Liga sozinho quando ADVBOX_API_TOKEN (ou ADVBOX_TOKEN) esta no .env. Sem token, volta ao
comportamento antigo: toda leitura devolve vazio e nada sai da maquina.

LEITURA (liga com o token):
    carregar_settings()                         usuarios, tipos de tarefa, etapas, tipos de acao...
    listar_tarefas(**filtros)                   GET /posts  (date_start, date_end, completed_start,
                                                completed_end, user_id, lawsuit_id; pagina tudo)
    listar_historico(lawsuit_id)                GET /history/{id}
    buscar_processo(numero=None, nome=None, id=None)  -> list (id -> dict)
    listar_processos(**filtros)                 GET /lawsuits
    buscar_cliente(documento=None, nome=None)   -> list
    listar_clientes(**filtros)                  GET /customers
    listar_lancamentos(**filtros)               GET /transactions (financeiro)
    buscar_tipo_tarefa(nome) -> id | None
    buscar_usuario(nome) -> id | None

ESCRITA (so com ADVBOX_ESCRITA=1; padrao 0):
    criar_publicacao(lawsuit_id, task_id, guest_ids, ...)   tarefa (POST /posts)
    criar_movimentacao(lawsuit_id, data, texto)             andamento (POST /movements)
  Com ADVBOX_ESCRITA=0 a tarefa continua nascendo no sistema (tarefas_esc) e o andamento so
  vai para o log, exatamente como antes. Nada escrito no ADVBOX pode ser apagado pela API.

A API tem limite de requisicoes: 429 espera e tenta de novo.
"""
import datetime as dt
import logging
import os
import sys
import time
import unicodedata

log = logging.getLogger('integracoes.advbox')

BASE = 'https://app.advbox.com.br/api/v1'
TIMEOUT = 40
LIMITE_PAGINA = 100
MAX_PAGINAS = 1000


def _token():
    return (os.getenv('ADVBOX_API_TOKEN') or os.getenv('ADVBOX_TOKEN') or '').strip()


def ativo():
    return bool(_token())


def escrita_ativa():
    return ativo() and os.getenv('ADVBOX_ESCRITA', '0').strip() == '1'


# compatibilidade: quem lia advbox_integration.ATIVO
ATIVO = ativo()


class _Vazio(dict):
    """{} que tambem aceita d['data'], .get('data') e iteracao (retorno sem token)."""

    def __missing__(self, chave):
        return _Vazio()


class AdvboxErro(Exception):
    def __init__(self, status, corpo, metodo, caminho):
        self.status, self.corpo = status, corpo
        super().__init__('ADVBOX %s %s -> HTTP %s: %s' % (metodo, caminho, status, str(corpo)[:300]))


# ============================================================
# BAIXO NIVEL
# ============================================================

def requisitar(metodo, caminho, params=None, json=None, tentativas=4):
    import requests
    url = '%s/%s' % ((os.getenv('ADVBOX_API_BASE') or BASE).rstrip('/'), caminho.lstrip('/'))
    headers = {'Authorization': 'Bearer %s' % _token(), 'Accept': 'application/json'}
    if params:
        params = {k: v for k, v in params.items() if v not in (None, '')}
    for i in range(tentativas):
        r = requests.request(metodo, url, headers=headers, params=params, json=json, timeout=TIMEOUT)
        if r.status_code == 429 and i < tentativas - 1:
            espera = int(r.headers.get('Retry-After') or 0) or 15 * (i + 1)
            log.warning('ADVBOX 429 em %s; esperando %ss', caminho, espera)
            time.sleep(espera)
            continue
        if r.status_code >= 400:
            raise AdvboxErro(r.status_code, r.text, metodo, caminho)
        if r.status_code == 204 or not r.content:
            return {}
        return r.json()
    raise AdvboxErro(429, 'limite de requisicoes', metodo, caminho)


def _paginar(caminho, filtros):
    """Pagina ate o fim. `limit` dos filtros e o TETO total de itens (nao o tamanho da pagina)."""
    itens, offset = [], 0
    teto = int(filtros.pop('limit', 0) or 0)
    limite = min(teto, LIMITE_PAGINA) if teto else LIMITE_PAGINA
    for _ in range(MAX_PAGINAS):
        resp = requisitar('GET', caminho, {**filtros, 'limit': limite, 'offset': offset})
        lote = (resp.get('data') or []) if isinstance(resp, dict) else (resp or [])
        itens.extend(lote)
        total = resp.get('totalCount') if isinstance(resp, dict) else None
        offset += len(lote)
        if not lote or len(lote) < limite or (total is not None and offset >= total) or (teto and offset >= teto):
            break
    return itens[:teto] if teto else itens


def _norm(txt):
    s = unicodedata.normalize('NFD', str(txt or ''))
    return ' '.join(''.join(ch for ch in s if unicodedata.category(ch) != 'Mn').upper().split())


def _so_digitos(txt):
    return ''.join(ch for ch in str(txt or '') if ch.isdigit())


# ============================================================
# LEITURA
# ============================================================

_settings_cache = {'em': 0, 'dados': None}


def carregar_settings(cache_segundos=600):
    if not ativo():
        return _Vazio()
    if _settings_cache['dados'] is None or time.time() - _settings_cache['em'] > cache_segundos:
        _settings_cache['dados'] = requisitar('GET', 'settings')
        _settings_cache['em'] = time.time()
    return _settings_cache['dados']


def listar_tarefas(**filtros):
    if not ativo():
        return []
    return _paginar('posts', filtros)


def listar_historico(lawsuit_id):
    if not ativo() or not lawsuit_id:
        return []
    resp = requisitar('GET', 'history/%s' % lawsuit_id)
    return (resp.get('data') or []) if isinstance(resp, dict) else (resp or [])


def listar_processos(**filtros):
    if not ativo():
        return []
    return _paginar('lawsuits', filtros)


def buscar_processo(numero=None, nome=None, id=None, **filtros):
    """Por id devolve o dict do processo; por numero (CNJ, com ou sem mascara) ou nome do
    cliente devolve a lista de processos encontrados."""
    if not ativo():
        return [] if not id else None
    if id:
        try:
            return requisitar('GET', 'lawsuits/%s' % id)
        except AdvboxErro as e:
            if e.status == 404:
                return None
            raise
    if numero:
        achados = listar_processos(process_number=numero, **filtros)
        if not achados and _so_digitos(numero) != numero:
            achados = listar_processos(process_number=_so_digitos(numero), **filtros)
        return achados
    if nome:
        return listar_processos(name=nome, **filtros)
    return listar_processos(**filtros)


def listar_clientes(**filtros):
    if not ativo():
        return []
    return _paginar('customers', filtros)


def buscar_cliente(documento=None, nome=None, **filtros):
    """Lista de clientes pelo CPF/CNPJ (compara so digitos) ou pelo nome."""
    if not ativo():
        return []
    if documento:
        alvo = _so_digitos(documento)
        achados = listar_clientes(identification=documento, **filtros)
        return [c for c in achados if _so_digitos(c.get('identification') or c.get('document')) == alvo] or achados
    if nome:
        return listar_clientes(name=nome, **filtros)
    return listar_clientes(**filtros)


def listar_lancamentos(**filtros):
    if not ativo():
        return []
    return _paginar('transactions', filtros)


def _tipos_equivalentes():
    """ADVBOX_TIPOS={"NOME DO ROBO": "NOME NO ADVBOX" ou id, ...} (JSON no .env)."""
    import json
    try:
        return {_norm(k): v for k, v in json.loads(os.getenv('ADVBOX_TIPOS') or '{}').items()}
    except ValueError:
        log.warning('ADVBOX_TIPOS nao e JSON valido; ignorado')
        return {}


def buscar_tipo_tarefa(nome):
    """id do tipo de tarefa pelo nome EXATO (sem acento/caixa), pelo id, ou pela tabela
    ADVBOX_TIPOS. Sem busca por trecho: tipo errado no ADVBOX nao se desfaz."""
    if not ativo() or nome in (None, ''):
        return None
    if str(nome).isdigit():
        return int(nome)
    equivalente = _tipos_equivalentes().get(_norm(nome))
    if equivalente is not None:
        if str(equivalente).isdigit():
            return int(equivalente)
        nome = equivalente
    alvo = _norm(nome)
    for t in carregar_settings().get('tasks') or []:
        if _norm(t.get('task')) == alvo:
            return t.get('id')
    return None


def buscar_usuario(nome):
    """id do usuario do ADVBOX pelo nome completo ou primeiro nome."""
    if not ativo() or not nome:
        return None
    if str(nome).isdigit():
        return int(nome)
    alvo = _norm(nome)
    usuarios = carregar_settings().get('users') or []
    for u in usuarios:
        if _norm(u.get('name')) == alvo:
            return u.get('id')
    primeiro = alvo.split()[0]
    candidatos = [u for u in usuarios if _norm(u.get('name')).split()[:1] == [primeiro]]
    return candidatos[0].get('id') if len(candidatos) == 1 else None


# ============================================================
# ESCRITA (ADVBOX_ESCRITA=1)
# ============================================================

def _usuario_advbox(g):
    """Responsavel dos robos (papel 'revisora', nome ou id do ADVBOX) -> id do ADVBOX."""
    if isinstance(g, int) or str(g).isdigit():
        return int(g)
    uid = buscar_usuario(g)
    if uid:
        return uid
    try:  # papel de juridico/equipe.json -> nome do usuario do sistema -> ADVBOX
        import tarefas_esc
        c = tarefas_esc.con(escrever=False)
        try:
            _, nome = tarefas_esc.usuario_esc(c, g)
        finally:
            c.close()
        return buscar_usuario(nome)
    except Exception as e:
        log.warning('responsavel %r sem correspondente no ADVBOX: %s', g, e)
        return None


def _data_br(d):
    if not d:
        return None
    s = str(d)[:10]
    try:
        return dt.date.fromisoformat(s).strftime('%d/%m/%Y')
    except ValueError:
        return s


def criar_publicacao(lawsuit_id, task_id, guest_ids, comments='', from_id=None, date_deadline=None, urgent=False,
                     start_date=None, **_):
    """Tarefa. ADVBOX_ESCRITA=1 cria no ADVBOX; senao nasce no sistema (como sempre foi)."""
    if not escrita_ativa():
        import tarefas_esc
        return tarefas_esc.criar_como_advbox(lawsuit_id, task_id, guest_ids, comments, from_id, date_deadline,
                                            urgent, start_date)
    tipo = buscar_tipo_tarefa(task_id)
    if not tipo:
        raise ValueError('tipo de tarefa %r nao existe no ADVBOX' % (task_id,))
    guests = guest_ids if isinstance(guest_ids, (list, tuple)) else [guest_ids]
    ids = [u for u in (_usuario_advbox(g) for g in guests) if u]
    if not ids:
        raise ValueError('nenhum responsavel encontrado no ADVBOX: %r' % (guest_ids,))
    autor = _usuario_advbox(from_id) if from_id else None
    corpo = {
        'from': autor or ids[0],
        'guests': ids,
        'tasks_id': tipo,
        'lawsuits_id': int(lawsuit_id),
        'comments': comments or '',
        'start_date': _data_br(start_date) or dt.date.today().strftime('%d/%m/%Y'),
        'date_deadline': _data_br(date_deadline),
        'urgent': 1 if urgent else 0,
        'important': 1 if urgent else 0,
        'display_schedule': 1,
    }
    return requisitar('POST', 'posts', json={k: v for k, v in corpo.items() if v is not None})


def criar_movimentacao(lawsuit_id, data, texto):
    """Andamento no processo. data: dd/mm/aaaa ou aaaa-mm-dd."""
    if not escrita_ativa():
        log.info('andamento no processo %s ficou so no log (ADVBOX_ESCRITA desligada)', lawsuit_id)
        return _Vazio()
    return requisitar('POST', 'movements', json={'lawsuit_id': int(lawsuit_id), 'date': _data_br(data),
                                                 'description': str(texto)})


def __getattr__(nome):
    """Funcao antiga que nao existe mais aqui: nao derruba quem chama."""
    if nome.startswith('__'):
        raise AttributeError(nome)

    def f(*a, **k):
        log.info('advbox_integration.%s nao implementada; devolvendo vazio', nome)
        return _Vazio()
    f.__name__ = nome
    return f


# ============================================================
# CLI de conferencia (somente leitura)
#   python advbox_integration.py resumo
#   python advbox_integration.py processo <CNJ ou nome>
#   python advbox_integration.py cliente <CPF ou nome>
#   python advbox_integration.py historico <lawsuit_id>
#   python advbox_integration.py tarefas <aaaa-mm-dd> <aaaa-mm-dd>
# ============================================================

if __name__ == '__main__':
    import json
    try:
        from dotenv import load_dotenv
        load_dotenv(os.getenv('ESC_CONFIG_ENV', os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'config', '.env')))
        if os.path.exists('/opt/escritorio/config/.env'):
            load_dotenv('/opt/escritorio/config/.env')
    except ImportError:
        pass
    if not ativo():
        sys.exit('ADVBOX_API_TOKEN nao definido')
    cmd, args = (sys.argv[1] if len(sys.argv) > 1 else 'resumo'), sys.argv[2:]
    mostrar = lambda x: print(json.dumps(x, ensure_ascii=False, indent=2)[:6000])  # noqa: E731
    if cmd == 'resumo':
        s = carregar_settings()
        print('Escrita no ADVBOX:', 'LIGADA' if escrita_ativa() else 'desligada (ADVBOX_ESCRITA=0)')
        print('Usuarios:', ', '.join('%s (%s)' % (u['name'], u['id']) for u in s.get('users') or []))
        for k in ('tasks', 'stages', 'lawsuit_types', 'origins'):
            print('%s: %d' % (k, len(s.get(k) or [])))
    elif cmd == 'processo':
        q = ' '.join(args)
        mostrar(buscar_processo(numero=q) if any(ch.isdigit() for ch in q) else buscar_processo(nome=q))
    elif cmd == 'cliente':
        q = ' '.join(args)
        mostrar(buscar_cliente(documento=q) if _so_digitos(q) else buscar_cliente(nome=q))
    elif cmd == 'historico':
        mostrar(listar_historico(args[0]))
    elif cmd == 'tarefas':
        for t in listar_tarefas(date_start=args[0], date_end=args[1]):
            print(t.get('id'), t.get('date'), t.get('task'), '|', (t.get('lawsuit') or {}).get('process_number'))
    else:
        sys.exit(__doc__)
