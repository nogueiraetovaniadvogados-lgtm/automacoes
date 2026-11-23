# -*- coding: utf-8 -*-
"""Dados do escritorio no Infinitum, no FORMATO do antigo advbox_integration.

O ADVBOX foi descontinuado em 19/11/2026. Este modulo tem as mesmas funcoes e devolve os
mesmos formatos que o advbox_integration devolvia, entao quem lia o ADVBOX troca so o import:

    import infinitum_dados as AB

Onde cada coisa esta no Infinitum:
    processos      banco "Processos" (cliente e parte adversa sao vinculos)
    clientes       bancos "Clientes" e "Parte adversa"
    historico      banco "Movimentacoes" (andamentos)
    tarefas        cards do POP "Controladoria" (a fase do card e o andamento da tarefa;
                   as publicacoes entram sozinhas na fase "1. Publicacoes")
    financeiro     NAO fica aqui: vem do Asaas (INTEGRACOES/asaas_integration.py)

Ids: o "id" de processo/cliente e o ID Advbox quando o registro tem um (o banco do sistema
guarda esses ids em jur_processos.advbox_id); registro novo, sem ID Advbox, usa o id do
registro no Infinitum. buscar_processo(id=...) aceita os dois.

Cache: a API do Infinitum nao filtra e devolve 100 registros por pagina (~1s cada). Os bancos
ficam em config/cache_infinitum/ (fora do git: tem dado de cliente) e sao relidos quando
passam de CACHE_MIN minutos. Movimentacoes le so as paginas novas (a API devolve do mais
novo para o mais antigo); a releitura completa dele fica para o cron. Cron:
    python infinitum_dados.py atualizar        (a cada 15 min: mantem o cache quente)
    python infinitum_dados.py atualizar --tudo (1x por noite: pega edicao/exclusao de andamento)

Escrita (so com INFINITUM_ESCRITA=1; padrao 0):
    criar_publicacao(...)    a tarefa nasce no sistema (tarefas_esc, como sempre) e ganha
                             um card no POP Controladoria
    criar_movimentacao(...)  andamento no banco Movimentacoes
Com INFINITUM_ESCRITA=0 nada sai daqui: a tarefa fica so no sistema e o andamento so no log.
"""
import datetime as dt
import json
import logging
import os
import re
import sys
import tempfile
import time
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import infinitum_integration as INF  # noqa: E402

log = logging.getLogger('integracoes.infinitum_dados')

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.getenv('INFINITUM_CACHE_DIR') or os.path.join(RAIZ, 'config', 'cache_infinitum')
CACHE_MIN = int(os.getenv('INFINITUM_CACHE_MIN') or 30)
INCREMENTAL = {'Movimentacoes'}          # bancos que crescem por cima e quase nao sao editados

POP_TAREFAS = os.getenv('INFINITUM_POP_TAREFAS') or 'Controladoria'
FASES_CONCLUIDAS = ('PRAZO CUMPRIDO', 'MERO EXPEDIENTE', 'RENUNCIA')
# tipo de tarefa dos robos -> fase do card (o resto vai para a analise)
FASE_DO_TIPO = (
    ('REVIS', '4. Revisão'), ('PROTOCOL', '5. Protocolo'), ('ELABORA', '3. Elaboração'),
    ('PECA', '3. Elaboração'), ('PUBLICA', '1. Publicações'), ('INTIMA', '1. Publicações'),
)
FASE_PADRAO = '2. Em Análise / Estratégia'


class _Vazio(dict):
    """{} que aceita d['x'], .get('x') e iteracao: o mesmo retorno vazio do advbox_integration."""

    def __missing__(self, chave):
        return _Vazio()


# ============================================================
# BASE
# ============================================================

if not os.getenv('INFINITUM_API_TOKEN'):   # backend/robos que nao exportaram o config/.env
    try:
        INF._carregar_env()
    except Exception as _e:
        log.warning('config/.env nao carregado: %s', _e)


def ativo():
    return bool((os.getenv('INFINITUM_API_TOKEN') or '').strip())


def escrita_ativa():
    return ativo() and os.getenv('INFINITUM_ESCRITA', '0').strip() == '1'


ATIVO = ativo()


def _norm(txt):
    s = unicodedata.normalize('NFD', str(txt or ''))
    return ' '.join(''.join(ch for ch in s if unicodedata.category(ch) != 'Mn').upper().split())


def _digitos(txt):
    return re.sub(r'\D', '', str(txt or ''))


def _txt(v):
    return '' if v is None else str(v).strip()


def _data(v):
    """'2026-11-25T00:00:00Z' / '25/11/2026' -> '2026-11-25' (ou None)."""
    s = _txt(v)
    if not s:
        return None
    if '/' in s[:10]:
        try:
            return dt.datetime.strptime(s[:10], '%d/%m/%Y').date().isoformat()
        except ValueError:
            return s
    return s[:10]


def _num(v):
    try:
        return float(v) if _txt(v) else None
    except ValueError:
        return None


def _vinculos(v):
    """Campo 'database': '{"record_id","title"}' ou lista disso -> [{'record_id','title'}]."""
    if isinstance(v, str) and v.strip()[:1] in '[{':
        try:
            v = json.loads(v)
        except ValueError:
            return []
    if isinstance(v, dict):
        v = [v]
    return [x for x in v or [] if isinstance(x, dict) and x.get('record_id')]


def _ids_advbox(v):
    """'17905118, 17961830' (registro mesclado pela curadoria) -> ['17905118', '17961830']."""
    return [x for x in re.split(r'[\s,;/]+', _txt(v)) if x]


# ============================================================
# CACHE DOS BANCOS
# ============================================================

_mem = {}   # nome -> {'em': ts, 'total_em': ts, 'regs': [...]}


def _arq(nome):
    return os.path.join(CACHE_DIR, re.sub(r'[^a-z0-9]+', '_', _norm(nome).lower()) + '.json')


def _ler_disco(nome):
    try:
        with open(_arq(nome), encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _gravar_disco(nome, dados):
    os.makedirs(CACHE_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=CACHE_DIR, suffix='.tmp')
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(dados, f, ensure_ascii=False)
    os.replace(tmp, _arq(nome))


def _plano(r):
    """Registro da API -> {'record_id','title','created_at','updated_at', <slug>: valor}."""
    out = {'record_id': r['id'], 'title': _txt(r.get('title')),
           'created_at': r.get('created_at'), 'updated_at': r.get('updated_at')}
    for f in r.get('field_values') or []:
        if f.get('field_id'):
            out[f['field_id']] = f.get('value')
    return out


def _pagina(banco_id, pagina):
    resp = INF.requisitar('GET', 'database-records',
                          {'database_id': banco_id, 'limit': INF.LIMITE_PAGINA, 'page': pagina})
    return INF._lista(resp), (resp.get('total') or 0) if isinstance(resp, dict) else 0


_bancos = {}


def _banco_id(nome):
    if nome not in _bancos:
        b = INF.achar_banco(nome)
        if not b:
            raise RuntimeError('banco "%s" nao existe no Infinitum' % nome)
        _bancos[nome] = b['id']
    return _bancos[nome]


def _ler_tudo(banco_id):
    out, pagina = [], 1
    while True:
        lote, total = _pagina(banco_id, pagina)
        out += [_plano(r) for r in lote]
        if not lote or len(out) >= total:
            return out
        pagina += 1


def _ler_novos(banco_id, regs):
    """So as paginas do topo, ate achar um registro que o cache ja tem. None: relê tudo."""
    conhecidos = {r['record_id'] for r in regs}
    novos, pagina = [], 1
    while pagina <= 50:
        lote, total = _pagina(banco_id, pagina)
        for r in lote:
            if r['id'] in conhecidos:
                junto = novos + regs
                return junto if len(junto) == total else None   # algo apagado/editado no meio
            novos.append(_plano(r))
        if not lote:
            break
        pagina += 1
    return None


def registros(nome, max_min=None, forcar=False):
    """Todos os registros do banco `nome` (lista de dicts planos), do cache quando da."""
    if not ativo():
        return []
    max_min = CACHE_MIN if max_min is None else max_min
    agora = time.time()
    c = _mem.get(nome) or _ler_disco(nome)
    if c and not forcar and agora - c['em'] < max_min * 60:
        _mem[nome] = c
        return c['regs']
    bid = _banco_id(nome)
    regs = None
    if c and not forcar and nome in INCREMENTAL:
        # a releitura completa (~2 min) fica para o cron (atualizar --tudo); aqui so o topo
        regs = _ler_novos(bid, c['regs'])
        total_em = c.get('total_em', 0)
    if regs is None:
        regs, total_em = _ler_tudo(bid), agora
    c = {'em': agora, 'total_em': total_em, 'regs': regs}
    _mem[nome] = c
    try:
        _gravar_disco(nome, c)
    except OSError as e:
        log.warning('cache do Infinitum nao gravado (%s): %s', nome, e)
    return regs


def esquecer(nome):
    """Invalida o cache de um banco (depois de gravar nele)."""
    _mem.pop(nome, None)
    try:
        os.remove(_arq(nome))
    except OSError:
        pass


# ============================================================
# USUARIOS
# ============================================================

_usuarios = {'em': 0, 'lista': []}


def usuarios():
    """[{'id': user_id, 'name': nome completo}] da organizacao (cache 1h)."""
    if not ativo():
        return []
    if time.time() - _usuarios['em'] > 3600:
        r = INF.requisitar('GET', 'organization-users', {'organization_id': INF.org_id()})
        lista = []
        for u in (r.get('data') if isinstance(r, dict) else r) or []:
            uu = u.get('user') or {}
            nome = u.get('name') or ' '.join(x for x in (uu.get('first_name'), uu.get('last_name')) if x)
            lista.append({'id': u.get('user_id') or uu.get('id'), 'name': nome, 'email': u.get('email')})
        _usuarios.update(em=time.time(), lista=lista)
    return _usuarios['lista']


def buscar_usuario(nome):
    """user_id do Infinitum pelo id, nome completo ou primeiro nome (sem ambiguidade)."""
    if not ativo() or not nome:
        return None
    lista = usuarios()
    if any(u['id'] == nome for u in lista):
        return nome
    alvo = _norm(nome)
    for u in lista:
        if _norm(u['name']) == alvo:
            return u['id']
    primeiro = alvo.split()[0]
    cand = [u for u in lista if _norm(u['name']).split()[:1] == [primeiro]]
    return cand[0]['id'] if len(cand) == 1 else None


def _nome_usuario(uid):
    return next((u['name'] for u in usuarios() if u['id'] == uid), None) if uid else None


# ============================================================
# PESSOAS E PROCESSOS (formato ADVBOX)
# ============================================================

def _id_publico(reg):
    ids = _ids_advbox(reg.get('advbox_id'))
    return ids[0] if ids else reg['record_id']


def _pessoas_por_record():
    out = {}
    for banco, origem in (('Clientes', 'CLIENTE'), ('Parte adversa', 'PARTE CONTRARIA')):
        for r in registros(banco):
            out[r['record_id']] = (r, origem)
    return out


def _cliente_advbox(r, origem=None):
    return {
        'id': _id_publico(r), 'record_id': r['record_id'], 'advbox_id': _txt(r.get('advbox_id')) or None,
        'name': r.get('nome') or r['title'],
        'identification': r.get('documento') or r.get('cpf_cnpj'), 'document': r.get('rg'),
        'email': r.get('email'), 'phone': r.get('telefone'), 'cellphone': r.get('celular'),
        'origin': origem or r.get('origem_advbox') or r.get('origem'),
        'birthdate': r.get('data_nascimento'), 'civil_status': r.get('estado_civil'),
        'occupation': r.get('profissao'), 'gender': r.get('sexo'),
        'street': r.get('endereco'), 'city': r.get('cidade'), 'state': r.get('uf'),
        'postalcode': r.get('cep'), 'number_ctps': r.get('ctps'), 'number_pis': r.get('pis'),
        'notes': r.get('observacoes'), 'created_at': r.get('cadastrado_advbox_em') or r.get('created_at'),
    }


def _processo_advbox(r, pessoas):
    custs = []
    for campo, origem in (('cliente', 'CLIENTE'), ('parte_adversa', 'PARTE CONTRARIA')):
        for v in _vinculos(r.get(campo)):
            p, _ = pessoas.get(v['record_id'], (None, None))
            custs.append({
                'customer_id': _id_publico(p) if p else v['record_id'], 'record_id': v['record_id'],
                'name': (p or {}).get('nome') or v.get('title'),
                'identification': (p or {}).get('documento') or (p or {}).get('cpf_cnpj'),
                'origin': origem,
            })
    if not any(c['origin'] == 'PARTE CONTRARIA' for c in custs) and _txt(r.get('parte_contraria')):
        custs.append({'customer_id': None, 'record_id': None, 'name': _txt(r['parte_contraria']),
                      'identification': None, 'origin': 'PARTE CONTRARIA'})
    resp = r.get('advogado_responsavel')
    return {
        'id': _id_publico(r), 'record_id': r['record_id'], 'advbox_id': _txt(r.get('advbox_id')) or None,
        'process_number': _txt(r.get('numero_do_processo')) or None,
        'protocol_number': r.get('numero_protocolo'), 'folder': r.get('pasta_codigo_interno'),
        'type': r.get('tipo_de_acao') or r.get('natureza_acao'), 'nature': r.get('natureza_acao'),
        'group': r.get('area_do_direito'), 'stage': r.get('fase'), 'step': r.get('status_do_processo'),
        'instance': r.get('instancia'), 'court': r.get('tribunal'), 'court_division': r.get('orgao_vara'),
        'responsible_id': resp, 'responsible': _nome_usuario(resp),
        'process_date': _data(r.get('data_distribuicao_protocolo')),
        'status_closure': _data(r.get('data_de_encerramento')),
        'fees_expec': _num(r.get('honorarios')), 'fees_money': _num(r.get('honorarios_recebidos')),
        'fees_success_pct': _num(r.get('honorarios_exito')), 'value': _num(r.get('valor_da_causa')),
        'contingency': r.get('prognostico'), 'notes': r.get('observacoes'),
        'exit_production': r.get('saida_producao'), 'exit_execution': r.get('saida_execucao'),
        'created_at': r.get('cadastrado_advbox_em') or r.get('data_de_cadastro') or r.get('created_at'),
        'title': r['title'], 'customers': custs,
    }


def listar_processos(**filtros):
    """Processos no formato do ADVBOX. Filtros: process_number, name (cliente), customer_id.
    Outros filtros do ADVBOX (limit, offset...) sao ignorados."""
    if not ativo():
        return []
    pessoas = _pessoas_por_record()
    out = [_processo_advbox(r, pessoas) for r in registros('Processos')]
    num = _digitos(filtros.get('process_number'))
    if num:
        out = [p for p in out if _digitos(p['process_number']) == num or _digitos(p['protocol_number']) == num]
    nome = _norm(filtros.get('name'))
    if nome:
        out = [p for p in out if any(nome in _norm(c['name']) for c in p['customers'])
               or nome in _norm(p['title'])]
    cid = _txt(filtros.get('customer_id'))
    if cid:
        out = [p for p in out if any(_txt(c['customer_id']) == cid or c['record_id'] == cid for c in p['customers'])]
    return out


def buscar_processo(numero=None, nome=None, id=None, **filtros):
    """Por id (ID Advbox ou id do registro) devolve o dict; por numero CNJ (com ou sem mascara)
    ou nome do cliente devolve a lista."""
    if not ativo():
        return None if id else []
    if id:
        alvo = _txt(id)
        for r in registros('Processos'):
            if r['record_id'] == alvo or alvo in _ids_advbox(r.get('advbox_id')):
                return _processo_advbox(r, _pessoas_por_record())
        return None
    if numero:
        return listar_processos(process_number=numero)
    if nome:
        return listar_processos(name=nome)
    return listar_processos(**filtros)


def listar_clientes(**filtros):
    """Clientes (banco Clientes) no formato do ADVBOX. Filtros: identification, name.
    parte_adversa=True inclui o banco Parte adversa (origin 'PARTE CONTRARIA')."""
    if not ativo():
        return []
    out = [_cliente_advbox(r) for r in registros('Clientes')]
    if filtros.get('parte_adversa'):
        out += [_cliente_advbox(r, 'PARTE CONTRARIA') for r in registros('Parte adversa')]
    doc = _digitos(filtros.get('identification'))
    if doc:
        out = [c for c in out if doc in _digitos(c['identification'])]
    nome = _norm(filtros.get('name'))
    if nome:
        out = [c for c in out if nome in _norm(c['name'])]
    return out


def buscar_cliente(documento=None, nome=None, **filtros):
    if not ativo():
        return []
    if documento:
        alvo = _digitos(documento)
        achados = listar_clientes(identification=documento, **filtros)
        return [c for c in achados if _digitos(c['identification']) == alvo] or achados
    if nome:
        return listar_clientes(name=nome, **filtros)
    return listar_clientes(**filtros)


def listar_lancamentos(**filtros):
    """O financeiro saiu daqui: vem do Asaas. Mantido vazio para nao quebrar quem chama."""
    log.info('listar_lancamentos: o financeiro vem do Asaas (asaas_integration), nao do Infinitum')
    return []


# ============================================================
# HISTORICO (banco Movimentacoes)
# ============================================================

def listar_historico(lawsuit_id=None, numero=None):
    """Andamentos do processo no formato do /history do ADVBOX, do mais novo para o mais antigo:
    [{'created_at','comments','task','author','responsible','process_number','start','date_deadline'}].
    lawsuit_id: ID Advbox ou id do registro do processo; numero: CNJ."""
    if not ativo() or not (lawsuit_id or numero):
        return []
    ids, nums = set(), set()
    if lawsuit_id:
        ids.add(_txt(lawsuit_id))
        p = None if numero else buscar_processo(id=lawsuit_id)   # com o numero, nao precisa
        if p:
            ids.update(_ids_advbox(p.get('advbox_id')))
            if p.get('process_number'):
                nums.add(_digitos(p['process_number']))
    if numero:
        nums.add(_digitos(numero))
    nums.discard('')
    out = []
    for m in registros('Movimentacoes'):
        if _txt(m.get('advbox_processo_id')) in ids or _digitos(m.get('numero_processo')) in nums:
            out.append({
                'created_at': _data(m.get('data')) or _data(m.get('created_at')),
                'comments': m.get('descricao') or m.get('titulo') or m['title'],
                'task': m.get('origem') or 'ANDAMENTO', 'author': None, 'responsible': None,
                'process_number': m.get('numero_processo'), 'start': _data(m.get('data')),
                'date_deadline': None, 'record_id': m['record_id'],
            })
    return sorted(out, key=lambda x: str(x['created_at'] or ''), reverse=True)


# ============================================================
# TAREFAS (cards do POP Controladoria)
# ============================================================

_pop = {}


def _pop_tarefas():
    if 'pop' not in _pop:
        p = INF.achar_pop(POP_TAREFAS)
        if not p:
            raise RuntimeError('POP "%s" nao existe no Infinitum' % POP_TAREFAS)
        _pop['pop'] = p
        _pop['fases'] = {f['id']: f.get('name') for f in INF.listar_fases(p['id'])}
    return _pop['pop'], _pop['fases']


def _card_advbox(card, fases, processos_por_num):
    v = INF.valores_por_rotulo(card)
    fase = fases.get(card.get('phase_id')) or ''
    concluida = any(x in _norm(fase) for x in FASES_CONCLUIDAS) or card.get('status') not in (None, 'active')
    num = _txt(v.get('Número do Processo'))
    proc_v = v.get('Processo')
    proc = processos_por_num.get(_digitos(num))
    if not proc and isinstance(proc_v, dict) and proc_v.get('record_id'):
        proc = next((p for p in processos_por_num.values() if p['record_id'] == proc_v['record_id']), None)
    notas = '\n'.join(_txt(v.get(k)) for k in ('O que será feito?', 'Anotações', 'Resumo do texto',
                                                'Anotações da estratégia', 'Anotações da elaboração',
                                                'Anotações da revisão', 'Anotações do protocolo')
                      if _txt(v.get(k)))
    users = [{'user_id': u, 'name': _nome_usuario(u), 'completed': concluida}
             for u in card.get('assignee_ids') or ([card['assigned_to']] if card.get('assigned_to') else [])]
    lw = {'id': proc['id'] if proc else None, 'process_number': (proc or {}).get('process_number') or num or None,
          'customers': (proc or {}).get('customers') or []}
    return {
        'id': card['id'], 'task': card.get('title'), 'phase': fase, 'completed': concluida,
        'notes': notas, 'date': _data(card.get('created_at')),
        'date_deadline': _data(v.get('Prazo fatal') or v.get('Data do prazo (Estipulado pela IA)')),
        'internal_deadline': _data(v.get('Prazo interno')),
        'created_at': card.get('created_at'), 'updated_at': card.get('updated_at'),
        'lawsuits_id': lw['id'], 'lawsuit': lw, 'users': users, 'values': v,
    }


def listar_tarefas(**filtros):
    """Cards do POP de tarefas no formato do /posts do ADVBOX.
    Filtros: date_start/date_end (prazo fatal, ou criacao sem prazo), completed_start/completed_end
    (concluidas no periodo), user_id/usuario, lawsuit_id, task (trecho do titulo ou da fase).
    Sem completed_*: so as abertas, como no ADVBOX."""
    if not ativo():
        return []
    pop, fases = _pop_tarefas()
    procs = {_digitos(p['process_number']): p for p in listar_processos() if p['process_number']}
    out = [_card_advbox(c, fases, procs) for c in INF.listar_cards(pop['id'])]
    if filtros.get('completed_start') or filtros.get('completed_end'):
        ini, fim = _txt(filtros.get('completed_start')) or '0', _txt(filtros.get('completed_end')) or '9'
        out = [t for t in out if t['completed'] and ini <= _txt(t['updated_at'])[:10] <= fim]
    else:
        out = [t for t in out if not t['completed']]
    if filtros.get('date_start') or filtros.get('date_end'):
        ini, fim = _txt(filtros.get('date_start')) or '0', _txt(filtros.get('date_end')) or '9'
        out = [t for t in out if ini <= (t['date_deadline'] or t['date'] or '') <= fim]
    uid = filtros.get('user_id') or (buscar_usuario(filtros['usuario']) if filtros.get('usuario') else None)
    if uid:
        out = [t for t in out if any(u['user_id'] == uid for u in t['users'])]
    if filtros.get('lawsuit_id'):
        out = [t for t in out if _txt(t['lawsuits_id']) == _txt(filtros['lawsuit_id'])]
    if filtros.get('task'):
        alvo = _norm(filtros['task'])
        out = [t for t in out if alvo in _norm(t['task']) or alvo in _norm(t['phase'])]
    return out


def tarefas_abertas(usuario, **filtros):
    uid = buscar_usuario(usuario)
    if not uid:
        return []
    lista = listar_tarefas(user_id=uid, **filtros)
    return sorted(lista, key=lambda t: (t.get('date_deadline') or t.get('date') or ''))


def _fase_do_tipo(tipo):
    try:
        extra = {_norm(k): v for k, v in json.loads(os.getenv('INFINITUM_FASES') or '{}').items()}
    except ValueError:
        extra = {}
    t = _norm(tipo)
    if t in extra:
        return extra[t]
    return next((f for k, f in FASE_DO_TIPO if k in t), FASE_PADRAO)


def buscar_tipo_tarefa(nome):
    """No Infinitum o tipo e a fase do card: devolve o proprio nome (normalizado) para os robos
    que so testam se o tipo existe."""
    return _norm(nome) or None if ativo() else None


# ============================================================
# CONFIGURACOES (no formato do /settings do ADVBOX)
# ============================================================

def _opcoes(banco, slug):
    for c in INF.listar_campos_banco(_banco_id(banco)):
        if c.get('field_id') == slug:
            o = c.get('options')
            o = o.get('options') if isinstance(o, dict) else o
            return [x.get('label') if isinstance(x, dict) else x for x in o or []]
    return []


def carregar_settings(cache_segundos=600):
    """users, stages, lawsuit_types e origins tirados do Infinitum. 'tasks' sao as fases do POP
    de tarefas. Sem 'financial': o financeiro vem do Asaas."""
    if not ativo():
        return _Vazio()
    _, fases = _pop_tarefas()
    return {
        'users': usuarios(),
        'tasks': [{'id': fid, 'task': nome} for fid, nome in fases.items()],
        'stages': [{'id': i + 1, 'stage': s, 'step': s} for i, s in enumerate(_opcoes('Processos', 'fase'))],
        'lawsuit_types': [{'id': i + 1, 'lawsuit': s, 'type': s}
                          for i, s in enumerate(_opcoes('Processos', 'tipo_de_acao'))],
        'origins': [{'id': i + 1, 'origin': s} for i, s in enumerate(_opcoes('Clientes', 'origem'))],
    }


# ============================================================
# ESCRITA (INFINITUM_ESCRITA=1)
# ============================================================

def _responsaveis(guest_ids):
    """Papel de juridico/equipe.json, nome ou user_id -> user_ids do Infinitum."""
    guests = guest_ids if isinstance(guest_ids, (list, tuple)) else [guest_ids]
    ids = []
    for g in guests:
        uid = buscar_usuario(g)
        if not uid:
            try:
                import tarefas_esc
                c = tarefas_esc.con(escrever=False)
                try:
                    _, nome = tarefas_esc.usuario_esc(c, g)
                finally:
                    c.close()
                uid = buscar_usuario(nome)
            except Exception as e:
                log.warning('responsavel %r sem usuario no Infinitum: %s', g, e)
        if uid and uid not in ids:
            ids.append(uid)
    return ids


def criar_card_tarefa(tipo, texto, processo=None, responsaveis=(), prazo_fatal=None, urgente=False):
    """Card no POP de tarefas. processo: dict de buscar_processo ou None."""
    pop, fases = _pop_tarefas()
    fase_nome = _fase_do_tipo(tipo)
    fase_id = next((fid for fid, n in fases.items() if _norm(n) == _norm(fase_nome)), None)
    num = (processo or {}).get('process_number') or ''
    clientes = ' / '.join(c['name'] for c in (processo or {}).get('customers') or []
                          if c['origin'] == 'CLIENTE' and c.get('name'))
    titulo = ' | '.join(x for x in (_txt(tipo).title(), 'Proc. %s' % num if num else '', clientes) if x)
    dados = {'Anotações': texto}
    if num:
        dados['Número do Processo'] = num
    if prazo_fatal:
        dados['Prazo fatal'] = '%sT00:00:00Z' % _data(prazo_fatal)
    if processo and processo.get('record_id'):
        dados['Processo'] = json.dumps({'record_id': processo['record_id'], 'title': processo.get('title')},
                                       ensure_ascii=False)
    extra = {'assignee_ids': list(responsaveis)} if responsaveis else {}
    if urgente:
        extra['priority'] = 'urgent'
    return INF.criar_card(pop['id'], titulo[:200], INF.montar_valores(pop['id'], dados), fase_id, **extra)


def criar_publicacao(lawsuit_id, task_id, guest_ids, comments='', from_id=None, date_deadline=None,
                     urgent=False, start_date=None, **_):
    """Tarefa (assinatura historica do ADVBOX). Nasce SEMPRE no sistema (tarefas_esc, que os
    robos e as telas leem); com INFINITUM_ESCRITA=1 ganha tambem um card no POP Controladoria."""
    import tarefas_esc
    r = tarefas_esc.criar_como_advbox(lawsuit_id, task_id, guest_ids, comments, from_id, date_deadline,
                                      urgent, start_date)
    if not escrita_ativa() or (isinstance(r, dict) and r.get('ja_existia')):
        return r
    try:
        proc = buscar_processo(id=lawsuit_id) if lawsuit_id else None
        card = criar_card_tarefa(task_id, comments, proc, _responsaveis(guest_ids), date_deadline, urgent)
        if isinstance(r, dict):
            r['infinitum_card'] = (card or {}).get('id')
    except Exception as e:
        log.warning('tarefa ficou so no sistema; card no Infinitum falhou: %s', e)
        if isinstance(r, dict):
            r['infinitum_erro'] = str(e)[:200]
    return r


def criar_movimentacao(lawsuit_id, data, texto):
    """Andamento no banco Movimentacoes. data: dd/mm/aaaa ou aaaa-mm-dd."""
    if not escrita_ativa():
        log.info('andamento no processo %s ficou so no log (INFINITUM_ESCRITA desligada)', lawsuit_id)
        return _Vazio()
    proc = buscar_processo(id=lawsuit_id) or {}
    clientes = ', '.join(c['name'] for c in proc.get('customers') or [] if c['origin'] == 'CLIENTE')
    dia = _data(data) or dt.date.today().isoformat()
    texto = _txt(texto)
    r = INF.criar_registro(_banco_id('Movimentacoes'), ('%s %s' % (dia, texto))[:200], {
        'advbox_processo_id': (_ids_advbox(proc.get('advbox_id')) or [None])[0],
        'numero_processo': proc.get('process_number'), 'cliente': clientes,
        'data': dia, 'origem': 'Sistema', 'titulo': texto[:200], 'descricao': texto,
    })
    esquecer('Movimentacoes')
    return r


def __getattr__(nome):
    """Funcao do ADVBOX que nao existe aqui: nao derruba quem chama (mesmo contrato do modulo antigo)."""
    if nome.startswith('__'):
        raise AttributeError(nome)

    def f(*a, **k):
        log.info('infinitum_dados.%s nao existe; devolvendo vazio', nome)
        return _Vazio()
    f.__name__ = nome
    return f


# ============================================================
# CLI (somente leitura, exceto 'atualizar', que so grava o cache local)
#   python infinitum_dados.py atualizar [--tudo]   relê os bancos para o cache
#   python infinitum_dados.py processo <CNJ, nome ou id>
#   python infinitum_dados.py cliente <CPF ou nome>
#   python infinitum_dados.py historico <id ou CNJ>
#   python infinitum_dados.py tarefas [usuario]
# ============================================================

if __name__ == '__main__':
    INF._carregar_env()
    if not ativo():
        sys.exit('INFINITUM_API_TOKEN nao definido')
    cmd, args = (sys.argv[1] if len(sys.argv) > 1 else 'atualizar'), sys.argv[2:]
    mostrar = lambda x: print(json.dumps(x, ensure_ascii=False, indent=2, default=str)[:6000])  # noqa: E731
    if cmd == 'atualizar':
        for banco in ('Clientes', 'Parte adversa', 'Processos', 'Movimentacoes'):
            t = time.time()
            n = len(registros(banco, max_min=0, forcar='--tudo' in args))
            print('%-14s %6d registros  %.0fs' % (banco, n, time.time() - t))
    elif cmd == 'processo':
        q = ' '.join(args)
        if re.fullmatch(r'[0-9a-f-]{36}|\d{5,8}', q):
            mostrar(buscar_processo(id=q))
        else:
            mostrar(buscar_processo(numero=q) if len(_digitos(q)) >= 15 else buscar_processo(nome=q))
    elif cmd == 'cliente':
        q = ' '.join(args)
        mostrar(buscar_cliente(documento=q) if len(_digitos(q)) >= 11 else buscar_cliente(nome=q))
    elif cmd == 'historico':
        q = args[0]
        h = listar_historico(numero=q) if len(_digitos(q)) >= 15 else listar_historico(q)
        print('%d andamento(s)' % len(h))
        for x in h[:30]:
            print(x['created_at'], '|', re.sub(r'\s+', ' ', _txt(x['comments']))[:110])
    elif cmd == 'tarefas':
        lista = tarefas_abertas(' '.join(args)) if args else listar_tarefas()
        print('%d tarefa(s) aberta(s)' % len(lista))
        for t in lista:
            print('%s | prazo %s | %-26s | %s' % (t['date'], t['date_deadline'] or '-', t['phase'][:26],
                                                  _txt(t['task'])[:80]))
    else:
        sys.exit(__doc__)
