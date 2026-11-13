"""
Integracao com o Infinitum (POPs / fases / cards) - API publica.

Documentacao: https://public.infinitum.app.br/swagger/index.html
Spec:         https://public.infinitum.app.br/swagger/doc.json

Modelo do Infinitum:
  Organizacao -> POPs (processos: lead, opportunity, project, task)
              -> fases (colunas do quadro) -> cards
  Cada card tem `values`: lista de {field_id, field_label, field_type, value}.

Funcoes disponiveis:
  Baixo nivel:
    get_headers()
    requisitar(metodo, caminho, params=None, json=None) -> dict | list

  Consulta:
    listar_organizacoes() -> list
    org_id() -> str                               (INFINITUM_ORG_ID ou a unica org do token)
    listar_pops(tipo=None, ativos=None) -> list
    buscar_pop(pop_id, com_fases=False) -> dict
    listar_fases(pop_id) -> list
    buscar_fase(fase_id) -> dict
    listar_campos(pop_id, fase_id=None) -> list   (pagina tudo)
    listar_tags(pop_id) -> list
    listar_setores() / listar_tipos_pop()
    listar_cards(pop_id, fase_id=None) -> list    (pagina tudo)
    buscar_card(card_id) -> dict

  Escrita:
    criar_card(pop_id, titulo, valores=None, fase_id=None, **extra) -> dict
    atualizar_card(card_id, valores=None, **extra) -> dict
    mover_card(card_id, fase_id) -> dict

  Utilitarios:
    valores_por_rotulo(card) -> {rotulo: valor}
    montar_valores(pop_id, {rotulo_ou_field_id: valor}, fase_id=None) -> [{field_id, value}]
    achar_pop(nome) / achar_fase(pop_id, nome)    (busca por nome, sem acento/caixa)

Auth: header X-Public-Token com INFINITUM_API_TOKEN (.env).

ATENCAO: o Swagger nao descreve o corpo de POST/PUT (objeto livre). O formato
usado em criar/atualizar/mover segue o que a API devolve na leitura
(pop_id, phase_id, title, values[{field_id, value}]). Ainda nao foi testado
contra a producao.
"""
import os
import sys
import json
import logging
import unicodedata

import requests

log = logging.getLogger('integracoes.infinitum')

INFINITUM_BASE = 'https://public.infinitum.app.br/api/v1/public'
TIMEOUT = 30
LIMITE_PAGINA = 100


# ============================================================
# BAIXO NIVEL
# ============================================================

def _base():
    return (os.getenv('INFINITUM_API_BASE') or INFINITUM_BASE).rstrip('/')


def get_headers():
    """Headers com o Public Token. Levanta RuntimeError se nao houver token."""
    token = os.getenv('INFINITUM_API_TOKEN')
    if not token:
        raise RuntimeError('INFINITUM_API_TOKEN nao encontrado no .env')
    return {
        'X-Public-Token': token,
        'Content-Type': 'application/json',
        'Accept': 'application/json',
    }


class InfinitumErro(Exception):
    def __init__(self, status, corpo, metodo, caminho):
        self.status, self.corpo = status, corpo
        super().__init__(f'Infinitum {metodo} {caminho} -> HTTP {status}: {corpo[:300]}')


def requisitar(metodo, caminho, params=None, json=None):
    """Chama a API e devolve o JSON. Erro HTTP (inclusive 'backend returned
    status 4xx' embrulhado em 200/502 pelo gateway) vira InfinitumErro."""
    url = f'{_base()}/{caminho.lstrip("/")}'
    if params:
        params = {k: v for k, v in params.items() if v is not None}
    r = requests.request(metodo, url, headers=get_headers(), params=params,
                         json=json, timeout=TIMEOUT)
    if r.status_code >= 400:
        raise InfinitumErro(r.status_code, r.text, metodo, caminho)
    if not r.content:
        return {}
    dados = r.json()
    if isinstance(dados, dict) and set(dados) == {'error'}:
        raise InfinitumErro(r.status_code, str(dados['error']), metodo, caminho)
    return dados


def _lista(resp):
    """A API devolve ora {'data': [...]} ora a lista direta."""
    if isinstance(resp, list):
        return resp
    if isinstance(resp, dict):
        return resp.get('data') or []
    return []


def _obj(resp):
    """Respostas de item unico vem como {'data': {...}}."""
    if isinstance(resp, dict) and isinstance(resp.get('data'), dict):
        return resp['data']
    return resp


def _paginar(caminho, params):
    itens, pagina = [], 1
    while True:
        resp = requisitar('GET', caminho, {**params, 'page': pagina, 'limit': LIMITE_PAGINA})
        lote = _lista(resp)
        itens.extend(lote)
        total = resp.get('total') if isinstance(resp, dict) else None
        if not lote or len(lote) < LIMITE_PAGINA or (total is not None and len(itens) >= total):
            return itens
        pagina += 1


# ============================================================
# CONSULTA
# ============================================================

def listar_organizacoes():
    return _lista(requisitar('GET', 'organizations/my'))


def org_id():
    """INFINITUM_ORG_ID do .env; sem ele, a unica organizacao do token."""
    oid = os.getenv('INFINITUM_ORG_ID')
    if oid:
        return oid
    orgs = listar_organizacoes()
    if len(orgs) != 1:
        raise RuntimeError(f'Token ve {len(orgs)} organizacoes; defina INFINITUM_ORG_ID')
    return orgs[0]['id']


def listar_pops(tipo=None, ativos=None):
    """tipo: lead | opportunity | project | task."""
    params = {'type': tipo}
    if ativos is not None:
        params['is_active'] = 'true' if ativos else 'false'
    return _lista(requisitar('GET', f'organizations/{org_id()}/pops', params))


def buscar_pop(pop_id, com_fases=False):
    return _obj(requisitar('GET', f'pops/{pop_id}/with-phases' if com_fases else f'pops/{pop_id}'))


def listar_fases(pop_id):
    """Usa /with-phases: o /phases corta em 10 e ignora limit."""
    fases = buscar_pop(pop_id, com_fases=True).get('phases') or []
    return sorted(fases, key=lambda f: f.get('position', 0))


def buscar_fase(fase_id):
    return _obj(requisitar('GET', f'phases/{fase_id}'))


def listar_campos(pop_id, fase_id=None):
    return _paginar('pop-fields/by-pop', {'pop_id': pop_id, 'phase_id': fase_id})


def listar_tags(pop_id):
    return _lista(requisitar('GET', f'pops/{pop_id}/tags'))


def listar_setores():
    return _lista(requisitar('GET', 'pops/sectors'))


def listar_tipos_pop():
    return _lista(requisitar('GET', 'pops/types'))


def listar_cards(pop_id, fase_id=None):
    """A API exige pop_id (so organization_id devolve 400)."""
    return _paginar('cards', {'organization_id': org_id(), 'pop_id': pop_id,
                              'phase_id': fase_id})


def buscar_card(card_id):
    return _obj(requisitar('GET', f'cards/{card_id}'))


# ============================================================
# BANCOS DE DADOS (cadastros: Clientes, Processos, Lancamentos...)
# Endpoints fora do Swagger, descobertos em 13/11/2026.
# ============================================================

def listar_bancos():
    return _lista(requisitar('GET', 'databases', {'organization_id': org_id(), 'limit': 100}))


def achar_banco(nome):
    alvo = _norm(nome)
    return next((b for b in listar_bancos() if _norm(b.get('name')) == alvo), None)


def listar_campos_banco(database_id):
    return _lista(requisitar('GET', 'database-fields', {'database_id': database_id, 'limit': 200}))


def listar_registros(database_id):
    """Todos os registros (a API pagina por page/limit e informa total)."""
    out, pagina = [], 1
    while True:
        resp = requisitar('GET', 'database-records',
                          {'database_id': database_id, 'limit': LIMITE_PAGINA, 'page': pagina})
        lote = _lista(resp)
        out.extend(lote)
        if not lote or len(out) >= (resp.get('total') or 0):
            return out
        pagina += 1


def valor_registro(registro, rotulo):
    alvo = _norm(rotulo)
    return next((f.get('value') for f in registro.get('field_values') or [] if _norm(f.get('label')) == alvo), None)


def criar_registro(database_id, titulo, valores):
    """valores: {slug do campo: valor}. Grava pelo slug e pelo id do campo, como a
    importacao original do ADVBOX fez."""
    campos = {c['field_id']: c['id'] for c in listar_campos_banco(database_id)}
    desconhecidos = [k for k in valores if k not in campos]
    if desconhecidos:
        raise KeyError('campos inexistentes no banco %s: %s' % (database_id, desconhecidos))
    vals = {}
    for slug, v in valores.items():
        if v in (None, ''):
            continue
        vals[slug] = vals[campos[slug]] = str(v)
    return _obj(requisitar('POST', 'database-records',
                           json={'database_id': database_id, 'title': titulo, 'values': vals}))


# ============================================================
# ESCRITA (formato do corpo inferido - ver ATENCAO no topo)
# ============================================================

def criar_card(pop_id, titulo, valores=None, fase_id=None, **extra):
    """valores: [{field_id, value}] (use montar_valores para partir de rotulos).
    extra: assignee_ids, tag_ids, priority, etc., repassados como estao."""
    corpo = {'organization_id': org_id(), 'pop_id': pop_id, 'title': titulo, **extra}
    if fase_id:
        corpo['phase_id'] = fase_id
    if valores:
        corpo['values'] = valores
    return _obj(requisitar('POST', 'cards', json=corpo))


def atualizar_card(card_id, valores=None, **extra):
    corpo = dict(extra)
    if valores:
        corpo['values'] = valores
    return _obj(requisitar('PUT', f'cards/{card_id}', json=corpo))


def mover_card(card_id, fase_id):
    return requisitar('PUT', f'cards/{card_id}/move', json={'phase_id': fase_id})


# ============================================================
# UTILITARIOS
# ============================================================

def _norm(s):
    s = unicodedata.normalize('NFKD', str(s or '')).encode('ascii', 'ignore').decode()
    return ' '.join(s.lower().split())


def valores_por_rotulo(card):
    """{rotulo: valor} de um card. Campos 'database' viram o dict decodificado."""
    out = {}
    for v in card.get('values') or []:
        rotulo = v.get('field_label') or v.get('label') or v.get('field_id')
        valor = v.get('value')
        if v.get('field_type') == 'database' and isinstance(valor, str) and valor.startswith('{'):
            try:
                valor = json.loads(valor)
            except ValueError:
                pass
        out[rotulo] = valor
    return out


def montar_valores(pop_id, dados, fase_id=None):
    """Converte {rotulo, id ou slug do campo: valor} em [{field_id, value}].
    No card, `field_id` e o `id` do pop-field (nao o slug `field_id` dele).
    Rotulo desconhecido levanta KeyError (evita gravar em campo errado)."""
    campos = listar_campos(pop_id, fase_id)
    por_rotulo = {_norm(c['label']): c for c in campos}
    por_chave = {}
    for c in campos:
        por_chave[c['id']] = por_chave[c['field_id']] = c
    out = []
    for chave, valor in dados.items():
        if chave in por_chave:
            fid = por_chave[chave]['id']
        elif _norm(chave) in por_rotulo:
            fid = por_rotulo[_norm(chave)]['id']
        else:
            raise KeyError(f'Campo "{chave}" nao existe no POP {pop_id}')
        out.append({'field_id': fid, 'value': valor})
    return out


def achar_pop(nome):
    alvo = _norm(nome)
    pops = listar_pops()
    for p in pops:
        if _norm(p.get('name')) == alvo:
            return p
    for p in pops:
        if alvo in _norm(p.get('name')):
            return p
    return None


def achar_fase(pop_id, nome):
    alvo = _norm(nome)
    fases = listar_fases(pop_id)
    for f in fases:
        if _norm(f.get('name')) == alvo:
            return f
    for f in fases:
        if alvo in _norm(f.get('name')):
            return f
    return None


# ============================================================
# CLI de conferencia (somente leitura)
#   python infinitum_integration.py mapa            POPs e fases
#   python infinitum_integration.py campos <pop>    campos do POP
#   python infinitum_integration.py cards <pop>     cards do POP
#   python infinitum_integration.py card <id>       um card, por rotulo
# <pop> aceita id ou nome.
# ============================================================

def _carregar_env():
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(os.getenv('ESC_CONFIG_ENV', os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'config', '.env')))
    if os.path.exists('/opt/escritorio/config/.env'):
        load_dotenv('/opt/escritorio/config/.env')


def _pop_id(arg):
    if len(arg) == 36 and arg.count('-') == 4:
        return arg
    p = achar_pop(arg)
    if not p:
        sys.exit(f'POP "{arg}" nao encontrado')
    return p['id']


if __name__ == '__main__':
    _carregar_env()
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'mapa'
    if cmd == 'mapa':
        for o in listar_organizacoes():
            print(f"Organizacao: {o['name']} ({o['id']})")
        for p in listar_pops():
            print(f"\n[{p.get('type') or '-'}] {p['name']}  {p['id']}")
            for f in sorted(listar_fases(p['id']), key=lambda f: f.get('position', 0)):
                print(f"    - {f.get('name')}  {f['id']}")
    elif cmd == 'campos':
        for c in listar_campos(_pop_id(sys.argv[2])):
            print(f"{c['label']!r:60} {c['type']:18} {c['id']}  fase={c.get('phase_id')}")
    elif cmd == 'cards':
        for c in listar_cards(_pop_id(sys.argv[2])):
            print(f"{c['id']}  fase={c.get('phase_id')}  {c.get('title')}")
    elif cmd == 'card':
        print(json.dumps(valores_por_rotulo(buscar_card(sys.argv[2])), ensure_ascii=False, indent=2))
    else:
        sys.exit(__doc__)
